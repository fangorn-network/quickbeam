"""linkgen — generate a `sameAs` LINKSET that explicitly fuses two datasources.

Two publishers can describe the SAME real thing with no shared id — e.g. a bar in
Google Places (`gplace:` id) and the same bar in OpenStreetMap (`osm:` id). A
Composed View can't fuse them on identity (no shared alias), so you assert the join
with a *linkset*: a list of `{from, rel:"sameAs", to}` edges the View feeds into its
union-find, collapsing each pair into one fused entity.

This tool BUILDS that list deterministically by matching two sets of nodes on
**coordinate proximity + name similarity** (no ML). Output is a linkset JSON that
`publish_linkset.ts` (fangorn repo) publishes; then you add it to the View with
`publish_view.ts --linkset-name`.

Endpoints are written as **namespaced aliases** (`<namespace>:<value>`), the same
keys the published nodes carry — so the View resolves each endpoint back to its node.

Example — fuse Google Places businesses with OSM businesses:

    quickbeam data linkgen \
      --left  stage_volumes/volume_1_businesses.json --left-alias  gplace:placeId \
      --right stage_volumes/volume_3_osm_businesses.json --right-alias osm:osmId \
      --radius-m 75 --min-name-sim 0.35 \
      --out shotskis_links.json
"""
import argparse
import json
import math
from difflib import SequenceMatcher


def _haversine_m(a, b):
    (lat1, lon1), (lat2, lon2) = a, b
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _parse_coord(s):
    if not isinstance(s, str) or "," not in s:
        return None
    try:
        lat, lon = (float(x) for x in s.split(",", 1))
        return (lat, lon)
    except ValueError:
        return None


def _name_sim(a, b):
    """0..1 name similarity: max of full-string ratio and token-set Jaccard, so
    'Shotskis Bar & Grill' and 'Shotskis' still score high."""
    a, b = (a or "").lower().strip(), (b or "").lower().strip()
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    jacc = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
    return max(ratio, jacc)


def _load_side(path, alias_spec):
    """Read a volume node file → [{alias, coord, name}]. alias_spec is
    '<namespace>:<field>', e.g. 'gplace:placeId'."""
    if ":" not in alias_spec:
        raise SystemExit(f"--*-alias must be '<namespace>:<field>', got {alias_spec!r}")
    ns, field = alias_spec.split(":", 1)
    with open(path) as f:
        nodes = json.load(f)
    out = []
    for n in nodes:
        fields = n.get("fields", {})
        val = fields.get(field)
        coord = _parse_coord(fields.get("coordinates"))
        if not val or coord is None:
            continue  # no join key or no location → can't match it
        out.append({"alias": f"{ns}:{val}", "coord": coord, "name": fields.get("title") or ""})
    return out


def run():
    p = argparse.ArgumentParser(
        description="Generate a sameAs linkset that fuses two coordinate-bearing datasources.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--left", required=True, help="Left node file (e.g. volume_1_businesses.json)")
    p.add_argument("--left-alias", required=True, help="Left endpoint alias as '<namespace>:<field>' (e.g. gplace:placeId)")
    p.add_argument("--right", required=True, help="Right node file (e.g. volume_3_osm_businesses.json)")
    p.add_argument("--right-alias", required=True, help="Right endpoint alias as '<namespace>:<field>' (e.g. osm:osmId)")
    p.add_argument("--radius-m", type=float, default=75.0, help="Max distance (metres) to consider a match")
    p.add_argument("--min-name-sim", type=float, default=0.35, help="Min name similarity (0..1) to accept a match")
    p.add_argument("--rel", default="sameAs", help="Relation to emit (sameAs merges the two entities)")
    p.add_argument("--out", required=True, help="Output linkset JSON path")
    args = p.parse_args()

    left = _load_side(args.left, args.left_alias)
    right = _load_side(args.right, args.right_alias)
    print(f"[linkgen] left={len(left)} matchable, right={len(right)} matchable "
          f"(radius {args.radius_m:.0f}m, min name-sim {args.min_name_sim})")

    # Score every plausible pair, then assign greedily 1:1 so one bar can't fuse
    # into several. Score blends distance (closer = better) and name similarity.
    candidates = []
    for ri, r in enumerate(right):
        for li, l in enumerate(left):
            dist = _haversine_m(l["coord"], r["coord"])
            if dist > args.radius_m:
                continue
            nsim = _name_sim(l["name"], r["name"])
            if nsim < args.min_name_sim:
                continue
            closeness = 1.0 - (dist / args.radius_m)        # 0..1
            score = round(0.5 * closeness + 0.5 * nsim, 4)   # confidence in [0,1]
            candidates.append((score, dist, nsim, li, ri))

    candidates.sort(reverse=True)  # best first
    used_l, used_r, links = set(), set(), []
    for score, dist, nsim, li, ri in candidates:
        if li in used_l or ri in used_r:
            continue
        used_l.add(li)
        used_r.add(ri)
        links.append({
            "from": left[li]["alias"],
            "rel": args.rel,
            "to": right[ri]["alias"],
            "confidence": score,
            "evidence": {"distance_m": round(dist, 1), "name_sim": round(nsim, 3),
                         "left_name": left[li]["name"], "right_name": right[ri]["name"]},
        })

    with open(args.out, "w") as f:
        json.dump(links, f, indent=2)
    print(f"[linkgen] wrote {len(links)} sameAs link(s) → {args.out}")
    for lk in links[:10]:
        e = lk["evidence"]
        print(f"   {e['left_name']!r} ⟷ {e['right_name']!r}  "
              f"({e['distance_m']}m, name {e['name_sim']}, conf {lk['confidence']})")
    if len(links) > 10:
        print(f"   … +{len(links) - 10} more")
    if not links:
        print("[linkgen] no matches — widen --radius-m or lower --min-name-sim.")


def run_keylink():
    """keylink — emit a typed-edge linkset straight from a FOREIGN-KEY field.

    The fuzzy matcher above (`run`) joins two sources that share no id. This is the
    opposite, exact case: source A already stores the id of a node in source B in one
    of its fields (e.g. a tribe Event's `hostBusinessId` literally holds a Google
    `placeId`). No matching needed — for every node we read that field and emit one
    `{from, rel, to}` edge. `rel` is anything but `sameAs` (e.g. `hostedAt`): the View
    turns it into a graph EDGE between the two entities, never a fusion. This is the
    general primitive — any FK on any source becomes a typed cross-source relation.

      from = the node's own local id (already `<ns>:<value>`, e.g. `tribe:10020845`),
             which the View indexes as an addressable endpoint; override with
             --from-namespace + --from-field to build `<ns>:<field>` instead.
      to   = `<to-namespace>:<value of --fk-field>`.

    Example — link tribe events to their Google host business:

        quickbeam data keylink \
          --nodes stage_volumes/volume_4_events.json \
          --fk-field hostBusinessId --to-namespace gplace \
          --rel hostedAt --out host_links.json
    """
    p = argparse.ArgumentParser(
        description="Emit a typed-edge linkset from a foreign-key field (exact join, no matching).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--nodes", required=True, help="Node file holding the foreign key (e.g. volume_4_events.json)")
    p.add_argument("--fk-field", required=True, help="Field whose value is the TARGET node's id (e.g. hostBusinessId)")
    p.add_argument("--to-namespace", required=True, help="Namespace of the target endpoint (e.g. gplace)")
    p.add_argument("--rel", required=True, help="Relation to emit (anything but sameAs → a graph edge, e.g. hostedAt)")
    p.add_argument("--from-field", default=None,
                   help="Build the source endpoint as <from-namespace>:<this field> instead of using the node's "
                        "(already-namespaced) local id")
    p.add_argument("--from-namespace", default=None, help="Namespace for --from-field (required with it)")
    p.add_argument("--out", required=True, help="Output linkset JSON path")
    args = p.parse_args()

    if args.rel == "sameAs":
        raise SystemExit("keylink emits typed EDGES; use `linkgen` for sameAs fusion.")
    if bool(args.from_field) != bool(args.from_namespace):
        raise SystemExit("--from-field and --from-namespace must be given together.")

    with open(args.nodes) as f:
        nodes = json.load(f)

    links, skipped = [], 0
    for n in nodes:
        fields = n.get("fields", {})
        fk = fields.get(args.fk_field)
        if not fk:
            continue  # node has no foreign key → nothing to link
        if args.from_field:
            src = fields.get(args.from_field)
            if not src:
                skipped += 1
                continue
            frm = f"{args.from_namespace}:{src}"
        else:
            frm = n.get("name") or n.get("id")  # already-namespaced local id
            if not frm:
                skipped += 1
                continue
        links.append({"from": frm, "rel": args.rel, "to": f"{args.to_namespace}:{fk}"})

    with open(args.out, "w") as f:
        json.dump(links, f, indent=2)
    print(f"[keylink] wrote {len(links)} {args.rel!r} edge(s) from {args.fk_field!r} → {args.out}"
          + (f" (skipped {skipped} with no source id)" if skipped else ""))
    for lk in links[:10]:
        print(f"   {lk['from']}  --{lk['rel']}-->  {lk['to']}")
    if len(links) > 10:
        print(f"   … +{len(links) - 10} more")
    if not links:
        print(f"[keylink] no nodes carried {args.fk_field!r} — nothing to link.")


if __name__ == "__main__":
    run()

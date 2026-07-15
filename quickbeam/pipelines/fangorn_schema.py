"""
Auto-generate Fangorn schemas + a bundle shape from an extracted graph.

Given the node/edge files produced by a graph pipeline (e.g. `quickbeam data mbpg`
turning a MusicBrainz Postgres DB into Artist/Recording/Work/… nodes + typed
edges), this infers:

  • one Fangorn `SchemaDefinition` per node type   — {field: {"@type": ...}}
  • one bundle shape spanning them                 — {nodes: {Type: schema}, edges: [...]}

…in the exact shapes the Fangorn SDK consumes:

    fangorn.schema.register({ name, definition, agentId: "" })          // resolver
    fangorn.schema.register({ kind: "bundle", name, bundle })           // bundle
    fangorn.publisher.publishBundle({ bundleName, nodes, edges, ... })

This tool only *writes the definitions*; registration/commit is left to the SDK
(`fangorn schema register …`). The output is the relational-DB-→-Fangorn-schema
bridge: point it at any extracted graph and it derives the schemas + bundle.

Field `@type` mapping note: the SDK docs confirm "string" and "handle". "number"
and "boolean" are inferred for non-string scalars; pass --all-strings if your SDK
build only accepts "string"/"handle". Collection fields fall back to ARRAY_TYPE.
The mapping is the TYPE constants below — adjust to match your SDK exactly.
"""
import os
import re
import json
import glob
import argparse
from collections import defaultdict

# --- @type vocabulary (adjust to your SDK's SchemaDefinition types) ----------
T_STRING = "string"
T_NUMBER = "number"
T_BOOL   = "boolean"
ARRAY_TYPE = "string"   # collections → stringified; change if arrays are supported

# Filename stem → node-type name, for datasets whose nodes don't carry an
# `entityType` field (mbpg output does; mb.py track output doesn't).
STEM_TYPE = {
    "artists": "Artist", "releasegroups": "ReleaseGroup", "releases": "Release",
    "recordings": "Recording", "works": "Work",
    "tracks": "Track", "taxonomies": "Taxonomy",
}


# ===========================================================================
# Streaming reader for our JSON-array files (one record per line).
# ===========================================================================
def _iter_array(path: str, limit: int = 0):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        return

    n = 0

    for obj in data:
        if not isinstance(obj, dict):
            continue

        yield obj

        n += 1
        if limit and n >= limit:
            break


# ===========================================================================
# Identity inference  (mirror of the SDK's NodeIdentity, docs/CROSS_PUBLISHER_LINKING)
#
# A node type's `identity` declares how its records expose *global* identity so a
# foreign edge can reference them and two datasources can join on a shared key:
#   { "@id"?: field,  aliases?: { namespace: field } }
# The join contract is the alias *namespace*, never the field name. We detect a
# namespace by value shape — e.g. a field whose values are Google Place IDs
# (`ChIJ…`) is claimed by `gplace` and, since a Place ID is canonical, promoted
# to `@id`. This backfills the existing `ChIJ…` business ids as `gplace:` aliases
# with no hand-authoring.
# ===========================================================================
ALIAS_PATTERNS = {
    "gplace": re.compile(r"^ChIJ[0-9A-Za-z_\-]{10,}$"),   # Google Place ID
    "osm":    re.compile(r"^(node|way|relation)/[0-9]+$"),  # OpenStreetMap element id
    "isrc":   re.compile(r"^[A-Za-z]{2}[A-Za-z0-9]{3}[0-9]{7}$"),  # ISRC
}
# An alias is a FUSION JOIN KEY: a View's union-find merges any two nodes that share
# one. So a field may become an alias ONLY when it is the node's *own* id column (its
# value equals the record's local id) — the "is that place" case. A foreign-key field
# (a Review's `businessId`, an Event's `hostBusinessId`) "points at that place"; it is
# NOT identity and must never become an alias, or every child would collapse onto its
# parent in a View. (The child→parent relationship is carried by an EDGE, not identity.)
# A multi-source type (Business from Google *and* OSM) can still carry both own-id
# aliases — `gplace:placeId` + `osm:osmId` — and gplace wins @id.
PROMOTE_ID = ("gplace", "osm")


def _infer_identity(field_values: dict, id_fields: frozenset = frozenset(),
                    threshold: float = 0.8) -> dict | None:
    """Claim a field for an alias namespace when a strong majority of its sampled
    non-null string values match that namespace's shape, BUT keep only aliases on the
    node's own id column (`id_fields`) — a foreign key that merely references another
    entity must not become a join key. Returns a NodeIdentity dict, or None when the
    node has no own-id alias. The (single) remaining namespace is promoted to `@id`."""
    aliases: dict[str, str] = {}
    for field, vals in field_values.items():
        non_null = [v for v in vals if isinstance(v, str) and v.strip()]
        if not non_null:
            continue
        for ns, pat in ALIAS_PATTERNS.items():
            if ns in aliases:        # first field to claim a namespace wins
                continue
            hits = sum(1 for v in non_null if pat.match(v))
            if hits / len(non_null) >= threshold:
                aliases[ns] = field
    # Drop any alias that isn't the node's own id — a foreign-key alias would make a
    # View's union-find merge every child onto its parent (the over-merge bug).
    aliases = {ns: f for ns, f in aliases.items() if f in id_fields}
    if not aliases:
        return None
    identity: dict = {"aliases": aliases}
    for ns in PROMOTE_ID:
        if ns in aliases:  # all remaining aliases are own-id → any may be @id
            identity["@id"] = aliases[ns]
            break
    return identity


# ===========================================================================
# Type inference
# ===========================================================================
def _infer_type(values, all_strings: bool) -> str:
    if all_strings:
        return T_STRING
    has_str = has_bool = has_num = has_coll = False
    for v in values:
        if v is None:
            continue
        if isinstance(v, (list, dict)):
            has_coll = True
        elif isinstance(v, bool):
            has_bool = True
        elif isinstance(v, (int, float)):
            has_num = True
        else:
            has_str = True
    if has_coll:
        return ARRAY_TYPE
    if has_str:
        return T_STRING
    if has_bool and not has_num:
        return T_BOOL
    if has_num and not has_bool:
        return T_NUMBER
    return T_STRING


def infer_schema(path: str, sample: int, all_strings: bool):
    """Return (type_name, SchemaDefinition, identity|None, count) from a node file."""
    field_values = defaultdict(list)
    # Per-field tally of "this field's value equals the record's own local id" —
    # the signal that a field IS the node's identity (vs. a foreign key). Used to
    # gate @id promotion (see _infer_identity / option (a)).
    id_match: dict[str, int] = defaultdict(int)
    id_total: dict[str, int] = defaultdict(int)
    type_name = None
    count = 0
    for node in _iter_array(path, limit=sample):
        fields = node.get("fields") or {}
        node_id = node.get("name")
        if type_name is None:
            type_name = fields.get("entityType")
        for k, v in fields.items():
            if len(field_values[k]) < sample:
                field_values[k].append(v)
            if isinstance(v, str) and v.strip():
                id_total[k] += 1
                if node_id is not None and v == node_id:
                    id_match[k] += 1
        count += 1
    if type_name is None:
        stem = os.path.basename(path).split("_", 2)[-1].rsplit(".", 1)[0]
        type_name = STEM_TYPE.get(stem, stem.title())
    definition = {k: {"@type": _infer_type(vs, all_strings)}
                  for k, vs in sorted(field_values.items())}
    id_fields = frozenset(k for k, tot in id_total.items()
                          if tot and id_match[k] / tot >= 0.8)
    identity = _infer_identity(field_values, id_fields)
    return type_name, definition, identity, count


# ===========================================================================
# Bundle inference (distinct rel/fromType/toType triples across the edge file)
# ===========================================================================
def infer_bundle_edges(edge_path: str, scan: int = 0):
    triples = {}   # (rel, fromType, toType) -> count
    skipped = 0
    for i, e in enumerate(_iter_array(edge_path, limit=scan)):
        rel, ft, tt = e.get("rel"), e.get("fromType"), e.get("toType")
        if not (rel and ft and tt):
            skipped += 1
            continue
        key = (rel, ft, tt)
        triples[key] = triples.get(key, 0) + 1
        if (i + 1) % 5_000_000 == 0:
            print(f"   …scanned {i + 1:,} edges, {len(triples)} distinct shapes", flush=True)
    if skipped:
        print(f"   ⚠️  {skipped:,} edges lacked fromType/toType and were ignored")
    return triples


# ===========================================================================
# CLI
# ===========================================================================
def parse_args():
    p = argparse.ArgumentParser(
        description="Generate Fangorn schemas + a bundle shape from an extracted node/edge graph.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--input-dir", default="./stage_volumes",
                   help="Directory containing volume_<n>_*.json node files + an edges file.")
    p.add_argument("--volume", type=int, default=1,
                   help="Volume to scan (0 = ALL volumes — merges node types across "
                        "volumes and combines every edges file; use when one bundle "
                        "spans several *_pg shapers, e.g. places + events).")
    p.add_argument("--out-dir", default="./stage_volumes/schemas")
    p.add_argument("--prefix", default="fangorn.mb",
                   help="Schema name prefix → <prefix>.<type>.<version>")
    p.add_argument("--version", default="v1")
    p.add_argument("--bundle-name", default="creativecore",
                   help="Bundle stem → <prefix>.<bundle-name>.<version>")
    p.add_argument("--sample", type=int, default=20000,
                   help="Records to sample per node type for type inference.")
    p.add_argument("--edge-scan", type=int, default=0,
                   help="Edges to scan for bundle shape (0 = all; rare rel types need a full scan).")
    p.add_argument("--all-strings", action="store_true", default=False,
                   help="Declare every field as {\"@type\":\"string\"} (safe if your SDK "
                        "only supports string/handle).")
    return p.parse_args()


def generate_schemas(*, input_dir: str = "./stage_volumes", volume: int = 1,
                     out_dir: str | None = None, prefix: str, version: str = "v1",
                     bundle_name: str, sample: int = 20000, edge_scan: int = 0,
                     all_strings: bool = False) -> dict:
    """Infer Fangorn node schemas + a bundle shape from staged volume files and write
    them to `out_dir` (default `<input_dir>/schemas`, where `fangorn commit --bundle`
    looks by default). This is the callable core of `schemagen` — the CLI `run()` is a
    thin argparse wrapper over it, and `Publisher.onboard()` calls it directly.

    Returns {"bundle_name": <full bundle schema name>, "schemas": [...], "bundle": {...}}
    — the bundle schema name is what `fangorn repo init -s <name>` needs."""
    out_dir = out_dir or os.path.join(input_dir, "schemas")
    os.makedirs(out_dir, exist_ok=True)
    pat = (os.path.join(input_dir, "volume_*_*.json") if volume == 0
           else os.path.join(input_dir, f"volume_{volume}_*.json"))
    files = sorted(glob.glob(pat))
    node_files = [f for f in files if not f.endswith("_edges.json")]
    edge_files = [f for f in files if f.endswith("_edges.json")]
    if not node_files:
        raise SystemExit(f"No node files matched {pat}")

    def schema_name(type_name: str) -> str:
        return f"{prefix}.{type_name.lower()}.{version}"

    # ── Node schemas ────────────────────────────────────────────────────────
    # When scanning all volumes, several files can share a type (e.g. a places
    # `categories` file and an events `event_categories` file both → Category):
    # merge their field definitions into the union so the schema covers both.
    print(f"🔎 Inferring schemas from {len(node_files)} node file(s)...")
    merged: dict[str, dict] = {}
    merged_identity: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for path in node_files:
        type_name, definition, identity, count = infer_schema(path, sample, all_strings)
        merged.setdefault(type_name, {}).update(definition)
        counts[type_name] = counts.get(type_name, 0) + count
        # Union aliases across files sharing a type; first @id wins.
        if identity:
            mi = merged_identity.setdefault(type_name, {"aliases": {}})
            mi["aliases"].update(identity.get("aliases", {}))
            if identity.get("@id") and "@id" not in mi:
                mi["@id"] = identity["@id"]

    schemas, type_to_schema = [], {}
    for type_name, definition in merged.items():
        name = schema_name(type_name)
        type_to_schema[type_name] = name
        entry = {"name": name, "definition": definition}
        identity = merged_identity.get(type_name)
        if identity and identity.get("aliases"):
            entry["identity"] = identity
        schemas.append(entry)
        out = os.path.join(out_dir, f"{name}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(entry, f, indent=2)
        id_note = ""
        if identity and identity.get("aliases"):
            ns = ", ".join(f"{k}:{v}" for k, v in identity["aliases"].items())
            id_note = f"  [identity @id={identity.get('@id', '<node id>')}; aliases {ns}]"
        print(f"   ✅ {type_name:<14} → {name}  ({len(definition)} fields, sampled {counts[type_name]:,}){id_note}")

    # ── Bundle shape ────────────────────────────────────────────────────────
    bundle_edges = []
    if edge_files:
        from collections import Counter
        print(f"\n🔗 Inferring bundle edges from {len(edge_files)} edges file(s)...")
        triples: Counter = Counter()
        for ef in edge_files:
            triples.update(infer_bundle_edges(ef, edge_scan))
        for (rel, ft, tt), n in sorted(triples.items(), key=lambda kv: -kv[1]):
            if ft not in type_to_schema or tt not in type_to_schema:
                print(f"   ⚠️  edge {rel} {ft}→{tt}: endpoint type not among node schemas, skipping")
                continue
            # min 0 keeps publishing permissive (no cardinality rejections);
            # tighten by hand if a relationship is truly required.
            bundle_edges.append({"rel": rel, "from": ft, "to": tt, "min": 0})
            print(f"   ✅ {rel:<24} {ft} → {tt}  ({n:,} observed)")
    else:
        print("   ⚠️  no edges file found — bundle will have nodes only")

    full_bundle_name = f"{prefix}.{bundle_name}.{version}"
    bundle = {
        "name": full_bundle_name,
        "kind": "bundle",
        "bundle": {"nodes": type_to_schema, "edges": bundle_edges},
    }
    with open(os.path.join(out_dir, f"{full_bundle_name}.json"), "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2)

    # ── Consolidated manifest (registration order: node schemas, then bundle) ─
    consolidated = {"schemas": schemas, "bundle": bundle}
    with open(os.path.join(out_dir, "fangorn_schemas.json"), "w", encoding="utf-8") as f:
        json.dump(consolidated, f, indent=2)

    print(f"\n📦 Wrote {len(schemas)} node schema(s) + bundle '{full_bundle_name}' "
          f"({len(bundle_edges)} edge shapes) → {out_dir}/")
    print("   Register with the Fangorn SDK in this order: node schemas first, then the bundle.")
    return {"bundle_name": full_bundle_name, "schemas": schemas, "bundle": bundle}


def run():
    """CLI wrapper — parse argv and delegate to `generate_schemas`."""
    args = parse_args()
    generate_schemas(
        input_dir=args.input_dir, volume=args.volume, out_dir=args.out_dir,
        prefix=args.prefix, version=args.version, bundle_name=args.bundle_name,
        sample=args.sample, edge_scan=args.edge_scan, all_strings=args.all_strings)


if __name__ == "__main__":
    run()

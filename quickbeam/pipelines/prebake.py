"""
prebake.py — embed local volume_<n>_*.json node files straight into Qdrant.

The offline twin of `quickbeam build`. Where `build` walks the on-chain subgraph
+ IPFS to source a published bundle, this embeds node files that already exist on
disk (the output of a *_pg shaper, e.g. events_pg.py / places_pg.py) and upserts
them into a Qdrant collection — no chain, no IPFS, no bundle id. It's how you fold
a freshly-shaped node type into an existing collection for a local demo before
(or instead of) publishing the bundle on-chain.

It reuses the EXACT embedding recipe from embeddings.py — the same fastembed
`nomic-embed-text-v1.5` engine, the `search_document:` prefix, and matryoshka
truncation to the collection's dim — so prebaked vectors are directly comparable
to build-time vectors and to the in-browser query vectors. Point ids are the
deterministic UUIDv5 of the node `name`, so re-running is idempotent (upsert, not
duplicate).

Document text mirrors `_embed_and_upload`: Title + tag fields + the scalar `text`
role blurb + any projected list fields. For an Event that blurb already verbalizes
venue / date / price / organizer / summary (see events_pg.shape_event).

Example:
  quickbeam data prebake --input-dir ./stage_volumes --volume 2 \
      --types Event,Organizer --collection fangorn
  quickbeam cdn bake --domain bars        # re-bake the shard with events folded in
"""
import os
import glob
import json
import argparse

from qdrant_client import QdrantClient, models

from quickbeam.embeddings import (
    _init_embed_engine, matryoshka, ensure_indexes, _str_to_uuid,
)


# Default text-composition role map (matches the auto-inferred places role map).
# `text` carries the rich human blurb each shaper builds; `tags` are list fields.
DEFAULT_ROLE_MAP = {
    "title": "title",
    "subtitle": "",
    "tags": ["categories"],
    "text": ["text"],
}


def _load_role_map(path: str) -> dict:
    if path and os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_ROLE_MAP


# ---------------------------------------------------------------------------
# DOCUMENT ENRICHMENT
#
# Raw shaper text is too thin for good semantic ranking: a real lakeside
# restaurant reads "Joe's — Restaurant in Vilas County" (no spatial or dining
# words), while Airbnb-style *lodging* listings literally carry "Lakefront",
# "Waterfront", "on Found Lake" in their titles. So "lakeside dining" embeds
# closer to cabins than to restaurants. We fix this at the document side — far
# more robust than a brittle query-side classifier — by appending:
#   1. category synonyms that DISAMBIGUATE dining vs. lodging, and
#   2. a "lakefront / near the lake" tag computed from each place's distance to
#      the nearest Lake node, so actual waterfront restaurants can compete on the
#      "lakeside" axis the lodging titles currently monopolize.
# ---------------------------------------------------------------------------
# Substrings tested against a Business's lowercased primaryType.
_DINING_HINTS = ("restaurant", "bar", "pub", "cafe", "coffee", "fast food",
                 "supper club", "grill", "barbecue", "bbq", "diner", "bistro",
                 "brewery", "winery", "ice cream", "bakery", "food", "tavern",
                 "steak", "pizza", "deli", "eatery")
_LODGING_HINTS = ("lodging", "hotel", "motel", "resort", "inn", "camp", "cabin",
                  "guest house", "hostel", "chalet", "apartment", "cottage",
                  "vacation", "travel agency", "real estate")
_DINING_SYN = ("Dining, restaurant, food, dinner, lunch, where to eat out, "
               "grab a meal or drinks.")
_LODGING_SYN = ("Lodging, a place to stay, vacation rental, cabin, overnight "
                "accommodation.")
# Distance bands (metres) from the nearest lake → spatial phrasing.
_LAKE_ON_M = 200
_LAKE_NEAR_M = 800


def _parse_latlon(v) -> tuple[float, float] | None:
    if not isinstance(v, str) or "," not in v:
        return None
    try:
        a, b = v.split(",")
        return float(a), float(b)
    except ValueError:
        return None


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math
    r = 6_371_000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _load_lake_coords(input_dir: str) -> list[tuple[float, float]]:
    """Every Lake node's coordinates across all volumes — the anchors for the
    lakefront-proximity tag. Source-agnostic (scans by entityType, not filename)."""
    coords: list[tuple[float, float]] = []
    for path in sorted(glob.glob(os.path.join(input_dir, "volume_*_*.json"))):
        if path.endswith("_edges.json"):
            continue
        try:
            recs = json.load(open(path, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for rec in recs:
            f = rec.get("fields", {}) or {}
            if f.get("entityType") == "Lake":
                c = _parse_latlon(f.get("coordinates"))
                if c:
                    coords.append(c)
    return coords


def _enrich_text(fields: dict, entity_type: str, lakes: list[tuple[float, float]]) -> str:
    """Extra description folded into the embedding document (not shown to users)."""
    extra: list[str] = []
    # Category synonyms (Business only — keyed on its primaryType).
    if entity_type == "Business":
        pt = str(fields.get("primaryType") or "").lower()
        if any(h in pt for h in _DINING_HINTS):
            extra.append(_DINING_SYN)
        elif any(h in pt for h in _LODGING_HINTS):
            extra.append(_LODGING_SYN)
    # Lakefront proximity (any located thing except a Lake itself).
    if entity_type != "Lake" and lakes:
        c = _parse_latlon(fields.get("coordinates"))
        if c:
            d = min(_haversine_m(c, L) for L in lakes)
            if d <= _LAKE_ON_M:
                extra.append("On the lakefront, right on the water, lakeside with lake views.")
            elif d <= _LAKE_NEAR_M:
                extra.append("Near the lake, close to the water.")
    return " ".join(extra)


def _compose_text(fields: dict, role_map: dict, extra: str = "") -> str:
    """Reproduce embeddings._embed_and_upload's document composition."""
    tags = " ".join(
        fields.get(t, "") if isinstance(fields.get(t), str) else
        (", ".join(str(x) for x in fields.get(t)) if isinstance(fields.get(t), list) else "")
        for t in role_map.get("tags", []) or []
    )
    rels = "; ".join(
        f"{k}: {', '.join(str(x) for x in v[:20] if x)}"
        for k, v in fields.items()
        if isinstance(v, list) and v and k != "entityType"
    )
    subtitle = fields.get(role_map.get("subtitle", ""), "")
    text_terms = "; ".join(
        str(fields[t]) for t in (role_map.get("text", []) or []) if fields.get(t)
    )
    s = f"Title: {fields.get(role_map.get('title', 'title'), '')}. Tags: {tags}"
    if subtitle:
        s += f". Subtitle: {subtitle}"
    if text_terms:
        s += f". {text_terms}"
    if rels:
        s += f". {rels}"
    if extra:
        s += f". {extra}"
    return f"search_document: {s[:1000]}"


def _load_nodes(input_dir: str, volume: int, types: set[str] | None) -> list[dict]:
    pat = os.path.join(input_dir, f"volume_{volume}_*.json")
    records = []
    for path in sorted(glob.glob(pat)):
        if path.endswith("_edges.json"):
            continue
        for rec in json.load(open(path, encoding="utf-8")):
            fields = rec.get("fields", {}) or {}
            et = fields.get("entityType")
            if types and et not in types:
                continue
            name = rec.get("name")
            if not name:
                continue
            records.append({"track_id": str(name), "entity_type": et, "fields": fields})
    return records


def _link_events(qdrant: QdrantClient, collection: str, input_dir: str, volume: int,
                 records: list[dict]) -> int:
    """Fold each Business's hosted events into its payload as `fields.events` so the
    UI's connections rail (which renders list fields, not edges) shows a bar's
    events. Payload-only update — the existing Business *vectors* are untouched, so
    we never degrade the richly-projected build-time documents."""
    edges_path = os.path.join(input_dir, f"volume_{volume}_edges.json")
    if not os.path.exists(edges_path):
        return 0
    title = {r["track_id"]: r["fields"].get("title") for r in records}
    by_biz: dict[str, list[str]] = {}
    for e in json.load(open(edges_path, encoding="utf-8")):
        if e.get("rel") == "hostsEvent":
            t = title.get(e.get("to"))
            if t:
                by_biz.setdefault(e["from"], []).append(t)
    linked = 0
    for biz, titles in by_biz.items():
        pid = _str_to_uuid(biz)
        got = qdrant.retrieve(collection, ids=[pid], with_payload=True)
        if not got:
            continue
        payload = got[0].payload or {}
        fields = dict(payload.get("fields", {}) or {})
        fields["events"] = sorted(set(titles))
        qdrant.set_payload(collection, payload={"fields": fields}, points=[pid])
        linked += 1
    return linked


def _collection_dim(qdrant: QdrantClient, collection: str) -> int | None:
    try:
        info = qdrant.get_collection(collection)
        v = info.config.params.vectors
        return getattr(v, "size", None)
    except Exception:
        return None


def parse_args():
    p = argparse.ArgumentParser(
        description="Embed local volume node files into Qdrant (offline build).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--input-dir", default="./stage_volumes")
    p.add_argument("--volume", type=int, default=2)
    p.add_argument("--types", default="",
                   help="Comma-separated entityTypes to embed (default: all in the volume).")
    p.add_argument("--collection", default="fangorn")
    p.add_argument("--owner", default=None, help="owner payload value (default: null).")
    p.add_argument("--embedding-model", default="nomic-ai/nomic-embed-text-v1.5")
    p.add_argument("--dim", type=int, default=0, help="Truncate dim (0 = match the collection).")
    p.add_argument("--embed-batch", type=int, default=16)
    p.add_argument("--role-map-file", default="./db/role_map.json")
    p.add_argument("--link-events", action="store_true", default=False,
                   help="Fold hosted-event titles into each Business payload "
                        "(fields.events) via a payload-only update (vectors untouched).")
    p.add_argument("--qdrant-host", default="localhost")
    p.add_argument("--qdrant-port", type=int, default=6333)
    p.add_argument("--qdrant-grpc-port", type=int, default=6334)
    p.add_argument("--qdrant-url", default=None)
    p.add_argument("--qdrant-api-key", default=None)
    return p.parse_args()


def run():
    args = parse_args()
    types = {t.strip() for t in args.types.split(",") if t.strip()} or None

    if args.qdrant_url:
        qdrant = QdrantClient(url=args.qdrant_url, api_key=args.qdrant_api_key, timeout=120)
    else:
        qdrant = QdrantClient(host=args.qdrant_host, port=args.qdrant_port,
                              grpc_port=args.qdrant_grpc_port, prefer_grpc=True, timeout=120)

    dim = args.dim or _collection_dim(qdrant, args.collection) or 256
    role_map = _load_role_map(args.role_map_file)
    records = _load_nodes(args.input_dir, args.volume, types)
    if not records:
        raise SystemExit(f"No matching nodes in volume {args.volume} "
                         f"of {args.input_dir} (types={types or 'all'}).")
    by_type: dict[str, int] = {}
    for r in records:
        by_type[r["entity_type"]] = by_type.get(r["entity_type"], 0) + 1
    print(f"📦 {len(records)} node(s) to embed → {args.collection} (dim={dim})")
    for t, n in sorted(by_type.items()):
        print(f"   {t:<12} {n}")

    ensure_indexes(qdrant, args.collection)
    engine = _init_embed_engine(args)

    # Lake anchors for the lakefront-proximity tag (loaded once across volumes).
    lakes = _load_lake_coords(args.input_dir)
    if lakes:
        print(f"🌊 lakefront tagging against {len(lakes):,} lake anchors")
    texts = [
        _compose_text(r["fields"], role_map,
                      _enrich_text(r["fields"], r["entity_type"], lakes))
        for r in records
    ]
    print("🧮 Embedding...")
    vectors = [matryoshka(v, dim) for v in engine.embed(texts, batch_size=args.embed_batch)]

    qdrant.upload_points(
        collection_name=args.collection,
        points=[
            models.PointStruct(
                id=_str_to_uuid(r["track_id"]),
                vector=vec,
                payload={
                    "id": r["track_id"],
                    "entityType": r["entity_type"],
                    "owner": args.owner,
                    "fields": r["fields"],
                    "meta": {"manifestCid": "local-prebake"},
                },
            )
            for vec, r in zip(vectors, records)
        ],
        wait=True,
    )
    print(f"✅ Upserted {len(records)} point(s) into {args.collection}.")

    if args.link_events:
        n = _link_events(qdrant, args.collection, args.input_dir, args.volume, records)
        print(f"🔗 Folded events into {n} Business payload(s) (fields.events).")

    print("Next: quickbeam cdn bake --domain <name>")


if __name__ == "__main__":
    run()

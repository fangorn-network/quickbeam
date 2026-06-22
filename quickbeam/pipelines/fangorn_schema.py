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
    """Yield objects from a `[ {..},\\n {..} ]` file without loading it whole."""
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s in ("[", "]"):
                continue
            if s.endswith(","):
                s = s[:-1]
            if not s:
                continue
            try:
                yield json.loads(s)
            except json.JSONDecodeError:
                continue
            n += 1
            if limit and n >= limit:
                return


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
    """Return (type_name, SchemaDefinition) inferred from a node file."""
    field_values = defaultdict(list)
    type_name = None
    count = 0
    for node in _iter_array(path, limit=sample):
        fields = node.get("fields") or {}
        if type_name is None:
            type_name = fields.get("entityType")
        for k, v in fields.items():
            if len(field_values[k]) < sample:
                field_values[k].append(v)
        count += 1
    if type_name is None:
        stem = os.path.basename(path).split("_", 2)[-1].rsplit(".", 1)[0]
        type_name = STEM_TYPE.get(stem, stem.title())
    definition = {k: {"@type": _infer_type(vs, all_strings)}
                  for k, vs in sorted(field_values.items())}
    return type_name, definition, count


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
    p.add_argument("--volume", type=int, default=1)
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


def run():
    args = parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    pat = os.path.join(args.input_dir, f"volume_{args.volume}_*.json")
    files = sorted(glob.glob(pat))
    node_files = [f for f in files if not f.endswith("_edges.json")]
    edge_files = [f for f in files if f.endswith("_edges.json")]
    if not node_files:
        raise SystemExit(f"No node files matched {pat}")

    def schema_name(type_name: str) -> str:
        return f"{args.prefix}.{type_name.lower()}.{args.version}"

    # ── Node schemas ────────────────────────────────────────────────────────
    print(f"🔎 Inferring schemas from {len(node_files)} node file(s)...")
    schemas, type_to_schema = [], {}
    for path in node_files:
        type_name, definition, count = infer_schema(path, args.sample, args.all_strings)
        name = schema_name(type_name)
        type_to_schema[type_name] = name
        schemas.append({"name": name, "definition": definition})
        out = os.path.join(args.out_dir, f"{name}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"name": name, "definition": definition}, f, indent=2)
        print(f"   ✅ {type_name:<14} → {name}  ({len(definition)} fields, sampled {count:,})")

    # ── Bundle shape ────────────────────────────────────────────────────────
    bundle_edges = []
    if edge_files:
        print(f"\n🔗 Inferring bundle edges from {os.path.basename(edge_files[0])}...")
        triples = infer_bundle_edges(edge_files[0], args.edge_scan)
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

    bundle_name = f"{args.prefix}.{args.bundle_name}.{args.version}"
    bundle = {
        "name": bundle_name,
        "kind": "bundle",
        "bundle": {"nodes": type_to_schema, "edges": bundle_edges},
    }
    with open(os.path.join(args.out_dir, f"{bundle_name}.json"), "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=2)

    # ── Consolidated manifest (registration order: node schemas, then bundle) ─
    consolidated = {"schemas": schemas, "bundle": bundle}
    with open(os.path.join(args.out_dir, "fangorn_schemas.json"), "w", encoding="utf-8") as f:
        json.dump(consolidated, f, indent=2)

    print(f"\n📦 Wrote {len(schemas)} node schema(s) + bundle '{bundle_name}' "
          f"({len(bundle_edges)} edge shapes) → {args.out_dir}/")
    print("   Register with the Fangorn SDK in this order: node schemas first, then the bundle.")


if __name__ == "__main__":
    run()

import certifi
import os
import io
import sys
import argparse
import asyncio
import aiohttp
import hashlib
import json
import uuid

import numpy as np
from qdrant_client import QdrantClient
from qdrant_client import models
from fastembed import TextEmbedding
from quickbeam.roles import infer_roles
from tqdm import tqdm

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

os.environ['SSL_CERT_FILE'] = certifi.where()

# ---------------------------------------------------------------------------
# CONFIG & ARGS
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(description="Fangorn Qdrant Ingestion CLI Builder")
    parser.add_argument("--schema", "-s", action="append", dest="schemas", default=[])
    parser.add_argument("--primary", "-p", default=None)
    parser.add_argument("--bundle", default=None,
                        help="Bundle schema as name=schemaId. Uses edge-walk join instead of --primary track-id join.")
    parser.add_argument("--view", default=None,
                        help="Composed View as name=schemaId. Fuses the view's source datasources "
                             "into one graph (joins on Entity URI + aliases) before projecting.")
    parser.add_argument(
        "--root-profile",
        action="append",
        default=[],
        help="Named projection(s) to emit, repeatable: e.g. --root-profile track "
             "--root-profile place. Each profile walks the graph from a root type "
             "and emits a distinct document (see ROOT_PROFILES). If omitted, falls "
             "back to a single --root-type one-hop projection (legacy behavior).",
    )
    parser.add_argument("--root-type", default="Track",
                        help="Legacy single-projection root node type (used only when "
                             "no --root-profile is given).")
    parser.add_argument("--profiles-file", default=None,
                        help="Optional JSON file of custom/override root profiles, "
                             "merged over the built-in ROOT_PROFILES.")
    parser.add_argument("--max-depth", type=int, default=2,
                        help="Default traversal depth for profiles that don't set one.")
    parser.add_argument("--label-cap", type=int, default=50,
                        help="Max neighbor labels collected per relation group in a projection.")
    parser.add_argument("--node-cap", type=int, default=2000,
                        help="Max nodes a single root's graph walk will visit (cost bound).")
    parser.add_argument("--subgraph-url", default="https://gateway.thegraph.com/api/subgraphs/id/8SgbhtiitpAhEfyTgeAHxHH5DQ2gTygUuXgc3b7MCFyc")
    parser.add_argument("--graph-api-key", default="")
    parser.add_argument("--ipfs-gateway", default="https://gateway.pinata.cloud/ipfs")
    parser.add_argument("--ipfs-gateway-key", default=None)
    parser.add_argument("--qdrant-host", default="localhost")
    parser.add_argument("--qdrant-port", type=int, default=6333)
    parser.add_argument("--qdrant-grpc-port", type=int, default=6334)
    parser.add_argument("--checkpoint-file", default="./db/ingest_checkpoint.json")
    parser.add_argument("--collection", default="fangorn")
    parser.add_argument("--searchable-fields", default="auto")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--ipfs-timeout", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--reset", action="store_true", default=False)
    parser.add_argument("--embedding-model", default="nomic-ai/nomic-embed-text-v1.5")
    parser.add_argument("--dim", type=int, default=256, help="Matryoshka output dim: 256, 512, or 768")
    parser.add_argument("--embed-batch", type=int, default=16)
    parser.add_argument("--role-map-file", default="./db/role_map.json")
    parser.add_argument("--umap", action="store_true", default=False)
    parser.add_argument("--umap-only", action="store_true", default=False)
    parser.add_argument("--umap-tmp-dir", default="./db/umap_tmp",
                        help="Directory for UMAP temp memmaps. MUST be on real disk — "
                             "the system temp dir is often tmpfs (RAM), where the ~10GB "
                             "vector array would exhaust memory.")
    parser.add_argument("--umap-neighbors", type=int, default=15)
    parser.add_argument("--umap-min-dist", type=float, default=0.05)
    parser.add_argument("--umap-writeback-batch", type=int, default=1000,
                        help="Points per Qdrant payload-update call during UMAP write-back. "
                             "Lower it if Qdrant balloons RAM on a small machine.")
    parser.add_argument("--umap-writeback-sleep", type=float, default=0.0,
                        help="Seconds to pause between write-back batches (synchronous mode "
                             "only, i.e. --umap-writeback-workers 1). Throttles Qdrant on "
                             "tiny machines.")
    parser.add_argument("--umap-writeback-workers", type=int, default=4,
                        help="Concurrent connections applying UMAP px/py updates. >1 multiplies "
                             "write-back throughput ~Nx. wait=True per call still bounds Qdrant "
                             "memory. Set 1 for the old synchronous (throttle-able) path.")
    parser.add_argument("--umap-writeback-wait", default=True,
                        action=argparse.BooleanOptionalAction,
                        help="If true (default), each write waits for Qdrant to apply it — "
                             "safe but disk-flush bound. Pass --no-umap-writeback-wait for a "
                             "big speedup (async WAL writes + one final flush); ONLY with a "
                             "Qdrant memory cap, or the apply-queue can OOM the box.")
    parser.add_argument("--umap-target", choices=["file", "payload"], default="file",
                        help="Where to put the UMAP projection. 'file' (default) writes a "
                             "catalog-map artifact the server streams from /catalog/map — one "
                             "fast read pass, no Qdrant writes (the only path that scales to "
                             "millions). 'payload' writes px/py into each Qdrant point (slow; "
                             "needed only if you must bake coords into the Qdrant snapshot).")
    parser.add_argument("--umap-map-file", default="./db/catalog_map.json.gz",
                        help="Output path for the --umap-target file artifact (gzipped JSON).")
    parser.add_argument("--max-manifests", type=int, default=0,
                        help="Build at most N bundle manifests (shards) this run, then stop. "
                             "0 = no limit. Progress is checkpointed, so a later run resumes "
                             "with the next un-built shards. Use this to build a small number of "
                             "shards on a memory-limited machine.")
    return parser.parse_args()

MODEL_DIM_MAP = {
    "nomic-ai/nomic-embed-text-v1.5": 768,
    "BAAI/bge-small-en-v1.5": 384,
    "sentence-transformers/all-MiniLM-L6-v2": 384
}

# ---------------------------------------------------------------------------
# SHARED HELPERS
# ---------------------------------------------------------------------------
def _load_checkpoint(path):
    try:
        with open(path) as f:
            ck = json.load(f)
            # Legacy format: top-level keys were manifest cids
            if "manifests" not in ck:
                ck = {"manifests": ck, "processed_track_ids": []}
            ck.setdefault("completed_manifest_cids", [])
            ck.setdefault("processed_track_ids", [])
            return ck
    except Exception:
        return {"manifests": {}, "processed_track_ids": [], "completed_manifest_cids": []}


def _save_checkpoint(ck, path):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as f:
        json.dump(ck, f)


def _str_to_uuid(s: str) -> str:
    """Deterministic UUID v5 from a track id. Using a stable id (rather than a
    random one) makes re-upserting a manifest idempotent — a crash that re-runs
    an already-embedded manifest overwrites the same points instead of creating
    duplicates. Matches server.py's _str_to_uuid so builder + bundle-import agree.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, s))


def _save_role_map(role_map, path):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as f:
        json.dump(role_map, f)


def _build_text_embedding(args, cpu_only: bool = False):
    """Construct a raw fastembed TextEmbedding, first clearing any corrupted
    model cache snapshots.

    `arena_extend_strategy=kSameAsRequested` (not kNextPowerOfTwo) is deliberate:
    on small/laptop GPUs the power-of-two strategy rounds every allocation up to
    the next power of two, wasting VRAM and fragmenting the arena over a long run
    until a mid-size allocation can no longer fit (the BFCArena "Available memory
    of 0" OOM). Requesting exactly what's needed keeps the arena dense.

    `cpu_only=True` builds a CPU-only session — used as the last-resort fallback
    when the GPU is exhausted (see ResilientEmbedder).
    """
    import glob, shutil, tempfile
    cache_root = os.environ.get("FASTEMBED_CACHE_PATH", os.path.join(tempfile.gettempdir(), "fastembed_cache"))
    slug = args.embedding_model.replace("/", "--")
    for snap in glob.glob(os.path.join(cache_root, f"models--{slug}", "snapshots", "*")):
        if os.path.isdir(snap) and not os.path.isfile(os.path.join(snap, "onnx", "model.onnx")):
            print(f"[Builder] Corrupt model cache at {snap!r}, removing for re-download...")
            shutil.rmtree(snap)
    providers = ["CPUExecutionProvider"] if cpu_only else [
        ("CUDAExecutionProvider", {
            "device_id": 0,
            "arena_extend_strategy": "kSameAsRequested",
            "gpu_mem_limit": 3 * 1024 * 1024 * 1024,
            "cudnn_conv_algo_search": "DEFAULT",
        }),
        "CPUExecutionProvider",
    ]
    return TextEmbedding(model_name=args.embedding_model, max_length=256, providers=providers)


_OOM_SIGNATURES = ("available memory", "out of memory", "bfcarena",
                   "bfc_arena", "cudaerrormemoryallocation", "cublas_status_alloc_failed")


def _is_gpu_oom(exc: Exception) -> bool:
    return any(sig in str(exc).lower() for sig in _OOM_SIGNATURES)


class ResilientEmbedder:
    """Wraps a fastembed TextEmbedding so a GPU out-of-memory during embedding is
    recoverable instead of fatal.

    Recovery strategy (in order):
      1. Retry the same texts at progressively smaller batch sizes on the SAME
         GPU session. A failed onnxruntime run rolls its allocations back, so the
         session stays usable and a smaller batch often fits in the arena's freed
         space. (We do NOT rebuild the session: onnxruntime does not release the
         CUDA arena on `del`, so a rebuild's own initialization OOMs — making
         things worse.)
      2. If even batch_size=1 OOMs, the arena is exhausted and cannot be
         reclaimed in-process. Mark the GPU dead and fall back to a CPU session
         for the rest of the run — slow, but it never OOMs, so the build always
         finishes. The deterministic-id checkpoint means stopping and restarting
         with a smaller --embed-batch resumes on GPU with no lost or duplicated work.

    Exposes the same `.embed(texts, batch_size=...)` surface as TextEmbedding.
    """

    def __init__(self, args):
        self.args        = args
        self.engine      = _build_text_embedding(args)
        self._cpu_engine = None
        self._gpu_dead   = False

    def _cpu(self):
        if self._cpu_engine is None:
            print("[Builder] Building CPU fallback embedder (one-time)...")
            self._cpu_engine = _build_text_embedding(self.args, cpu_only=True)
        return self._cpu_engine

    def embed(self, texts, batch_size: int = 16):
        if not self._gpu_dead:
            sizes, bs = [], max(1, batch_size)
            while bs > 1:
                sizes.append(bs); bs //= 2
            sizes.append(1)
            for bs in sizes:
                try:
                    # Materialise (not lazy) so a retry can re-run the same texts.
                    return list(self.engine.embed(texts, batch_size=bs))
                except Exception as exc:  # noqa: BLE001
                    if not _is_gpu_oom(exc):
                        raise
                    if bs > 1:
                        print(f"[Builder] GPU OOM at batch_size={bs} — retrying at {bs // 2}...")
            # batch_size=1 still OOM'd → GPU is unrecoverable in this process.
            self._gpu_dead = True
            print("[Builder] GPU OOM persists at batch_size=1; the CUDA arena can't be "
                  "reclaimed in-process — falling back to CPU for the rest of this run.")
            print("[Builder] TIP: stop (Ctrl-C) and restart with a smaller --embed-batch "
                  "(e.g. 4) to run on GPU again; the checkpoint resumes where it left off.")
        return list(self._cpu().embed(texts, batch_size=max(1, batch_size)))


def _init_embed_engine(args):
    """Init the embedder used by build + watch. Returns a ResilientEmbedder that
    transparently recovers from GPU OOM (see class docs)."""
    return ResilientEmbedder(args)

# ---------------------------------------------------------------------------
# MATRYOSHKA TRUNCATION
# ---------------------------------------------------------------------------
def matryoshka(vec, dim):
    x = np.asarray(vec, dtype=np.float32)
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-5)
    x = x[:dim]
    n = np.linalg.norm(x)
    return (x / n).astype(np.float32).tolist() if n else x.tolist()

# ---------------------------------------------------------------------------
# PAYLOAD INDEXES
# ---------------------------------------------------------------------------
def ensure_indexes(qdrant, collection):
    specs = [
        ("fields.title",    models.TextIndexParams(type="text", tokenizer=models.TokenizerType.WORD, lowercase=True)),
        ("fields.byArtist", models.TextIndexParams(type="text", tokenizer=models.TokenizerType.WORD, lowercase=True)),
        ("owner",           models.KeywordIndexParams(type="keyword")),
        ("entityType",      models.KeywordIndexParams(type="keyword")),
        # Structured filters for hybrid search (Business records). Harmless for
        # other entity types — the fields are simply absent, so the index stays
        # empty and never matches.
        ("fields.rating",     models.FloatIndexParams(type="float")),
        ("fields.priceLevel", models.KeywordIndexParams(type="keyword")),
        ("fields.amenities",  models.KeywordIndexParams(type="keyword")),
        ("fields.categories", models.KeywordIndexParams(type="keyword")),
        ("fields.locality",   models.KeywordIndexParams(type="keyword")),
        # Event records (merged in via events_pg): browse upcoming/past + by source,
        # and look up the events a given Business hosts (fields.hostBusinessId).
        ("fields.source",         models.KeywordIndexParams(type="keyword")),
        ("fields.isPast",         models.BoolIndexParams(type="bool")),
        ("fields.hostBusinessId", models.KeywordIndexParams(type="keyword")),
    ]
    for field, schema in specs:
        try:
            qdrant.create_payload_index(collection_name=collection, field_name=field, field_schema=schema)
            print(f"[index] created {field}")
        except Exception as e:
            print(f"[index] {field} already present ({type(e).__name__})")

# ---------------------------------------------------------------------------
# UMAP PRECOMPUTE
# ---------------------------------------------------------------------------
def _shape_map_track(fields: dict, doc_id: str, px: float, py: float, role_map: dict):
    """Project a record's fields into the catalog-map track shape using the role
    map. Returns (track_dict, primary_tags) — mirrors server.py's map shaping so
    the file artifact and the recompute path produce identical output."""
    title_f = role_map.get("title")
    sub_f   = role_map.get("subtitle")
    tags    = role_map.get("tags", []) or []

    def _vals(f):
        v = fields.get(f) if f else None
        if isinstance(v, list):
            return [str(x) for x in v if x]
        return [str(v)] if v else []

    primary   = _vals(tags[0]) if len(tags) > 0 else []
    secondary = _vals(tags[1]) if len(tags) > 1 else []
    track = {
        "id":     doc_id,
        "title":  str(fields.get(title_f) or doc_id) if title_f else doc_id,
        "artist": str(fields.get(sub_f) or "")       if sub_f   else "",
        "genres": primary[:3],
        "moods":  secondary[:3],
        "px":     px,
        "py":     py,
    }
    return track, primary


def write_umap_coords(qdrant, collection, neighbors, min_dist, tmp_dir=None, reconnect=None,
                      writeback_batch=1000, writeback_sleep=0.0, writeback_workers=4,
                      writeback_wait=True, target="file", map_file="./db/catalog_map.json.gz",
                      role_map=None):
    """Project the whole collection to 2D and write px/py back onto each point.

    Built to survive a multi-hour run on a flaky laptop against 10M × 256-d:

      • Memory-bounded. Temp arrays are memmapped onto REAL DISK (not the system
        temp dir, which is commonly tmpfs/RAM — a 10M×256 float32 array is ~10GB
        and would brick the machine). Point ids are never held in RAM; write-back
        re-scrolls in deterministic id order and aligns rows by position. Peak RAM
        is ~one scroll page + the fit sample + the tiny n×2 projection.

      • Transient-fault tolerant. Every Qdrant call is retried with backoff, and
        the client is rebuilt via `reconnect()` on a dropped connection
        ("Stream removed (Socket closed)", UNAVAILABLE, etc.).

      • Resumable. Progress is checkpointed to umap_state.json in tmp_dir across
        three stages (pull → transform → writeback). A crash resumes from the
        last completed step instead of repeating the ~1h pull/transform. The fit
        is deterministic (fixed seeds), so a re-fit on resume reproduces the exact
        same projection. Delete tmp_dir to force a clean re-projection.
    """
    try:
        import umap
    except ImportError:
        print("[umap] umap-learn not installed. Run: pip install umap-learn")
        return
    import numpy as np
    from tqdm import tqdm
    import gc, os, json, time

    tmp_dir = tmp_dir or os.path.join("db", "umap_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    arr_path   = os.path.join(tmp_dir, "umap_vectors.f32")
    proj_path  = os.path.join(tmp_dir, "umap_proj.f32")
    state_path = os.path.join(tmp_dir, "umap_state.json")

    # ── Qdrant call wrapper: retry transient gRPC faults, reconnect if possible.
    client = [qdrant]
    _TRANSIENT = ("unavailable", "socket closed", "stream removed", "deadline exceeded",
                  "connection reset", "broken pipe", "timed out", "transport is closing")

    def _retry(fn, what):
        delay = 1.0
        for attempt in range(8):
            try:
                return fn(client[0])
            except Exception as exc:  # noqa: BLE001
                if attempt == 7 or not any(t in str(exc).lower() for t in _TRANSIENT):
                    raise
                print(f"\n[umap] {what}: transient error ({str(exc)[:80]}); "
                      f"reconnect+retry {attempt + 1}/8 in {delay:.0f}s")
                time.sleep(delay)
                delay = min(30.0, delay * 2)
                if reconnect:
                    try:
                        client[0] = reconnect()
                    except Exception as rexc:  # noqa: BLE001
                        print(f"[umap] reconnect failed: {rexc}")

    def _save_state(st):
        tmp = state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, state_path)  # atomic

    def _load_state():
        try:
            with open(state_path) as f:
                return json.load(f)
        except Exception:
            return None

    st = _load_state()
    resuming = bool(st and st.get("collection") == collection and os.path.exists(proj_path))
    if resuming:
        # The output stage follows the CURRENT --umap-target, not whatever a prior
        # run used. So a projection that finished transform under the old payload
        # path can be redirected to the fast file artifact on resume (and vice versa).
        if st.get("stage") in ("writeback", "artifact"):
            st["stage"] = "artifact" if target == "file" else "writeback"
        print(f"[umap] resuming: stage={st['stage']} n={st['n']} (tmp dir: {tmp_dir})")
    else:
        st = None

    # ── STAGE 1: PULL all vectors → on-disk memmap (not resumable; if interrupted
    #    here, the cheaper pull simply restarts). Skipped entirely when resuming.
    if not resuming:
        total = _retry(lambda c: c.count(collection).count, "count")
        print(f"[umap] projecting {total} vectors (temp dir on disk: {tmp_dir}) ...")
        probe = _retry(lambda c: c.scroll(collection, limit=1, with_vectors=True, with_payload=False), "probe")[0]
        if not probe:
            print("[umap] collection empty, nothing to project")
            return
        dims = len(probe[0].vector)
        arr = np.memmap(arr_path, dtype=np.float32, mode="w+", shape=(max(total, 1), dims))
        cursor, offset = 0, None
        pbar = tqdm(total=total, desc="  ↳ pulling vectors", unit=" vec")
        while True:
            pts, offset = _retry(
                lambda c, o=offset: c.scroll(collection, limit=2000, offset=o,
                                             with_vectors=True, with_payload=False),
                "scroll-pull")
            if not pts:
                break
            for p in pts:
                if cursor >= total:
                    break
                arr[cursor] = p.vector if p.vector is not None else 0.0
                cursor += 1
            pbar.update(len(pts))
            if offset is None:
                break
        pbar.close()
        arr.flush()
        n = cursor
        if n == 0:
            del arr; os.unlink(arr_path)
            print("[umap] no vectors pulled, nothing to project")
            return
        st = {"collection": collection, "n": n, "dims": dims,
              "stage": "transform", "transform_row": 0}
        _save_state(st)
    else:
        n, dims = st["n"], st["dims"]

    # ── STAGE 2: TRANSFORM (resumable per TBATCH). Fit is deterministic, so a
    #    re-fit on resume yields the identical mapping for already-done rows.
    if st["stage"] == "transform":
        arr  = np.memmap(arr_path, dtype=np.float32, mode="r", shape=(n, dims))
        proj = np.memmap(proj_path, dtype=np.float32,
                         mode=("r+" if (resuming and os.path.exists(proj_path)) else "w+"),
                         shape=(n, 2))
        sample_size = min(30_000, n)
        rng = np.random.default_rng(42)
        idx = np.unique(rng.integers(0, n, size=sample_size))
        print(f"[umap] fitting UMAP on {len(idx)}-vector sample ...")
        reducer = umap.UMAP(
            n_components=2, n_neighbors=min(neighbors, max(2, len(idx) - 1)),
            min_dist=min_dist, metric="cosine", low_memory=True,
            random_state=42, verbose=True,
        )
        reducer.fit(np.array(arr[idx], dtype=np.float32))

        TBATCH = 10_000
        start = st.get("transform_row", 0)
        for i in tqdm(range(start, n, TBATCH), desc="  ↳ transforming", initial=start // TBATCH,
                      total=(n + TBATCH - 1) // TBATCH):
            end = min(i + TBATCH, n)
            proj[i:end] = reducer.transform(np.array(arr[i:end], dtype=np.float32))
            st["transform_row"] = end
            _save_state(st)
            gc.collect()
        proj.flush()

        # Normalize to [-1, 1] once, at the transform→writeback boundary.
        for ax in range(2):
            mn, mx = float(proj[:, ax].min()), float(proj[:, ax].max())
            rng_ax = (mx - mn) or 1.0
            proj[:, ax] = (proj[:, ax] - mn) / rng_ax * 2 - 1
        proj.flush()
        del arr, proj
        gc.collect()
        try:
            os.unlink(arr_path)  # vectors no longer needed
        except OSError:
            pass
        st["stage"] = "artifact" if target == "file" else "writeback"
        st["writeback_row"] = 0
        _save_state(st)

    # ── STAGE 3a: MAP ARTIFACT (target=file). One read pass over payloads aligned
    #    to the projection, streamed to a gzipped catalog-map JSON the server can
    #    serve directly from /catalog/map. No Qdrant writes — the only path that
    #    scales to millions of points. Genre centroids are aggregated incrementally
    #    (running mean) so memory stays flat.
    if st["stage"] == "artifact":
        import gzip
        proj = np.memmap(proj_path, dtype=np.float32, mode="r", shape=(n, 2))
        rmap = role_map or {}
        os.makedirs(os.path.dirname(map_file) or ".", exist_ok=True)
        tmp_map = map_file + ".tmp"
        tag_agg: dict = {}     # tag -> [sum_px, sum_py, count]
        bad = row = written = 0
        offset = None
        print(f"[umap] writing catalog-map artifact → {map_file} (no Qdrant writes) ...")
        pbar = tqdm(total=n, desc="  ↳ map artifact", unit=" pt")
        with gzip.open(tmp_map, "wt", encoding="utf-8") as fh:
            fh.write('{"tracks":[')
            first = True
            while row < n:
                pts, offset = _retry(
                    lambda c, o=offset: c.scroll(collection, limit=5000, offset=o,
                                                 with_vectors=False, with_payload=True),
                    "scroll-artifact")
                if not pts:
                    break
                for p in pts:
                    if row >= n:
                        break
                    fields = (p.payload or {}).get("fields", {}) or {}
                    px, py = float(proj[row, 0]), float(proj[row, 1])
                    if not (np.isfinite(px) and np.isfinite(py)):
                        bad += 1
                        px, py = 0.0, 0.0
                    track, primary = _shape_map_track(fields, str(p.id), px, py, rmap)
                    fh.write(("" if first else ",") + json.dumps(track, separators=(",", ":")))
                    first = False
                    if primary:
                        a = tag_agg.setdefault(primary[0], [0.0, 0.0, 0])
                        a[0] += px; a[1] += py; a[2] += 1
                    row += 1
                    written += 1
                pbar.update(len(pts))
                if offset is None:
                    break
            centroids = [
                {"genre": t, "px": s[0] / s[2], "py": s[1] / s[2], "count": s[2]}
                for t, s in tag_agg.items() if s[2] >= 5
            ]
            centroids.sort(key=lambda x: -x["count"])
            fh.write('],"genres":' + json.dumps(centroids[:40], separators=(",", ":"))
                     + ',"total":' + str(written) + '}')
        pbar.close()
        os.replace(tmp_map, map_file)   # atomic publish
        if bad:
            print(f"[umap] warning: {bad} points had non-finite coords, zeroed out")
        del proj
        gc.collect()
        for path in (proj_path, arr_path, state_path):
            try:
                os.unlink(path)
            except OSError:
                pass
        print(f"[umap] done — catalog map artifact with {written} tracks at {map_file}.\n"
              f"[umap] point the server at it: quickbeam serve --catalog-map-file {map_file}")
        return

    # ── STAGE 3b: WRITE-BACK (resumable, parallel). The main thread scrolls ids in
    #    order and forms batches; a pool of worker connections applies the payload
    #    updates concurrently. wait=True per call keeps Qdrant from queueing
    #    unbounded work (memory backpressure that prevents the OOM/swap brick),
    #    while N workers multiply throughput ~N× over the single-stream path.
    #    set_payload is idempotent, so a resume may harmlessly re-write the few
    #    in-flight batches that weren't yet checkpointed.
    import concurrent.futures
    import queue as _queue
    import collections as _collections

    proj = np.memmap(proj_path, dtype=np.float32, mode="r", shape=(n, 2))
    skip_until = st.get("writeback_row", 0)   # resume point (constant for this run)
    flushed = skip_until                       # contiguous rows confirmed written
    B = max(1, writeback_batch)
    bad = 0
    seen = 0
    offset = None

    # Build a pool of worker clients (each thread needs its own gRPC channel).
    W = max(1, writeback_workers)
    pool = _queue.Queue()
    if reconnect and W > 1:
        for _ in range(W):
            try:
                pool.put(reconnect())
            except Exception:  # noqa: BLE001
                pass
    parallel = not pool.empty()
    if not parallel:
        W = 1  # no spare connections — fall back to synchronous on the main client

    print(f"[umap] writing px/py back to payloads"
          + (f" (resuming at {flushed}/{n})" if flushed else "")
          + f" — {W} worker(s){' [parallel]' if parallel else ''} ...")

    def _update_ops(c, ops):
        delay = 1.0
        for attempt in range(8):
            try:
                c.batch_update_points(collection_name=collection,
                                      update_operations=ops, wait=writeback_wait)
                return c
            except Exception as exc:  # noqa: BLE001
                if attempt == 7 or not any(t in str(exc).lower() for t in _TRANSIENT):
                    raise
                time.sleep(delay)
                delay = min(30.0, delay * 2)
                if reconnect:
                    try:
                        c = reconnect()
                    except Exception:  # noqa: BLE001
                        pass
        return c

    def _worker_wrapped(ops):
        # Take a client from the pool, apply the batch (reconnecting on transient
        # faults), and return the live client to the pool.
        c = pool.get()
        try:
            c = _update_ops(c, ops)
        finally:
            pool.put(c)

    pbar = tqdm(total=n, initial=flushed, desc="  ↳ set_payload", unit=" pt")
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=W) if parallel else None
    inflight = _collections.deque()   # (end_row, future) in submission order
    MAXQ = W * 2
    last_ops = None   # most recent batch — re-applied as a wait=True barrier when async

    def _drain_one():
        nonlocal flushed
        end_row, fut = inflight.popleft()
        fut.result()                  # propagate any terminal failure
        pbar.update(end_row - flushed)
        flushed = end_row
        st["writeback_row"] = flushed
        _save_state(st)

    try:
        while True:
            pts, offset = _retry(
                lambda c, o=offset: c.scroll(collection, limit=B, offset=o,
                                             with_vectors=False, with_payload=False),
                "scroll-writeback")
            if not pts:
                break
            ops = []
            for p in pts:
                if seen < skip_until:        # already written in a prior run — skip
                    seen += 1
                    continue
                if seen >= n:
                    break
                x, y = float(proj[seen, 0]), float(proj[seen, 1])
                if not (np.isfinite(x) and np.isfinite(y)):
                    bad += 1
                    x, y = 0.0, 0.0
                ops.append(models.SetPayloadOperation(
                    set_payload=models.SetPayload(payload={"px": x, "py": y}, points=[p.id])))
                seen += 1

            if ops:
                last_ops = ops
                if parallel:
                    inflight.append((seen, ex.submit(_worker_wrapped, ops)))
                    while len(inflight) >= MAXQ:
                        _drain_one()
                else:
                    _update_ops(client[0], ops)
                    pbar.update(seen - flushed)
                    flushed = seen
                    st["writeback_row"] = flushed
                    _save_state(st)
                    if writeback_sleep:
                        time.sleep(writeback_sleep)
            if offset is None or seen >= n:
                break
        while inflight:
            _drain_one()
        # With wait=False the futures only confirm the server *accepted* each
        # batch, not that it applied it. Re-issue the final batch with wait=True
        # as a barrier so everything is durably applied before we delete temps.
        if not writeback_wait and last_ops is not None:
            print("[umap] final flush — waiting for queued writes to apply ...")
            _retry(lambda c, ops=last_ops: c.batch_update_points(
                collection_name=collection, update_operations=ops, wait=True),
                "final-flush")
    finally:
        if ex:
            ex.shutdown(wait=True)
    pbar.close()
    done = flushed

    if bad:
        print(f"[umap] warning: {bad} points had non-finite coords, zeroed out")

    # ── Done — clean up temp artifacts.
    del proj
    gc.collect()
    for path in (proj_path, arr_path, state_path):
        try:
            os.unlink(path)
        except OSError:
            pass
    print(f"[umap] done — px/py on {done} points. Snapshot now to bake it in.")

# ---------------------------------------------------------------------------
# SUBGRAPH QUERIES
# block_gt variants add `blockNumber_gt` to the where clause for incremental
# polling in the watcher (avoids re-scanning the full event history each cycle).
# ---------------------------------------------------------------------------
_PUBLISHES_Q = "query Publishes($schemaId: Bytes!, $first: Int!, $skip: Int!) { manifestPublisheds(where: { schemaId: $schemaId }, first: $first, skip: $skip, orderBy: blockNumber, orderDirection: asc) { id owner schemaId nameHash name manifestCid blockNumber blockTimestamp transactionHash } }"
_PUBLISHES_Q_FROM = "query Publishes($schemaId: Bytes!, $first: Int!, $skip: Int!, $blockGt: BigInt!) { manifestPublisheds(where: { schemaId: $schemaId, blockNumber_gt: $blockGt }, first: $first, skip: $skip, orderBy: blockNumber, orderDirection: asc) { id owner schemaId nameHash name manifestCid blockNumber blockTimestamp transactionHash } }"
_UPDATES_Q = "query Updates($schemaId: Bytes!, $first: Int!, $skip: Int!) { manifestUpdateds(where: { schemaId: $schemaId }, first: $first, skip: $skip, orderBy: version, orderDirection: desc) { id owner schemaId nameHash manifestCid version blockNumber blockTimestamp transactionHash } }"
_UPDATES_Q_FROM = "query Updates($schemaId: Bytes!, $first: Int!, $skip: Int!, $blockGt: BigInt!) { manifestUpdateds(where: { schemaId: $schemaId, blockNumber_gt: $blockGt }, first: $first, skip: $skip, orderBy: version, orderDirection: desc) { id owner schemaId nameHash manifestCid version blockNumber blockTimestamp transactionHash } }"

# Unfiltered variants — used by the Composed View path (Phase 1). A view names
# its sources by *resourceId* (a hash of owner+schemaId+name), which the subgraph
# does not index, so we page the full ManifestPublished/Updated history and
# recompute each event's resourceId locally (see _identity.resource_id) to keep
# the ones a view asked for.
#
# These page with a KEYSET cursor (`id_gt`), not `skip`: The Graph hard-caps `skip`
# at 5000, and the global history routinely exceeds that (a single sharded publish
# emits thousands of ManifestPublished records). Ordering by `id` asc and advancing
# `id_gt` to the last row's id has no such limit. Consumers select the latest
# manifest per resourceId by comparing blockNumber explicitly, so query order is
# irrelevant here.
_PUBLISHES_ALL_Q = "query Publishes($first: Int!, $lastId: String!) { manifestPublisheds(first: $first, where: { id_gt: $lastId }, orderBy: id, orderDirection: asc) { id owner schemaId nameHash name manifestCid blockNumber blockTimestamp transactionHash } }"
_UPDATES_ALL_Q = "query Updates($first: Int!, $lastId: String!) { manifestUpdateds(first: $first, where: { id_gt: $lastId }, orderBy: id, orderDirection: asc) { id owner schemaId nameHash manifestCid version blockNumber blockTimestamp transactionHash } }"

async def _query_subgraph_async(url, api_key, query, variables):
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with aiohttp.ClientSession() as session:
        for attempt in range(5):
            try:
                async with session.post(url, json={"query": query, "variables": variables}, headers=headers, timeout=30) as resp:
                    if resp.status in {429, 500, 502, 503, 504}: raise Exception()
                    resp.raise_for_status()
                    data = await resp.json()
                    if "errors" in data: raise RuntimeError(data["errors"])
                    return data["data"]
            except Exception:
                if attempt == 4: raise
                await asyncio.sleep(1 + attempt)


async def _fetch_all_events_async(url, api_key, schema_id, page_size, block_gt=None):
    publishes, updates = [], []
    pairs = [
        (publishes, _PUBLISHES_Q_FROM if block_gt is not None else _PUBLISHES_Q, "manifestPublisheds"),
        (updates,   _UPDATES_Q_FROM   if block_gt is not None else _UPDATES_Q,   "manifestUpdateds"),
    ]
    for target, query, key in pairs:
        skip = 0
        pbar = tqdm(desc=f"  ↳ Fetching {key}", unit=" events", leave=False)
        while True:
            variables = {"schemaId": schema_id, "first": page_size, "skip": skip}
            if block_gt is not None:
                variables["blockGt"] = block_gt
            data = await _query_subgraph_async(url, api_key, query, variables)
            batch = data.get(key, [])
            target.extend(batch)
            pbar.update(len(batch))
            if len(batch) < page_size:
                break
            skip += page_size
        pbar.close()
    return publishes, updates


async def _fetch_all_events_global(url, api_key, page_size):
    """Page the *entire* ManifestPublished/Updated history (no schemaId filter).
    Used only by the Composed View path, where sources are resourceIds we must
    match against every datasource rather than a single known schema."""
    publishes, updates = [], []
    pairs = [
        (publishes, _PUBLISHES_ALL_Q, "manifestPublisheds"),
        (updates,   _UPDATES_ALL_Q,   "manifestUpdateds"),
    ]
    for target, query, key in pairs:
        last_id = ""  # keyset cursor: "" is lexicographically smallest → first page
        pbar = tqdm(desc=f"  ↳ Scanning {key}", unit=" events", leave=False)
        while True:
            data = await _query_subgraph_async(url, api_key, query, {"first": page_size, "lastId": last_id})
            batch = data.get(key, [])
            target.extend(batch)
            pbar.update(len(batch))
            if len(batch) < page_size:
                break
            last_id = batch[-1]["id"]  # advance past the last row; no skip cap
        pbar.close()
    return publishes, updates

# ---------------------------------------------------------------------------
# IPFS HELPERS
# ---------------------------------------------------------------------------
_B58_ALPHABET = b'123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'

def _b58encode(v: bytes) -> str:
    leading = len(v) - len(v.lstrip(b'\x00'))
    n = int.from_bytes(v, 'big')
    res = []
    while n:
        n, r = divmod(n, 58)
        res.append(_B58_ALPHABET[r])
    return ('1' * leading) + bytes(reversed(res)).decode('ascii')

def _cid_to_path(cid: str) -> str:
    # Chunk dataCids are stored as full `ipfs://<dirCid>/<file>` URIs (UnixFS dir +
    # path); strip the scheme so the gateway URL is `<gw>/ipfs/<dirCid>/<file>` and
    # not the malformed `<gw>/ipfs/ipfs://<dirCid>/<file>` (→ 400).
    if cid.startswith("ipfs://"):
        cid = cid[len("ipfs://"):]
    if cid.startswith(('0x', '0X')):
        raw = bytes.fromhex(cid[2:])
        if len(raw) == 34 and raw[0] == 0x12 and raw[1] == 0x20:
            return _b58encode(raw)
    return cid

async def _fetch_json(session, sem, url, timeout, pbar):
    async with sem:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 200:
                    res = json.loads(await resp.text())
                    pbar.update(1)
                    return res
                print(f"\n[IPFS] {resp.status} {url}", flush=True)
        except Exception as e:
            print(f"\n[IPFS] error fetching {url}: {e}", flush=True)
        pbar.update(1)
        return None

async def fetch_all_ipfs(cids, gateway, timeout, concurrency, desc="Downloading IPFS", headers=None):
    if not cids: return {}
    sem = asyncio.Semaphore(concurrency)
    pbar = tqdm(total=len(cids), desc=f"  ↳ {desc}", unit=" file")
    async with aiohttp.ClientSession(headers=headers or {}) as session:
        tasks = [
            asyncio.create_task(_fetch_json(session, sem, f"{gateway.rstrip('/')}/{_cid_to_path(cid)}", timeout, pbar))
            for cid in cids
        ]
        results = await asyncio.gather(*tasks)
    pbar.close()
    return dict(zip(cids, results))

def _track_id(fields: dict, prefer: str | None = None) -> str:
    if prefer:
        return str(prefer).strip().removeprefix("track:")
    for key in ["trackId", "track_id", "id", "contentId"]:
        if fields.get(key): return str(fields[key]).strip().removeprefix("track:")
    artist = str(fields.get("artist") or "").strip()
    title  = str(fields.get("title")  or "").strip()
    if artist and title: return hashlib.sha256(f"{artist}:{title}".encode()).hexdigest()[:24]
    return str(uuid.uuid4())[:12]

# ---------------------------------------------------------------------------
# ROOT PROFILES — graph-as-source-of-truth projections
#
# A bundle is a graph (typed nodes + typed edges). A *profile* projects that one
# graph from a chosen root type into a distinct document: walking the graph up to
# `max_depth` hops and folding the neighbor entities it cares about (`include`)
# into grouped label lists. The SAME graph yields a Track view, an Artist view, a
# Place view, etc. — each becomes its own embedding. Add a profile here (or via
# --profiles-file) and a new semantic view exists with no change to the graph.
#
# Node types are the entityTypes produced by the mb_pg registry: Artist,
# ReleaseGroup, Release, Recording, Work, Area, Place, Event, Instrument.
# ---------------------------------------------------------------------------

# These are a collection of default or frequently used root profiles
# Root profiles can be defined externally and passed as a cli arg
ROOT_PROFILES: dict[str, dict] = {
    "track": {
        "root_type": "Recording", "max_depth": 2,
        "include": ["Artist", "Work", "Release", "ReleaseGroup", "Place", "Event", "Area"],
    },
    "recording": {  # alias of track for graphs that name the root "Recording"
        "root_type": "Recording", "max_depth": 2,
        "include": ["Artist", "Work", "Release", "ReleaseGroup", "Place", "Event", "Area"],
    },
    "artist": {
        "root_type": "Artist", "max_depth": 2,
        "include": ["Recording", "Release", "ReleaseGroup", "Work", "Place", "Event", "Area"],
    },
    "release": {
        "root_type": "Release", "max_depth": 2,
        "include": ["Artist", "Recording", "ReleaseGroup", "Work"],
    },
    "place": {
        "root_type": "Place", "max_depth": 3,
        "include": ["Artist", "Recording", "Event", "Area"],
    },
    "event": {
        "root_type": "Event", "max_depth": 2,
        "include": ["Artist", "Recording", "Place", "Area"],
    },
    "work": {
        "root_type": "Work", "max_depth": 2,
        "include": ["Artist", "Recording", "Release"],
    },
    # local-business graph (places_pg): one document per Business, folding in its
    # reviews, categories, locality, and reviewers — the shape the per-bar demo
    # shard embeds. Depth 2 reaches Business→Review→Reviewer. Nearby businesses
    # ("Business") are deliberately NOT folded: a list of 20 neighbouring bar
    # names is pure noise that dilutes the vector and crowds review content out of
    # the embedding's token budget. The `near` graph edges still exist for the
    # "nearby" UI rail — they just don't pollute the embedded text.
    "business": {
        "root_type": "Business", "max_depth": 2,
        "include": ["Review", "Category", "Locality", "Reviewer", "Event"],
    },
    # one document per Review, so the review *body* (the high-value free-text
    # signal — "best tacos in town") is embedded and directly searchable. Without
    # this, a review only ever folds into its Business as a label ("<author> on
    # <business>") and its body is invisible to vector search. Folding the body
    # into the Business doc alone isn't enough: dozens of long reviews can't fit a
    # single 256-token business embedding, so each review needs its own document.
    # Depth 1 folds in the venue Business + Reviewer for context; the Review's
    # businessId field links a hit back to its place.
    "review": {
        "root_type": "Review", "max_depth": 1,
        "include": ["Business", "Reviewer"],
    },
    # events graph (events_pg), merged into the places graph: one document per
    # Event, folding in its venue Business, organizer, category and locality.
    "localevent": {
        "root_type": "Event", "max_depth": 2,
        "include": ["Business", "Organizer", "Category", "Locality"],
    },
}


def _load_profiles(args) -> list[dict]:
    """Resolve --root-profile names (or fall back to a single --root-type) into a
    list of fully-specified profile dicts. Each carries a `name` and `root_type`.
    """
    registry = dict(ROOT_PROFILES)
    if args.profiles_file and os.path.exists(args.profiles_file):
        with open(args.profiles_file) as f:
            for name, prof in (json.load(f) or {}).items():
                registry[name.lower()] = {**registry.get(name.lower(), {}), **prof}

    # TODO: I think I can remove this
    if not args.root_profile:
        # Legacy: one projection, one-hop neighbor *field* fold (preserves the old
        # Recording-gets-byArtist behavior the catalog map relies on).
        return [{"name": args.root_type.lower(), "root_type": args.root_type,
                 "fold": True, "max_depth": 1, "include": None}]

    profiles = []
    for raw in args.root_profile:
        key = raw.strip().lower()
        if key not in registry:
            raise SystemExit(
                f"Unknown --root-profile '{raw}'. Known: {', '.join(sorted(registry))} "
                f"(or define it in --profiles-file).")
        prof = {"name": key, **registry[key]}
        prof.setdefault("max_depth", args.max_depth)
        prof.setdefault("include", None)
        profiles.append(prof)
    return profiles


def _node_key(node: dict) -> str:
    """Global join key for a node: its Entity URI when present else the raw local id. 
    Keying the adjacency and projections on this resolves edges on the globally-unique identity rather than a
    publisher-local id for cross-publisher linking."""
    return node.get("entityUri") or node.get("id")


def _node_label(node: dict) -> str:
    """Human label for a node — title / name / label, whichever the node carries."""
    f = node.get("fields", {}) or {}
    for k in ("title", "name", "label"):
        v = f.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _node_content(node: dict) -> str:
    """Folded value for a neighbour node. Prefer a free-form *content* field (a
    Review `body`, an event/summary `description`) so that text becomes searchable
    when the node is folded into a root document — without this a Review folds in
    only its "<author> on <business>" title and the body ("best tacos in town") is
    silently dropped. Content-less nodes (Category, Locality, Reviewer) have none
    of these fields and fall back to their title label, so they don't bloat the doc."""
    f = node.get("fields", {}) or {}
    for k in ("body", "summary", "description"):
        v = f.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return _node_label(node)

# normalize group keys
def _group_key(type_name: str) -> str:
    """Node type → camelCase plural field name. Artist→artists, Work→works,
    Place→places, ReleaseGroup→releaseGroups."""
    t = (type_name[:1].lower() + type_name[1:]) if type_name else type_name
    if t.endswith("y"):
        return t[:-1] + "ies"
    if t.endswith(("s", "x", "z", "ch", "sh")):
        return t + "es"
    return t + "s"

# walk the graph and rebuild the bundles
def _walk_graph(root_id, adj, max_depth, node_cap):
    """BFS from root over an (undirected) adjacency map, returning [(node_id, depth)]
    for every reachable node within `max_depth` (excluding the root). Bounded by
    `node_cap` so a high-degree hub can't blow up a single projection."""
    from collections import deque
    visited = {root_id}
    queue = deque([(root_id, 0)])
    collected = []
    while queue:
        nid, d = queue.popleft()
        if d >= max_depth:
            continue
        for nb in adj.get(nid, ()):  # neighbors
            if nb in visited:
                continue
            visited.add(nb)
            collected.append((nb, d + 1))
            if len(collected) >= node_cap:
                return collected
            queue.append((nb, d + 1))
    return collected


def _project(root, nodes_by_id, adj, out, profile, defaults):
    # TODO: remove legacy support?
    """Project a root node into a profile document. `fold` profiles reproduce the
    legacy one-hop field merge; otherwise we walk the graph and fold included
    neighbors into grouped, deduped, capped label lists."""
    rt = profile.get("root_type") or root.get("type")
    fields = dict(root.get("fields", {}))

    if profile.get("fold"):
        for tid in out.get(_node_key(root), ()):
            nb = nodes_by_id.get(tid)
            if nb:
                fields.update(nb.get("fields", {}))
        fields["entityType"] = rt
        return fields

    depth = int(profile.get("max_depth", defaults["max_depth"]))
    label_cap = int(profile.get("label_cap", defaults["label_cap"]))
    node_cap = int(profile.get("node_cap", defaults["node_cap"]))
    include = profile.get("include")
    include_set = set(include) if include else None

    groups: dict = {}
    for nid, _depth in _walk_graph(_node_key(root), adj, depth, node_cap):
        nb = nodes_by_id.get(nid)
        if not nb:
            continue
        t = nb.get("type")
        if include_set is not None and t not in include_set:
            continue
        value = _node_content(nb)
        if value:
            groups.setdefault(_group_key(t), []).append(value)

    for k, vals in groups.items():
        fields[k] = list(dict.fromkeys(vals))[:label_cap]  # dedupe (order-preserving) + cap
    fields["entityType"] = rt
    return fields


# ---------------------------------------------------------------------------
# BUNDLE JOIN — async generator, one manifest at a time
#
# Yields (manifest_cid, [records]) for each pending bundle manifest.
# Processing one manifest at a time keeps memory bounded regardless of
# collection size; chunk data is freed before the next manifest is fetched.
#
# Parameters
#   completed_manifest_cids : set of already-finished manifest CIDs to skip
#   owner_filter            : set of lowercase owner addresses (None = any)
#   name_filter             : set of lowercase dataset names  (None = any)
#   block_gt                : if set, only query events with blockNumber > this
# ---------------------------------------------------------------------------
async def build_bundle_joined_data(
    args,
    schema_id,
    profiles,
    completed_manifest_cids=None,
    owner_filter=None,
    name_filter=None,
    block_gt=None,
):
    completed = completed_manifest_cids or set()
    defaults = {"max_depth": args.max_depth, "label_cap": args.label_cap,
                "node_cap": args.node_cap}

    print(f"\n[Builder] Querying Subgraph for bundle ManifestPublished events...")
    publishes, updates = await _fetch_all_events_async(
        args.subgraph_url, args.graph_api_key, schema_id, args.page_size,
        block_gt=block_gt
    )

    cids_meta = {}
    for p in publishes: cids_meta[p["manifestCid"]] = p
    for u in updates:   cids_meta[u["manifestCid"]] = u

    if not cids_meta:
        return

    # Apply filter hierarchy: owner → dataset name
    if owner_filter:
        cids_meta = {c: m for c, m in cids_meta.items()
                     if m.get("owner", "").lower() in owner_filter}
    if name_filter:
        cids_meta = {c: m for c, m in cids_meta.items()
                     if m.get("name", "").lower() in name_filter}

    pending_cids = [c for c in cids_meta if c not in completed]
    if not pending_cids:
        print("[Builder] No pending manifests after filters.")
        return

    print(f"[Builder] {len(pending_cids)} pending manifests (skipped {len(cids_meta) - len(pending_cids)} completed).")

    gw_headers = {"Authorization": f"Bearer {args.ipfs_gateway_key}"} if args.ipfs_gateway_key else {}

    # Fetch only the manifest envelopes (small JSON), not chunks yet.
    manifests = await fetch_all_ipfs(
        pending_cids, args.ipfs_gateway, args.ipfs_timeout,
        args.concurrency, desc="Bundle Manifests", headers=gw_headers
    )

    for mcid in pending_cids:
        m = manifests.get(mcid)
        # v3 bundle manifests are tagged either by `kind: "bundle"` (current
        # publisher format) or a legacy `version: 3`. Both carry nodeChunks +
        # edgeChunk; accept either tag.
        if not m or not (m.get("kind") == "bundle" or m.get("version") == 3):
            print(f"[Builder] Skipping invalid manifest {_cid_to_path(mcid)!r}: {str(m)[:120]!r}")
            continue

        node_cids = [c["dataCid"] for c in m.get("nodeChunks", [])]
        # Edges are chunked into many leaves (`edgeChunks`); older manifests had a
        # single `edgeChunk`. Accept both.
        edge_refs = m.get("edgeChunks") or ([m["edgeChunk"]] if m.get("edgeChunk") else [])
        edge_cids = [c["dataCid"] for c in edge_refs if c.get("dataCid")]
        if not edge_cids:
            print(f"[Builder] Skipping manifest {_cid_to_path(mcid)!r} — no edge chunks")
            continue

        # Fetch only this manifest's chunks, then free them before moving on.
        # This bounds RAM to one manifest's data at a time.
        print(f"[Builder] Fetching {len(node_cids) + len(edge_cids)} chunks for {_cid_to_path(mcid)[:16]}...")
        chunks = await fetch_all_ipfs(
            node_cids + edge_cids, args.ipfs_gateway, args.ipfs_timeout,
            args.concurrency, desc="  Chunks", headers=gw_headers
        )

        meta = cids_meta[mcid]
        # Index nodes by their global Entity URI (SDK slice 0.3), falling back to
        # the raw local id for pre-0.3 data. `id_to_key` translates edge endpoints
        # — still emitted as local ids — onto the same global key, so the
        # adjacency joins on identity rather than a publisher-local id.
        nodes_by_id: dict = {}
        id_to_key: dict = {}
        for ncid in node_cids:
            for node in (chunks.get(ncid) or []):
                key = _node_key(node)
                nodes_by_id[key] = node
                if node.get("id") is not None:
                    id_to_key[node["id"]] = key
        edges = []
        for ecid in edge_cids:
            edges.extend(chunks.get(ecid) or [])

        # Build adjacency once per manifest, reused across every profile.
        #   `out` — outgoing edges only (legacy one-hop field fold).
        #   `adj` — undirected, for multi-hop profile walks (a Place must reach the
        #           artists/events on either side of its edges).
        need_fold = any(p.get("fold") for p in profiles)
        need_walk = any(not p.get("fold") for p in profiles)
        out: dict = {}
        adj: dict = {}
        for e in edges:
            frm = id_to_key.get(e["from"], e["from"])
            to  = id_to_key.get(e["to"], e["to"])
            if need_fold:
                out.setdefault(frm, []).append(to)
            if need_walk:
                adj.setdefault(frm, []).append(to)
                adj.setdefault(to, []).append(frm)

        records = []
        for prof in profiles:
            rt = prof.get("root_type")
            n_roots = 0
            for node in nodes_by_id.values():
                if node.get("type") != rt:
                    continue
                fields = _project(node, nodes_by_id, adj, out, prof, defaults)
                records.append({
                    "track_id":    _track_id(fields, prefer=node.get("id")),
                    "entity_type": rt,
                    "fields":      fields,
                    "meta":        meta,
                })
                n_roots += 1
            if n_roots == 0:
                print(f"[Builder] Manifest {_cid_to_path(mcid)[:16]}... profile "
                      f"{prof['name']!r}: no {rt!r} root nodes.")

        del chunks, nodes_by_id, id_to_key, edges, out, adj
        import gc; gc.collect()

        if records:
            yield mcid, records
        else:
            print(f"[Builder] Manifest {_cid_to_path(mcid)!r} produced no records — skipping.")


# ---------------------------------------------------------------------------
# COMPOSED VIEW JOIN — multi-source fusion (Phase 1)
#
# A bundle is one publisher's graph. A *view* fuses several publishers' graphs
# into one, joining on global identity (Entity URI + namespaced aliases from
# Phase 0) — deterministically, no ML. Where the bundle path streams one manifest
# at a time, fusion is inherently cross-source, so the view path holds all
# sources' nodes at once and yields a single fused record set.
# ---------------------------------------------------------------------------
class _DSU:
    """Tiny union-find. Roots are the lexicographically-smallest member so a fused
    cluster's canonical key is stable across runs (deterministic point ids)."""
    def __init__(self):
        self._p = {}

    def find(self, x):
        p = self._p
        p.setdefault(x, x)
        root = x
        while p[root] != root:
            root = p[root]
        while p[x] != root:  # path compression
            p[x], x = root, p[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if rb < ra:
            ra, rb = rb, ra
        self._p[rb] = ra


def _alias_index(nodes_by_id):
    """alias string -> a node key that carries it (first wins). Lets a linkset
    endpoint expressed as an alias (`isrc:…`) resolve to an actual fused node."""
    idx = {}
    for key, node in nodes_by_id.items():
        for al in (node.get("aliases") or []):
            idx.setdefault(al, key)
    return idx


def _resolve_endpoint(endpoint, nodes_by_id, alias_idx):
    """Map a linkset endpoint (an Entity URI or a `namespace:value` alias) to a
    fused node key, or None when it points outside this view's loaded data."""
    if not isinstance(endpoint, str) or not endpoint:
        return None
    if endpoint in nodes_by_id:          # Entity URI naming a loaded node
        return endpoint
    return alias_idx.get(endpoint)       # namespaced alias → the node carrying it


def _fuse_nodes(nodes_by_id, extra_unions=()):
    """Union-find over the shared global key: two nodes collapse to one cluster
    when they share an alias (e.g. the same `isrc:`). Identical Entity URIs have
    already collapsed via dict keying. `extra_unions` is a list of (keyA, keyB)
    pairs from asserted `sameAs` linkset edges (Phase 2) — merged into the SAME
    clusters as shared ids. Returns (dsu, merged_by_canonical_key), where each
    merged node unions its members' fields (first-writer-wins) and aliases, and
    is re-keyed to the cluster's canonical Entity URI."""
    dsu = _DSU()
    alias_owner = {}
    for key, node in nodes_by_id.items():
        dsu.find(key)  # register every node, even alias-less ones
        for al in (node.get("aliases") or []):
            prev = alias_owner.get(al)
            if prev is not None:
                dsu.union(prev, key)
            else:
                alias_owner[al] = key

    for a, b in extra_unions:  # asserted sameAs equivalences
        dsu.union(a, b)

    merged = {}
    for key, node in nodes_by_id.items():
        c = dsu.find(key)
        m = merged.get(c)
        if m is None:
            merged[c] = {
                "id": node.get("id"),
                "type": node.get("type"),
                "entityUri": c,
                "aliases": list(node.get("aliases") or []),
                "fields": dict(node.get("fields") or {}),
            }
        else:
            for fk, fv in (node.get("fields") or {}).items():
                m["fields"].setdefault(fk, fv)
            for al in (node.get("aliases") or []):
                if al not in m["aliases"]:
                    m["aliases"].append(al)
            if not m.get("type"):
                m["type"] = node.get("type")
    return dsu, merged


async def build_view_joined_data(args, view_schema_id, profiles, completed_manifest_cids=None):
    """Fuse a Composed View's source datasources into one graph and project it.

    Yields exactly one (view_manifest_cid, records) pair — the view is treated as
    a single unit of work for checkpointing, keyed on its own manifest CID.
    """
    from quickbeam._identity import resource_id, norm_hex
    completed = completed_manifest_cids or set()
    defaults = {"max_depth": args.max_depth, "label_cap": args.label_cap, "node_cap": args.node_cap}
    gw_headers = {"Authorization": f"Bearer {args.ipfs_gateway_key}"} if args.ipfs_gateway_key else {}

    # ── 1. Resolve the view artifact → its latest manifest → source set ──
    print(f"\n[View] Resolving view schema {view_schema_id}...")
    publishes, updates = await _fetch_all_events_async(
        args.subgraph_url, args.graph_api_key, view_schema_id, args.page_size)
    view_events = publishes + updates
    if not view_events:
        print(f"[View] No manifests for view schema {view_schema_id}.")
        return
    view_ev = max(view_events, key=lambda e: int(e.get("blockNumber", 0)))
    view_mcid = view_ev["manifestCid"]
    if view_mcid in completed:
        print(f"[View] {_cid_to_path(view_mcid)[:16]}... already embedded.")
        return
    vman = (await fetch_all_ipfs([view_mcid], args.ipfs_gateway, args.ipfs_timeout,
                                 args.concurrency, desc="View Manifest", headers=gw_headers)).get(view_mcid)
    if not vman or vman.get("kind") != "view":
        print(f"[View] {_cid_to_path(view_mcid)!r} is not a view manifest: {str(vman)[:120]!r}")
        return
    sources = {norm_hex(s) for s in vman.get("sources", [])}
    if not sources:
        print("[View] view declares no sources.")
        return
    link_ids = {norm_hex(l) for l in vman.get("linksets", [])}
    # Phase 4 trust gate; for now honor a minConfidence floor if the view carries one.
    min_conf = (vman.get("trust") or {}).get("minConfidence")
    print(f"[View] fusing {len(sources)} source(s) + {len(link_ids)} linkset(s)"
          + (f"; minConfidence={min_conf}" if min_conf is not None else ""))

    # ── 2. Discover each source/linkset's latest manifest → resourceId match ──
    # A source is named by resourceId = keccak(owner, schemaId, datasetName), which
    # the subgraph does not index. If the view recorded the backing schemaIds
    # (ViewManifest.sourceSchemas), query those schemas directly — cheap. Fall back
    # to the whole-history scan only for sources NOT covered (e.g. foreign sources
    # whose schemaId the view didn't record), so a fully-hinted view never scans.
    wanted = sources | link_ids
    best = {}  # resourceId -> (blockNumber, manifestCid)

    def _absorb(events):
        for ev in events:
            try:
                rid = norm_hex(resource_id(ev["owner"], ev["schemaId"], ev["nameHash"], is_hash=True))
            except Exception:
                continue
            if rid not in wanted:
                continue
            bn = int(ev.get("blockNumber", 0))
            cur = best.get(rid)
            if cur is None or bn > cur[0]:
                best[rid] = (bn, ev["manifestCid"])

    schema_ids = {norm_hex(s) for s in (vman.get("sourceSchemas") or [])}
    for sid in schema_ids:
        p, u = await _fetch_all_events_async(args.subgraph_url, args.graph_api_key, sid, args.page_size)
        _absorb(p + u)
    if schema_ids:
        print(f"  ↳ view schema hint: resolved {len(set(best) & wanted)}/{len(wanted)} source(s) via {len(schema_ids)} per-schema query(ies)")

    # Global fallback: only if the hint left something unresolved (or was absent).
    if wanted - set(best):
        if schema_ids:
            print(f"  ↳ {len(wanted - set(best))} source(s) not covered by the schema hint — scanning full history")
        g_pub, g_upd = await _fetch_all_events_global(args.subgraph_url, args.graph_api_key, args.page_size)
        _absorb(g_pub + g_upd)

    if not (set(best) & sources):
        print("[View] none of the view's sources resolved to a manifest.")
        return
    missing = wanted - set(best)
    if missing:
        print(f"[View] {len(missing)} declared source/linkset(s) had no manifest and were skipped.")

    # ── 3. Fetch every source manifest's chunks → one global node index + edges ──
    nodes_by_id = {}
    edges_global = []  # (from_key, to_key), already resolved onto global keys
    for rid in (s for s in sources if s in best):
        _bn, mcid = best[rid]
        m = (await fetch_all_ipfs([mcid], args.ipfs_gateway, args.ipfs_timeout,
                                  args.concurrency, desc=f"  Src {rid[:10]}", headers=gw_headers)).get(mcid)
        if not m or not (m.get("kind") == "bundle" or m.get("version") == 3):
            print(f"[View] source {rid[:10]} manifest {_cid_to_path(mcid)!r} not a bundle — skipped.")
            continue
        node_cids = [c["dataCid"] for c in m.get("nodeChunks", [])]
        edge_refs = m.get("edgeChunks") or ([m["edgeChunk"]] if m.get("edgeChunk") else [])
        edge_cids = [c["dataCid"] for c in edge_refs if c.get("dataCid")]
        chunks = await fetch_all_ipfs(node_cids + edge_cids, args.ipfs_gateway, args.ipfs_timeout,
                                      args.concurrency, desc="  Chunks", headers=gw_headers)
        # Resolve local id -> global key PER SOURCE: two publishers can reuse the
        # same local node id, so they must not collide before the union-find joins
        # on global identity.
        id_to_key = {}
        for ncid in node_cids:
            for node in (chunks.get(ncid) or []):
                key = _node_key(node)
                nodes_by_id[key] = node
                if node.get("id") is not None:
                    id_to_key[node["id"]] = key
        for ecid in edge_cids:
            for e in (chunks.get(ecid) or []):
                edges_global.append((id_to_key.get(e["from"], e["from"]),
                                     id_to_key.get(e["to"], e["to"])))

    if not nodes_by_id:
        print("[View] sources resolved but produced no nodes.")
        return

    # ── 3b. Ingest the view's linksets (Phase 2): asserted cross-edges over global
    #        identity. `sameAs` becomes a union; any other rel becomes a graph edge.
    #        Endpoints resolve to a fused node by Entity URI or by namespaced alias;
    #        a link to an entity outside this view's loaded data is dropped. ──
    alias_idx = _alias_index(nodes_by_id)
    same_as = []   # (keyA, keyB) equivalences fed into the union-find
    n_links = n_skipped = 0
    for rid in (l for l in link_ids if l in best):
        _bn, mcid = best[rid]
        m = (await fetch_all_ipfs([mcid], args.ipfs_gateway, args.ipfs_timeout,
                                  args.concurrency, desc=f"  Link {rid[:10]}", headers=gw_headers)).get(mcid)
        if not m or m.get("kind") != "linkset":
            print(f"[View] linkset {rid[:10]} manifest {_cid_to_path(mcid)!r} not a linkset — skipped.")
            continue
        link_cids = [c["dataCid"] for c in m.get("linkChunks", []) if c.get("dataCid")]
        lchunks = await fetch_all_ipfs(link_cids, args.ipfs_gateway, args.ipfs_timeout,
                                       args.concurrency, desc="  Links", headers=gw_headers)
        for lcid in link_cids:
            for link in (lchunks.get(lcid) or []):
                if min_conf is not None and link.get("confidence") is not None \
                        and link["confidence"] < min_conf:
                    n_skipped += 1
                    continue
                a = _resolve_endpoint(link.get("from"), nodes_by_id, alias_idx)
                b = _resolve_endpoint(link.get("to"), nodes_by_id, alias_idx)
                if a is None or b is None:
                    n_skipped += 1
                    continue
                if link.get("rel") == "sameAs":
                    same_as.append((a, b))
                else:
                    edges_global.append((a, b))
                n_links += 1
    if link_ids:
        print(f"[View] applied {n_links} link(s) ({len(same_as)} sameAs); skipped {n_skipped}.")

    # ── 4. Union-find: collapse cross-source nodes sharing a global key OR an
    #        asserted sameAs ──
    dsu, merged = _fuse_nodes(nodes_by_id, extra_unions=same_as)
    print(f"[View] fused {len(nodes_by_id)} nodes → {len(merged)} entities.")

    # ── 5. Adjacency over canonical cluster keys ──
    need_fold = any(p.get("fold") for p in profiles)
    need_walk = any(not p.get("fold") for p in profiles)
    out, adj = {}, {}
    for frm, to in edges_global:
        f, t = dsu.find(frm), dsu.find(to)
        if need_fold:
            out.setdefault(f, []).append(t)
        if need_walk:
            adj.setdefault(f, []).append(t)
            adj.setdefault(t, []).append(f)

    # ── 6. Project per profile over the fused graph ──
    meta = {"manifestCid": view_mcid, "owner": view_ev.get("owner"), "name": view_ev.get("name")}
    records = []
    for prof in profiles:
        rt = prof.get("root_type")
        n_roots = 0
        for node in merged.values():
            if node.get("type") != rt:
                continue
            fields = _project(node, merged, adj, out, prof, defaults)
            records.append({
                "track_id":    _track_id(fields, prefer=node.get("entityUri")),
                "entity_type": rt,
                "fields":      fields,
                "meta":        meta,
            })
            n_roots += 1
        if n_roots == 0:
            print(f"[View] profile {prof['name']!r}: no {rt!r} root nodes in the fused graph.")

    if records:
        yield view_mcid, records
    else:
        print("[View] fused graph produced no records.")

# ---------------------------------------------------------------------------
# EMBED + UPLOAD(shared by build and watch)
#
# Embeds records in SAVE_BATCH_SIZE chunks and uploads to Qdrant.
# Writes partial progress to checkpoint after each batch for crash recovery.
# ---------------------------------------------------------------------------
async def _embed_and_upload(args, qdrant, embed_engine, records, role_map, dim, truncate, checkpoint):
    SAVE_BATCH_SIZE = 5000
    n_batches = max(1, (len(records) + SAVE_BATCH_SIZE - 1) // SAVE_BATCH_SIZE)
    for i in range(0, len(records), SAVE_BATCH_SIZE):
        chunk = records[i: i + SAVE_BATCH_SIZE]
        if n_batches > 1:
            print(f"  [Embed] sub-batch {i // SAVE_BATCH_SIZE + 1}/{n_batches} ({len(chunk)} records)")

        texts = []
        for item in chunk:
            fields = item["fields"]
            if args.searchable_fields == "auto":
                tags = " ".join(
                    fields.get(t, "") if isinstance(fields.get(t), str) else ""
                    for t in role_map.get("tags", [])
                )
                # Fold any projected neighbor lists (artists, events, …) into the
                # document text so each projection embeds its full graph context —
                # the whole point of root profiles. (Legacy scalar records have no
                # list fields, so this is a no-op for them.)
                rels = "; ".join(
                    f"{k}: {', '.join(str(x) for x in v[:20] if x)}"
                    for k, v in fields.items()
                    if isinstance(v, list) and v and k != "entityType"
                )
                # Fold scalar role fields (subtitle + the `text` role) into the
                # document text. The `text` role carries the rich human-readable
                # blurb (amenities, rating, hours, editorial, price for Business
                # records) that is otherwise invisible to vector search because
                # it is a scalar string, not a tag/list field. Mirrors the
                # server's runtime composer (_build_searchable_text).
                subtitle = fields.get(role_map.get("subtitle", ""), "")
                text_terms = "; ".join(
                    str(fields[t]) for t in (role_map.get("text", []) or []) if fields.get(t)
                )
                text_str = f"Title: {fields.get(role_map.get('title', ''), '')}. Tags: {tags}"
                if subtitle:
                    text_str += f". Subtitle: {subtitle}"
                if text_terms:
                    text_str += f". {text_terms}"
                if rels:
                    text_str += f". {rels}"
            else:
                text_str = " ".join(str(fields[k]) for k in args.searchable_fields.split(",") if fields.get(k))
            texts.append(f"search_document: {text_str[:1000]}")

        vectors = []
        SUB_CHUNK_SIZE = 1000
        with tqdm(total=len(texts), desc="  ↳ Embedding", unit=" doc") as pbar:
            for si in range(0, len(texts), SUB_CHUNK_SIZE):
                for vec in embed_engine.embed(texts[si: si + SUB_CHUNK_SIZE], batch_size=args.embed_batch):
                    vectors.append(matryoshka(vec, dim) if truncate else vec.tolist())
                    pbar.update(1)
                import gc; gc.collect()

        qdrant.upload_points(
            collection_name=args.collection,
            points=[
                models.PointStruct(
                    id=_str_to_uuid(p["track_id"]),
                    vector=vec,
                    payload={
                        "id":         p["track_id"],
                        "entityType": p.get("entity_type") or p["fields"].get("entityType"),
                        "owner":      p["meta"].get("owner"),
                        "fields":     p["fields"],
                        "meta":       {"manifestCid": p["meta"].get("manifestCid")},
                    }
                )
                for vec, p in zip(vectors, chunk)
            ],
            batch_size=256,
        )

        # Mutate the in-memory checkpoint only — persistence is the caller's job,
        # on its own cadence (see main()'s batched flush). Deterministic point
        # ids above make re-running an unflushed manifest idempotent.
        checkpoint["processed_track_ids"].extend(p["track_id"] for p in chunk)
        checkpoint["manifests"].update({
            p["meta"]["manifestCid"]: p["meta"].get("blockTimestamp")
            for p in chunk
        })

        del texts, vectors
        import gc; gc.collect()

# ---------------------------------------------------------------------------
# PIPELINE EXECUTION
# ---------------------------------------------------------------------------
async def main():
    args = parse_args()

    def _make_qdrant():
        return QdrantClient(
            host=args.qdrant_host, port=args.qdrant_port,
            grpc_port=args.qdrant_grpc_port, prefer_grpc=True, timeout=600
        )

    qdrant = _make_qdrant()

    if args.umap_only:
        if not qdrant.collection_exists(args.collection):
            print(f"[Builder] collection '{args.collection}' does not exist — nothing to project")
            return
        # The map artifact needs the role map (title/subtitle/tags field names) the
        # build wrote; load it from disk since umap-only doesn't run the join.
        umap_role_map = {}
        if os.path.exists(args.role_map_file):
            with open(args.role_map_file) as f:
                umap_role_map = json.load(f)
        write_umap_coords(qdrant, args.collection, args.umap_neighbors, args.umap_min_dist, tmp_dir=args.umap_tmp_dir, reconnect=_make_qdrant,
                          writeback_batch=args.umap_writeback_batch, writeback_sleep=args.umap_writeback_sleep,
                          writeback_workers=args.umap_writeback_workers,
                          writeback_wait=args.umap_writeback_wait,
                          target=args.umap_target, map_file=args.umap_map_file, role_map=umap_role_map)
        ensure_indexes(qdrant, args.collection)
        return

    model_dim = MODEL_DIM_MAP.get(args.embedding_model, 768)
    dim       = min(args.dim, model_dim)
    truncate  = dim < model_dim

    checkpoint = _load_checkpoint(args.checkpoint_file)
    completed_manifest_cids = set(checkpoint["completed_manifest_cids"])
    # processed_track_ids: only non-empty when a previous run crashed mid-manifest
    processed_track_ids = set(checkpoint["processed_track_ids"])

    # ── BUNDLE / VIEW PATH ───────────────────────────────────────────────────
    # Both walk a typed graph and project it; they differ only in the data
    # generator — a single source (bundle) vs. several fused sources (view).
    if args.bundle or args.view:
        if args.view:
            b_name, b_id = args.view.split("=", 1)
            _mode = "View"
        else:
            b_name, b_id = args.bundle.split("=", 1)
            _mode = "Bundle"
        profiles = _load_profiles(args)
        _prof_desc = ", ".join(f"{p['name']}→{p['root_type']}" for p in profiles)
        print(f"\n[Builder] {_mode} mode: '{b_name.strip()}' — projections: {_prof_desc}")

        if args.reset and qdrant.collection_exists(args.collection):
            print(f"[Builder] Resetting collection '{args.collection}'...")
            qdrant.delete_collection(args.collection)
            completed_manifest_cids.clear()
            processed_track_ids.clear()
            checkpoint["completed_manifest_cids"] = []
            checkpoint["processed_track_ids"] = []

        role_map: dict = {}
        if os.path.exists(args.role_map_file):
            with open(args.role_map_file) as f:
                role_map = json.load(f)

        # Persist the checkpoint every CHECKPOINT_EVERY completed manifests rather
        # than after each one. Serializing the full (growing) completed-cid list +
        # manifests dict on every manifest is O(N) per write → O(N²) over a run of
        # ~10k manifests. Batching cuts that to O(N²/CHECKPOINT_EVERY). Safe because
        # point ids are deterministic: a crash that re-runs the manifests since the
        # last flush re-upserts them idempotently (no duplicates), only re-spending
        # the embed compute for at most CHECKPOINT_EVERY manifests.
        CHECKPOINT_EVERY = 50

        def _mark_complete(mcid: str) -> None:
            completed_manifest_cids.add(mcid)
            checkpoint["completed_manifest_cids"] = list(completed_manifest_cids)
            checkpoint["processed_track_ids"] = []
            processed_track_ids.clear()

        def _flush() -> None:
            _save_checkpoint(checkpoint, args.checkpoint_file)

        # Lazy-init: don't load the model into GPU VRAM until we know there's
        # actual new work to do (avoids OOM when everything is already checkpointed).
        embed_engine = None
        any_new = False
        manifest_num = 0
        since_flush  = 0

        if args.view:
            _data_gen = build_view_joined_data(
                args, b_id.strip(), profiles,
                completed_manifest_cids=completed_manifest_cids,
            )
        else:
            _data_gen = build_bundle_joined_data(
                args, b_id.strip(), profiles,
                completed_manifest_cids=completed_manifest_cids,
            )

        async for mcid, records in _data_gen:
            new_records = [r for r in records if r["track_id"] not in processed_track_ids]
            if not new_records:
                # All records in this manifest were already embedded — mark complete.
                _mark_complete(mcid)
                since_flush += 1
                if since_flush >= CHECKPOINT_EVERY:
                    _flush()
                    since_flush = 0
                continue

            # First manifest with real work: set up collection + model.
            if embed_engine is None:
                if not qdrant.collection_exists(args.collection):
                    qdrant.create_collection(
                        args.collection,
                        vectors_config=models.VectorParams(
                            size=dim, distance=models.Distance.COSINE, on_disk=True)
                    )
                ensure_indexes(qdrant, args.collection)
                embed_engine = _init_embed_engine(args)
                print(f"[Builder] model dim={model_dim}, output dim={dim} (truncate={truncate})")

            if not role_map:
                role_map = infer_roles([r["fields"] for r in new_records])
                _save_role_map(role_map, args.role_map_file)
                print(f"[Builder] Role map inferred and saved to {args.role_map_file}")

            manifest_num += 1
            print(f"[Builder] Manifest {manifest_num}: {_cid_to_path(mcid)[:16]}... — {len(new_records)} records")
            await _embed_and_upload(args, qdrant, embed_engine, new_records, role_map, dim, truncate, checkpoint)

            _mark_complete(mcid)
            any_new = True
            since_flush += 1
            if since_flush >= CHECKPOINT_EVERY:
                _flush()
                since_flush = 0

            if args.max_manifests and manifest_num >= args.max_manifests:
                print(f"[Builder] Reached --max-manifests={args.max_manifests}; "
                      f"stopping. Re-run to build the next shards (resumes from checkpoint).")
                break

        # Final flush — persist whatever completed since the last batched write.
        if since_flush:
            _flush()

        if not any_new:
            print("\n[Builder] No new bundle manifests to embed.")

    # ── LEGACY PATH (--schema / --primary) ───────────────────────────────────
    else:
        schemas = {}
        for pair in args.schemas:
            name, s_id = pair.split("=", 1)
            schemas[name.strip()] = s_id.strip()
        primary_key = args.primary or (next(iter(schemas)) if schemas else None)

        if not schemas:
            print("[Builder] No --schema provided and no --bundle. Nothing to do.")
            return
        if primary_key is None:
            print("[Builder] No --primary provided. Nothing to do.")
            return

        if args.reset and qdrant.collection_exists(args.collection):
            print(f"[Builder] Resetting collection '{args.collection}'...")
            qdrant.delete_collection(args.collection)

        if not qdrant.collection_exists(args.collection):
            qdrant.create_collection(
                args.collection,
                vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE, on_disk=True)
            )
        ensure_indexes(qdrant, args.collection)
        embed_engine = _init_embed_engine(args)
        print(f"[Builder] model dim={model_dim}, output dim={dim} (truncate={truncate})")

        manifest_checkpoints = checkpoint.get("manifests", {})
        primary_records = []
        secondary_by_track: dict = {}

        for s_name, s_id in schemas.items():
            print(f"\n[Builder] [1/4] Querying Subgraph events for schema: {s_name}")
            publishes, updates = await _fetch_all_events_async(
                args.subgraph_url, args.graph_api_key, s_id, args.page_size
            )

            cids_meta: dict = {}
            for p in publishes: cids_meta[p["manifestCid"]] = p
            for u in updates:   cids_meta[u["manifestCid"]] = u

            print(f"[Builder] [2/4] Syncing manifests from IPFS Gateway...")
            _gw_h = {"Authorization": f"Bearer {args.ipfs_gateway_key}"} if args.ipfs_gateway_key else {}
            manifests = await fetch_all_ipfs(
                list(cids_meta.keys()), args.ipfs_gateway, args.ipfs_timeout,
                args.concurrency, desc="Manifest Files", headers=_gw_h
            )

            data_cids_to_meta: dict = {}
            for c, json_data in manifests.items():
                if not json_data: continue
                for entry in json_data.get("entries", []):
                    dcid = entry.get("fields", {}).get("dataCid")
                    if dcid: data_cids_to_meta[dcid] = cids_meta[c]

            print(f"[Builder] [3/4] Pulling structural data payloads from IPFS...")
            payloads = await fetch_all_ipfs(
                list(data_cids_to_meta.keys()), args.ipfs_gateway, args.ipfs_timeout,
                args.concurrency, desc="Payload Data", headers=_gw_h
            )

            for dcid, data in payloads.items():
                if not data: continue
                records = data if isinstance(data, list) else [data]
                for r in records:
                    fields = r.get("fields", r) if isinstance(r, dict) else {}
                    t_id = _track_id(fields)
                    meta = data_cids_to_meta[dcid]
                    if s_name == primary_key:
                        primary_records.append({"track_id": t_id, "fields": fields, "meta": meta})
                    else:
                        secondary_by_track.setdefault(t_id, []).append(fields)

            for c, m in cids_meta.items():
                manifest_checkpoints[c] = m["blockTimestamp"]

        if not primary_records:
            print("\n[Builder] No new entries found.")
            if args.umap:
                write_umap_coords(qdrant, args.collection, args.umap_neighbors, args.umap_min_dist, tmp_dir=args.umap_tmp_dir, reconnect=_make_qdrant,
                          writeback_batch=args.umap_writeback_batch, writeback_sleep=args.umap_writeback_sleep,
                          writeback_workers=args.umap_writeback_workers,
                          writeback_wait=args.umap_writeback_wait,
                          target=args.umap_target, map_file=args.umap_map_file, role_map=role_map)
            return

        print(f"\n[Builder] Merging primary and secondary schemas on track ID keys...")
        joined_data = []
        skipped_count = 0
        for item in primary_records:
            t_id, fields, meta = item["track_id"], dict(item["fields"]), item["meta"]
            if t_id in processed_track_ids:
                skipped_count += 1
                continue
            if t_id in secondary_by_track:
                for sec_fields in secondary_by_track[t_id]: fields.update(sec_fields)
            joined_data.append({"track_id": t_id, "fields": fields, "meta": meta})
        print(f"[Builder] Skipped {skipped_count} already-checkpointed track IDs.")

        if not joined_data:
            print("\n[Builder] No new joined records to embed.")
            if args.umap:
                write_umap_coords(qdrant, args.collection, args.umap_neighbors, args.umap_min_dist, tmp_dir=args.umap_tmp_dir, reconnect=_make_qdrant,
                          writeback_batch=args.umap_writeback_batch, writeback_sleep=args.umap_writeback_sleep,
                          writeback_workers=args.umap_writeback_workers,
                          writeback_wait=args.umap_writeback_wait,
                          target=args.umap_target, map_file=args.umap_map_file, role_map=role_map)
            return

        role_map = infer_roles([j["fields"] for j in joined_data])
        _save_role_map(role_map, args.role_map_file)
        print(f"[Builder] Wrote global role map to {args.role_map_file}")

        checkpoint["manifests"] = manifest_checkpoints
        await _embed_and_upload(args, qdrant, embed_engine, joined_data, role_map, dim, truncate, checkpoint)
        # _embed_and_upload mutates the checkpoint in memory; persist it once here.
        _save_checkpoint(checkpoint, args.checkpoint_file)

    # ── POST-PROCESSING ───────────────────────────────────────────────────────
    if args.umap:
        write_umap_coords(qdrant, args.collection, args.umap_neighbors, args.umap_min_dist, tmp_dir=args.umap_tmp_dir, reconnect=_make_qdrant,
                          writeback_batch=args.umap_writeback_batch, writeback_sleep=args.umap_writeback_sleep,
                          writeback_workers=args.umap_writeback_workers,
                          writeback_wait=args.umap_writeback_wait,
                          target=args.umap_target, map_file=args.umap_map_file, role_map=role_map)
    ensure_indexes(qdrant, args.collection)
    print("\n[Builder] All tasks complete.")


if __name__ == "__main__":
    asyncio.run(main())

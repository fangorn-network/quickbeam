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
    parser.add_argument("--root-type", default="Track",
                        help="Bundle root node type — one record is emitted per root node.")
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
    root_type,
    completed_manifest_cids=None,
    owner_filter=None,
    name_filter=None,
    block_gt=None,
):
    completed = completed_manifest_cids or set()

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
        edge_cid  = (m.get("edgeChunk") or {}).get("dataCid")
        if edge_cid is None:
            print(f"[Builder] Skipping manifest {_cid_to_path(mcid)!r} — missing edgeChunk")
            continue

        # Fetch only this manifest's chunks, then free them before moving on.
        # This bounds RAM to one manifest's data at a time.
        print(f"[Builder] Fetching {len(node_cids) + 1} chunks for {_cid_to_path(mcid)[:16]}...")
        chunks = await fetch_all_ipfs(
            node_cids + [edge_cid], args.ipfs_gateway, args.ipfs_timeout,
            args.concurrency, desc="  Chunks", headers=gw_headers
        )

        meta = cids_meta[mcid]
        nodes_by_id: dict = {}
        for ncid in node_cids:
            for node in (chunks.get(ncid) or []):
                nodes_by_id[node["id"]] = node
        edges = chunks.get(edge_cid) or []

        # Index outgoing edges by source node once, so the join below is O(edges)
        # instead of O(nodes × edges) — each root node looks up only its own edges.
        out: dict = {}
        for e in edges:
            out.setdefault(e["from"], []).append(e["to"])

        records = []
        for node in nodes_by_id.values():
            if node.get("type") != root_type:
                continue
            fields = dict(node.get("fields", {}))
            for tid in out.get(node["id"], ()):
                nb = nodes_by_id.get(tid)
                if nb:
                    fields.update(nb.get("fields", {}))
            records.append({
                "track_id": _track_id(fields, prefer=node.get("id")),
                "fields":   fields,
                "meta":     meta,
            })

        del chunks, nodes_by_id, edges, out
        import gc; gc.collect()

        if records:
            yield mcid, records
        else:
            print(f"[Builder] Manifest {_cid_to_path(mcid)!r} had no {root_type!r} nodes — skipping.")

# ---------------------------------------------------------------------------
# EMBED + UPLOAD  (shared by build and watch)
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
                text_str = f"Title: {fields.get(role_map.get('title', ''), '')}. Tags: {tags}"
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
                        "id":     p["track_id"],
                        "owner":  p["meta"].get("owner"),
                        "fields": p["fields"],
                        "meta":   {"manifestCid": p["meta"].get("manifestCid")},
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

    # ── BUNDLE PATH ──────────────────────────────────────────────────────────
    if args.bundle:
        b_name, b_id = args.bundle.split("=", 1)
        print(f"\n[Builder] Bundle mode: '{b_name.strip()}' (root={args.root_type})")

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

        async for mcid, records in build_bundle_joined_data(
            args, b_id.strip(), args.root_type,
            completed_manifest_cids=completed_manifest_cids,
        ):
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

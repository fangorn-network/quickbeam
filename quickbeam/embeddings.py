import certifi
import os
import io
import sys
import argparse
import asyncio
import json
import shlex
import subprocess
import uuid

import numpy as np
from qdrant_client import QdrantClient
from qdrant_client import models
from fastembed import TextEmbedding
from quickbeam.roles import infer_roles, role_map_applies
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
    parser.add_argument(
        "--source", action="append", dest="sources", default=[],
        help="OWNER:NAMESPACE to read and embed, repeatable, e.g. "
             "--source 0x147c24c5...:robinhood. Each source is read independently "
             "via `fangorn read` and tagged with its own owner/namespace in meta — "
             "there is no cross-source identity fusion yet, so multiple sources "
             "land in the same collection as distinct, unmerged records.",
    )
    parser.add_argument(
        "--fangorn-bin", default="fangorn",
        help="How to invoke the fangorn CLI. May be a full command, not just an "
             "executable — e.g. 'dotenvx run -f /abs/.env -- node /abs/lib/cli/cli.js'.",
    )
    parser.add_argument(
        "--root-profile",
        action="append",
        default=[],
        help="Named projection(s) to emit, repeatable: e.g. --root-profile track "
             "--root-profile place. A name matching a built-in (see ROOT_PROFILES) "
             "or --profiles-file profile uses its root_type/include/depth; any "
             "other name is treated as a literal vertex tag with no neighbor-type "
             "filter. If omitted entirely, one profile is auto-derived per distinct "
             "vertex tag actually present in the source, folding in whatever's "
             "within one hop.",
    )
    parser.add_argument("--profiles-file", default=None,
                        help="Optional JSON file of custom/override root profiles, "
                             "merged over the built-in ROOT_PROFILES.")
    parser.add_argument("--max-depth", type=int, default=1,
                        help="Default traversal depth for profiles that don't set one.")
    parser.add_argument("--label-cap", type=int, default=50,
                        help="Max neighbor labels collected per relation group in a projection.")
    parser.add_argument("--node-cap", type=int, default=2000,
                        help="Max nodes a single root's graph walk will visit (cost bound).")
    parser.add_argument("--qdrant-host", default="localhost")
    parser.add_argument("--qdrant-port", type=int, default=6333)
    parser.add_argument("--qdrant-grpc-port", type=int, default=6334)
    parser.add_argument("--checkpoint-file", default="./db/ingest_checkpoint.json")
    parser.add_argument("--collection", default="fangorn")
    parser.add_argument("--searchable-fields", default="auto")
    parser.add_argument("--concurrency", type=int, default=16,
                        help="Parallel `fangorn read` calls across sources.")
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
            ck.setdefault("processed_track_ids", [])
            # sources: "owner:namespace" -> {"head": "0x..."} — last on-chain root
            # seen for that source, so the watcher can skip a poll cycle cheaply.
            ck.setdefault("sources", {})
            return ck
    except Exception:
        return {"processed_track_ids": [], "sources": {}}


def _save_checkpoint(ck, path):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as f:
        json.dump(ck, f)


def _str_to_uuid(s: str) -> str:
    """Deterministic UUID v5 from a track id. Using a stable id (rather than a
    random one) makes re-upserting a source idempotent — a crash that re-runs
    an already-embedded source overwrites the same points instead of creating
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
    #    scales to millions. Genre centroids are aggregated incrementally
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

    def _drain_one():
        nonlocal flushed
        end_row, fut = inflight.popleft()
        fut.result()                  # propagate any terminal failure
        pbar.update(end_row - flushed)
        flushed = end_row
        st["writeback_row"] = flushed
        _save_state(st)

    last_ops = None   # most recent batch — re-applied as a wait=True barrier when async
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
# FANGORN READ — subprocess bridge to `fangorn read`/`fangorn head`
#
# The Fangorn SDK's owner+namespace read primitive (FangornEngine.listNamespace)
# is TypeScript-only; quickbeam shells out to the `fangorn` CLI the same way it
# already does for writes (see pipelines/robinhood.py's publish step).
# ---------------------------------------------------------------------------
def parse_sources(raw_sources: list[str]) -> list[tuple[str, str]]:
    """Parse --source OWNER:NAMESPACE pairs."""
    out = []
    for s in raw_sources:
        owner, sep, namespace = s.partition(":")
        if not sep or not owner.strip() or not namespace.strip():
            raise SystemExit(f"Invalid --source {s!r}, expected OWNER:NAMESPACE")
        out.append((owner.strip(), namespace.strip()))
    return out


def read_source(fangorn_bin: str, owner: str, namespace: str) -> dict:
    """Shell out to `fangorn read <namespace> --owner <owner>` and parse the JSON
    {owner, namespace, head, vertices, edges} it prints to stdout."""
    prefix = shlex.split(fangorn_bin)
    cmd = [*prefix, "read", namespace, "--owner", owner]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError(
            f"fangorn CLI not found (--fangorn-bin {fangorn_bin!r}, resolved to "
            f"{prefix[0]!r}). Install it or pass its full invocation, e.g. "
            f"--fangorn-bin \"dotenvx run -f ~/fangorn/fangorn/.env -- node "
            f"~/fangorn/fangorn/lib/cli/cli.js\".")
    if result.returncode != 0:
        raise RuntimeError(f"fangorn read {owner}:{namespace} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def read_head(fangorn_bin: str, owner: str) -> str:
    """Shell out to `fangorn head <owner>` — the cheap on-chain root check used
    by the watcher to skip a poll cycle with no on-chain change."""
    prefix = shlex.split(fangorn_bin)
    cmd = [*prefix, "head", owner]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError(f"fangorn CLI not found (--fangorn-bin {fangorn_bin!r})")
    if result.returncode != 0:
        raise RuntimeError(f"fangorn head {owner} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def subscribe_cmd(fangorn_bin: str, owner: str, namespace: str) -> list[str]:
    """Argv for `fangorn subscribe <namespace> --owner <owner>` — a light-client
    stream that emits one `NamespaceChange` JSON per line on stdout as commits land
    (status/logs go to stderr; the CLI persists its own resume cursor under
    ./.fangorn). Each line is a self-contained on-chain diff:
        {namespace, owner, commitCid, oldRoot, newRoot, blockNumber,
         addedVertices:[{cid,schemaId,payload}], addedEdges:[{sourceCid,relation,targetCid}],
         removedVertexCids:[cid], removedEdges:[...]}
    This is the push-based replacement for `read_head` polling: the chain tells us
    exactly what changed instead of us re-reading the whole namespace on a timer."""
    return [*shlex.split(fangorn_bin), "subscribe", namespace, "--owner", owner]

# ---------------------------------------------------------------------------
# ROOT PROFILES — graph-as-source-of-truth projections
#
# A namespace is a flat graph (tagged vertices + typed edges, from `fangorn
# read`). A *profile* projects that graph from a chosen root tag into a
# distinct document: walking the graph up to `max_depth` hops and folding the
# neighbor vertices it cares about (`include`) into grouped label lists. The
# SAME namespace can yield a Track view, an Artist view, etc. — each becomes
# its own embedding. Add a profile here (or via --profiles-file) and a new
# semantic view exists with no change to how the data was written.
# ---------------------------------------------------------------------------

# A collection of default/frequently-used root profiles. Root profiles can also
# be defined externally (--profiles-file) or implied by --root-profile naming a
# tag that isn't in this registry (treated as a literal vertex tag, no filter).
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
    # Robinhood-Chain financial graph (robinhood.py): one document per tokenized
    # equity (Asset), folding in that stock's notable on-chain transfer flow so a
    # semantic query matches the equity WITH its live context. Depth 1 — every
    # Transfer hangs directly off its Asset. Each Transfer also embeds as its own
    # record via the `transfer` profile, so a query can hit the event directly.
    "asset": {
        "root_type": "asset", "max_depth": 1,
        "include": ["transfer"],
        # fold each Transfer's verbalized blurb, not its label (the company name)
        "content_fields": ["text"],
    },
    # one document per Transfer, folding in its Asset's blurb (business profile +
    # live stats) so a semantic query matches the flow through what the company IS.
    "transfer": {
        "root_type": "transfer", "max_depth": 1,
        "include": ["asset"],
        "content_fields": ["text"],
    },
}


def load_profiles(args, discovered_tags: set[str] | None = None) -> list[dict]:
    """Resolve --root-profile names (or auto-derive from what's actually in the
    source) into a list of fully-specified profile dicts. Each carries a `name`
    and `root_type` (a vertex tag)."""
    registry = dict(ROOT_PROFILES)
    if args.profiles_file and os.path.exists(args.profiles_file):
        with open(args.profiles_file) as f:
            for name, prof in (json.load(f) or {}).items():
                registry[name.lower()] = {**registry.get(name.lower(), {}), **prof}

    if args.root_profile:
        profiles = []
        for raw in args.root_profile:
            key = raw.strip().lower()
            prof = {"name": key, **registry[key]} if key in registry else \
                   {"name": key, "root_type": raw.strip()}
            prof.setdefault("max_depth", args.max_depth)
            prof.setdefault("include", None)
            profiles.append(prof)
        return profiles

    # Zero-config default: one profile per distinct vertex tag actually present
    # in this source, folding in whatever's within one hop (no curated `include`
    # filter — we don't know the shape of arbitrary data ahead of time).
    tags = sorted(discovered_tags or [])
    return [{"name": t.lower(), "root_type": t, "max_depth": args.max_depth, "include": None}
            for t in tags]


def _node_label(node: dict) -> str:
    """Human label for a node — title / name / label, whichever the node carries."""
    f = node.get("fields", {}) or {}
    for k in ("title", "name", "label"):
        v = f.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _node_content(node: dict, extra_keys=()) -> str:
    """Folded value for a neighbour node. Prefer a free-form *content* field (a
    Review `body`, an event/summary `description`) so that text becomes searchable
    when the node is folded into a root document — without this a Review folds in
    only its "<author> on <business>" title and the body ("best tacos in town") is
    silently dropped. Content-less nodes (Category, Locality, Reviewer) have none
    of these fields and fall back to their title label, so they don't bloat the doc.

    `extra_keys` (a profile's `content_fields`) are consulted first — e.g. the
    robinhood graph carries its verbalized blurb in `text`, which would otherwise
    lose to the label ("NVIDIA") and fold every transfer down to the company name."""
    f = node.get("fields", {}) or {}
    for k in (*(extra_keys or ()), "body", "summary", "description"):
        v = f.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return _node_label(node)


def _group_key(type_name: str) -> str:
    """Node type/tag → camelCase plural field name. Artist→artists, Work→works,
    Place→places, ReleaseGroup→releaseGroups."""
    t = (type_name[:1].lower() + type_name[1:]) if type_name else type_name
    if t.endswith("y"):
        return t[:-1] + "ies"
    if t.endswith(("s", "x", "z", "ch", "sh")):
        return t + "es"
    return t + "s"


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


def project_vertex(root: dict, nodes_by_id: dict, adj: dict, profile: dict, defaults: dict) -> dict:
    """Project one root vertex into a profile document: walk the graph and fold
    included neighbor vertices into grouped, deduped, capped label lists."""
    rt = profile.get("root_type") or root.get("type")
    fields = dict(root.get("fields", {}))

    depth = int(profile.get("max_depth", defaults["max_depth"]))
    label_cap = int(profile.get("label_cap", defaults["label_cap"]))
    node_cap = int(profile.get("node_cap", defaults["node_cap"]))
    include = profile.get("include")
    include_set = set(include) if include else None

    groups: dict = {}
    content_fields = profile.get("content_fields") or ()
    for nid, _depth in _walk_graph(root["id"], adj, depth, node_cap):
        nb = nodes_by_id.get(nid)
        if not nb:
            continue
        t = nb.get("type")
        if include_set is not None and t not in include_set:
            continue
        value = _node_content(nb, content_fields)
        if value:
            groups.setdefault(_group_key(t), []).append(value)

    for k, vals in groups.items():
        fields[k] = list(dict.fromkeys(vals))[:label_cap]  # dedupe (order-preserving) + cap
    fields["entityType"] = rt
    return fields


def project_source(owner: str, namespace: str, contents: dict, profiles: list[dict], args) -> list[dict]:
    """Project one source's {vertices, edges} (from `fangorn read`) into
    root-profile documents. Each vertex's own content-addressed CID is used
    directly as its track_id — already a globally unique, stable identifier, so
    no field-sniffing heuristic is needed to derive one."""
    nodes_by_id = {
        v["cid"]: {"id": v["cid"], "type": v["schemaId"], "fields": v["payload"]}
        for v in contents.get("vertices", [])
    }
    adj: dict = {}
    for e in contents.get("edges", []):
        adj.setdefault(e["sourceCid"], []).append(e["targetCid"])
        adj.setdefault(e["targetCid"], []).append(e["sourceCid"])

    defaults = {"max_depth": args.max_depth, "label_cap": args.label_cap, "node_cap": args.node_cap}
    meta = {"owner": owner, "namespace": namespace}
    records = []
    for prof in profiles:
        rt = prof["root_type"]
        n_roots = 0
        for node in nodes_by_id.values():
            if node["type"] != rt:
                continue
            fields = project_vertex(node, nodes_by_id, adj, prof, defaults)
            records.append({
                "track_id":    node["id"],
                "entity_type": rt,
                "fields":      fields,
                "meta":        meta,
            })
            n_roots += 1
        if n_roots == 0:
            print(f"[Builder] profile {prof['name']!r}: no {rt!r} vertices in {owner}:{namespace}")
    return records

# ---------------------------------------------------------------------------
# EMBED + UPLOAD (shared by build and watch)
#
# Embeds records in SAVE_BATCH_SIZE chunks and uploads to Qdrant.
# Writes partial progress to checkpoint after each batch for crash recovery.
# ---------------------------------------------------------------------------
def compose_document_text(fields: dict, role_map: dict,
                          searchable_fields: str = "auto") -> str:
    """Build the `search_document:`-prefixed text embedded for one record — the
    single source of truth for the document side of retrieval (the query side is
    `search_query:` + the same nomic model). Mirrors the server's runtime composer
    (`_build_searchable_text`). Keeping this ONE function means the live embed loop
    and any re-embed/backfill produce byte-identical text for the same input.

    With `searchable_fields="auto"` the text is driven by the role map: the `title`,
    `tags`, `subtitle`, and (crucially) the `text` role — the rich human-readable
    blurb that is otherwise invisible to vector search — plus any projected neighbor
    lists folded in for graph context. A correct role map is load-bearing here: a
    stale/foreign map collapses every record to the same empty `"Title: . Tags:"`
    text (see `roles.role_map_applies`)."""
    if searchable_fields != "auto":
        text_str = " ".join(str(fields[k]) for k in searchable_fields.split(",")
                            if fields.get(k))
        return f"search_document: {text_str[:1000]}"

    tags = " ".join(
        fields.get(t, "") if isinstance(fields.get(t), str) else ""
        for t in role_map.get("tags", [])
    )
    # Fold any projected neighbor lists (artists, events, …) into the document text
    # so each projection embeds its full graph context — the whole point of root
    # profiles. (Legacy scalar records have no list fields, so this is a no-op.)
    rels = "; ".join(
        f"{k}: {', '.join(str(x) for x in v[:20] if x)}"
        for k, v in fields.items()
        if isinstance(v, list) and v and k != "entityType"
    )
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
    return f"search_document: {text_str[:1000]}"


async def _embed_and_upload(args, qdrant, embed_engine, records, role_map, dim, truncate, checkpoint):
    SAVE_BATCH_SIZE = 5000
    n_batches = max(1, (len(records) + SAVE_BATCH_SIZE - 1) // SAVE_BATCH_SIZE)
    for i in range(0, len(records), SAVE_BATCH_SIZE):
        chunk = records[i: i + SAVE_BATCH_SIZE]
        if n_batches > 1:
            print(f"  [Embed] sub-batch {i // SAVE_BATCH_SIZE + 1}/{n_batches} ({len(chunk)} records)")

        texts = [compose_document_text(item["fields"], role_map, args.searchable_fields)
                 for item in chunk]

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
                        "meta":       {"namespace": p["meta"].get("namespace")},
                    }
                )
                for vec, p in zip(vectors, chunk)
            ],
            batch_size=256,
        )

        # Mutate the in-memory checkpoint only — persistence is the caller's job,
        # on its own cadence. Deterministic point ids above make re-running an
        # unflushed source idempotent.
        checkpoint["processed_track_ids"].extend(p["track_id"] for p in chunk)

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
    processed_track_ids = set(checkpoint["processed_track_ids"])

    sources = parse_sources(args.sources)
    if not sources:
        print("[Builder] No --source provided. Nothing to do.")
        return

    if args.reset and qdrant.collection_exists(args.collection):
        print(f"[Builder] Resetting collection '{args.collection}'...")
        qdrant.delete_collection(args.collection)
        processed_track_ids.clear()
        checkpoint["processed_track_ids"] = []
        checkpoint["sources"] = {}

    role_map: dict = {}
    if os.path.exists(args.role_map_file):
        with open(args.role_map_file) as f:
            role_map = json.load(f)

    embed_engine = None
    any_new = False

    for owner, namespace in sources:
        print(f"\n[Builder] Reading {owner}:{namespace} via `fangorn read`...")
        contents = read_source(args.fangorn_bin, owner, namespace)
        discovered_tags = {v["schemaId"] for v in contents.get("vertices", [])}
        profiles = load_profiles(args, discovered_tags)
        _prof_desc = ", ".join(f"{p['name']}→{p['root_type']}" for p in profiles)
        print(f"[Builder] {owner}:{namespace} — {len(contents.get('vertices', []))} vertices, "
              f"{len(contents.get('edges', []))} edges — projections: {_prof_desc}")

        records = project_source(owner, namespace, contents, profiles, args)
        new_records = [r for r in records if r["track_id"] not in processed_track_ids]
        if not new_records:
            print(f"[Builder] {owner}:{namespace} — nothing new.")
            checkpoint.setdefault("sources", {})[f"{owner}:{namespace}"] = {"head": contents.get("head")}
            continue

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

        # (Re)infer when there's no map, OR when the loaded map doesn't apply to
        # these records — a stale ./db/role_map.json from a different corpus would
        # otherwise make every record embed the same empty "Title: . Tags:" text,
        # collapsing all vectors to one point (identical, undiscriminating scores).
        record_fields = [r["fields"] for r in new_records]
        if not role_map or not role_map_applies(role_map, record_fields):
            if role_map:
                print("[Builder] loaded role map does not match these records "
                      "(stale/foreign) — re-inferring")
            role_map = infer_roles(record_fields)
            _save_role_map(role_map, args.role_map_file)
            print(f"[Builder] Role map inferred and saved to {args.role_map_file}")

        await _embed_and_upload(args, qdrant, embed_engine, new_records, role_map, dim, truncate, checkpoint)

        processed_track_ids.update(r["track_id"] for r in new_records)
        checkpoint["processed_track_ids"] = list(processed_track_ids)
        checkpoint.setdefault("sources", {})[f"{owner}:{namespace}"] = {"head": contents.get("head")}
        _save_checkpoint(checkpoint, args.checkpoint_file)
        any_new = True

    if not any_new:
        print("\n[Builder] No new records to embed.")

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

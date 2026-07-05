"""2-D UMAP projection of the whole collection → catalog-map artifact or px/py payloads.

`write_umap_coords` is built to survive a multi-hour run on a flaky laptop against
10M x 256-d: memory-bounded (on-disk memmaps), transient-fault tolerant (retry +
reconnect), and resumable (checkpointed across pull/transform/writeback stages).
"""
import numpy as np
from qdrant_client import models
from tqdm import tqdm

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

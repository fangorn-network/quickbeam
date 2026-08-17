"""Embed a staged volume into Qdrant, streaming.

WHY NOT `quickbeam data prebake`. It holds four full copies of the corpus live at
once (records, texts, vectors, and a points list that duplicates every payload) and
embeds everything before upserting anything. The killer is `matryoshka(...).tolist()`
— a Python list of 256 Python floats costs ~8.2 KB, so 1.9M vectors is 15.6 GB in
that one list. Measured: RSS 5.2 -> 8.0 -> 12.9 GB over two hours, on course to OOM
about a third of the way through. This does the same work with bounded memory.

Three differences, each measured:
  * vectors stay float32 numpy, never Python lists          (8.2 KB -> 1 KB each)
  * embed -> upsert -> drop, one chunk at a time            (bounded RSS)
  * documents sorted by length inside each chunk, batch 64  (7.6x, and no VRAM OOM
    from a batch that happens to hold a 1000-char document)

Text composition is imported from prebake so the embedded bytes stay identical to
the path this replaces.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, "/home/coleman/fangorn/quickbeam")

import numpy as np  # noqa: E402
from qdrant_client import QdrantClient, models  # noqa: E402

from quickbeam.ingest.identity import _str_to_uuid, matryoshka  # noqa: E402
from quickbeam.pipelines.prebake import _compose_text  # noqa: E402

STEMS = ["artists", "tracks", "playlists", "genres", "moods", "tags"]
ENTITY = {"artists": "Artist", "tracks": "Track", "playlists": "Playlist",
          "genres": "Genre", "moods": "Mood", "tags": "Tag"}


def records(input_dir, volume):
    """Yield (name, entity, fields) one stem file at a time, dropping each parsed
    file before opening the next so peak RSS is one file, not the corpus."""
    for stem in STEMS:
        path = os.path.join(input_dir, f"volume_{volume}_{stem}.json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        print(f"[embed] {stem}: {len(rows)}", flush=True)
        for r in rows:
            yield r["name"], ENTITY[stem], r.get("fields") or {}
        del rows
        gc.collect()


def chunks(it, n):
    buf = []
    for x in it:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", required=True)
    ap.add_argument("--volume", type=int, required=True)
    ap.add_argument("--collection", required=True)
    ap.add_argument("--owner", required=True)
    ap.add_argument("--role-map-file", required=True)
    ap.add_argument("--dim", type=int, default=256)
    ap.add_argument("--chunk", type=int, default=50_000, help="Records per embed+upsert cycle.")
    ap.add_argument("--batch", type=int, default=64, help="Unused; see --char-budget.")
    ap.add_argument("--char-budget", type=int, default=24_000,
                    help="Max (batch x longest-doc chars) per forward pass. The knob "
                         "that keeps a run of long documents inside the CUDA arena.")
    ap.add_argument("--batch-cap", type=int, default=128)
    ap.add_argument("--batch-min", type=int, default=4)
    ap.add_argument("--qdrant", default="localhost")
    args = ap.parse_args()

    role_map = json.load(open(args.role_map_file))
    client = QdrantClient(host=args.qdrant, port=6333, timeout=300)
    # Use quickbeam's engine builder, NOT a bare TextEmbedding. It sets three things
    # that a direct construction silently loses:
    #   max_length=256              — without it nomic allows sequences up to 8192,
    #                                 i.e. up to 32x the attention cost per document.
    #                                 THIS is what ballooned the CUDA arena to 10 GB
    #                                 and OOM'd on a 202 MB allocation.
    #   arena_extend_strategy       — kSameAsRequested keeps the arena dense instead
    #                                 of rounding every allocation to a power of two.
    #   ResilientEmbedder           — halves the batch on OOM and finally falls back
    #                                 to CPU, so a bad batch cannot kill an hour's work.
    import argparse as _a

    from quickbeam.ingest.embed import ResilientEmbedder
    model = ResilientEmbedder(_a.Namespace(embedding_model="nomic-ai/nomic-embed-text-v1.5"))
    print("engine: quickbeam ResilientEmbedder (max_length=256, kSameAsRequested)", flush=True)

    done, t0 = 0, time.time()
    for chunk in chunks(records(args.input_dir, args.volume), args.chunk):
        texts = [_compose_text(f, role_map) for (_, _, f) in chunk]
        # Sort WITHIN the chunk: fastembed pads each batch to its longest member, and
        # this corpus is p50 ~113 chars against p99 ~1017. Unsorted, roughly half the
        # batches run ~9x wider than needed — and on GPU that is also what pushes a
        # batch past gpu_mem_limit and into a BiasSoftmax allocation failure.
        order = sorted(range(len(chunk)), key=lambda i: len(texts[i]))
        # ADAPTIVE BATCH. A fixed batch_size is wrong in both directions here: 64 is
        # wasteful for a 113-char tag blurb and fatal for a run of 1000-char track
        # descriptions, which is exactly what the length sort concentrates at the end
        # of every chunk. Volume 2 (short vocabulary docs) survived batch 64; volume 1
        # died on `MatMul` failing to allocate 180 MB inside the 3 GiB arena.
        # Budget on characters, because attention cost scales with sequence length.
        vecs = []
        i = 0
        while i < len(order):
            longest = len(texts[order[i]])
            for j in range(i, min(i + args.batch_cap, len(order))):
                longest = max(longest, len(texts[order[j]]))
                if (j - i + 1) * longest > args.char_budget:
                    break
            n = max(args.batch_min, min(args.batch_cap, max(1, args.char_budget // max(longest, 1))))
            sub = [texts[k] for k in order[i:i + n]]
            # ResilientEmbedder.embed materialises its result, so this is a list.
            vecs.extend(model.embed(sub, batch_size=len(sub)))
            i += n

        points = []
        for slot, i in enumerate(order):
            name, entity, fields = chunk[i]
            v = np.asarray(matryoshka(vecs[slot], args.dim), dtype=np.float32)
            points.append(models.PointStruct(
                id=_str_to_uuid(name), vector=v.tolist(),
                payload={"id": name, "entityType": entity, "owner": args.owner,
                         "fields": fields, "meta": {"manifestCid": "local-prebake"}}))
        client.upload_points(collection_name=args.collection, points=points, wait=True)
        done += len(points)
        del points, vecs, texts, chunk
        gc.collect()
        el = time.time() - t0
        print(f"[embed] {done} upserted  {done/max(el,1e-9):.0f}/s", flush=True)

    print(f"[embed] volume {args.volume} complete: {done} points in {time.time()-t0:.0f}s",
          flush=True)


if __name__ == "__main__":
    main()

"""The embedding side of ingest: model init, GPU-OOM resilience, document-text
composition, Qdrant payload indexes, and the embed→upload loop shared by build+watch.

`compose_document_text` is the single source of truth for the *document* side of
retrieval — the server's query-side composer must mirror it byte-for-byte.
"""
import os

from fastembed import TextEmbedding
from qdrant_client import models
from tqdm import tqdm

from quickbeam.ingest.identity import _str_to_uuid, matryoshka

MODEL_DIM_MAP = {
    "nomic-ai/nomic-embed-text-v1.5": 768,
    "BAAI/bge-small-en-v1.5": 384,
    "sentence-transformers/all-MiniLM-L6-v2": 384
}

# TODO: This should be more modular. We do not always want 'text embeddings'.
# ---------------------------------------------------------------------------
# EMBED ENGINE (GPU-OOM resilient)
# ---------------------------------------------------------------------------
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
        # this is schizo
        ("fields.content",  models.TextIndexParams(type="text", tokenizer=models.TokenizerType.WORD, lowercase=True)),
        ("fields.filename", models.TextIndexParams(type="text", tokenizer=models.TokenizerType.WORD, lowercase=True)),
    ]
    for field, schema in specs:
        try:
            qdrant.create_payload_index(collection_name=collection, field_name=field, field_schema=schema)
            print(f"[index] created {field}")
        except Exception as e:
            print(f"[index] {field} already present ({type(e).__name__})")


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
                        # Carry the on-chain source CID so served results have real
                        # provenance (the `source_cid` the MCP layer surfaces).
                        "meta":       {"namespace": p["meta"].get("namespace"),
                                       "sourceCid": p["meta"].get("sourceCid")},
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

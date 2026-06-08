from qdrant_client import QdrantClient

src = QdrantClient(host="localhost", port=6334, prefer_grpc=True)
dst = QdrantClient(
    url="https://c741b23e-fe75-4f00-8f6e-53709c011371.us-east-1-1.aws.cloud.qdrant.io:6334",
    api_key="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhY2Nlc3MiOiJtIiwic3ViamVjdCI6ImFwaS1rZXk6NDVkZjI4Y2YtNDhiMS00ZGY2LWExMDYtNWZiMzBiNGE4NDQzIn0.Rp90WUQjOuw410DXWzgdTYEb-9Jw5Y5Ob_DjFYfJyZU",
    prefer_grpc=True,
)

COLLECTION = "fangorn"
BATCH      = 100

# Mirror collection config from source
src_info = src.get_collection(COLLECTION)
vec_cfg  = src_info.config.params.vectors

if not dst.collection_exists(COLLECTION):
    from qdrant_client import models
    dst.create_collection(COLLECTION, vectors_config=vec_cfg)
    print("created collection on cloud")

offset = None
total  = 0

while True:
    records, next_offset = src.scroll(
        collection_name=COLLECTION,
        limit=BATCH,
        offset=offset,
        with_payload=True,
        with_vectors=True,
    )
    if not records:
        break

    from qdrant_client.models import PointStruct
    points = [
        PointStruct(id=pt.id, vector=pt.vector, payload=pt.payload)
        for pt in records
    ]
    dst.upsert(collection_name=COLLECTION, points=points, wait=True)
    total += len(points)
    print(f"  {total} points migrated", end="\r", flush=True)

    if next_offset is None:
        break
    offset = next_offset

print(f"\ndone — {total} points")
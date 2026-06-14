#!/usr/bin/env python3
"""
export_bundle.py  --src http://localhost:8080  --out bundle.ndjson
                  --src http://localhost:8080  --out embeddings.ndjson --embeddings-only
"""
import argparse, json, requests

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src",              required=True)
    ap.add_argument("--out",              default="bundle.ndjson")
    ap.add_argument("--owner",            default=None)
    ap.add_argument("--embeddings-only",  action="store_true", default=False,
                    help="Export only track_id + embedding, no fields or metadata")
    args = ap.parse_args()

    src_url = f"{args.src.rstrip('/')}/bundle/export"
    params  = {"owner": args.owner} if args.owner else {}

    total = 0
    with requests.get(src_url, params=params, stream=True) as resp:
        resp.raise_for_status()
        with open(args.out, "w") as f:
            for raw in resp.iter_lines():
                if not raw:
                    continue
                if args.embeddings_only:
                    row     = json.loads(raw)
                    out_row = {
                        "track_id":  row["track_id"],
                        "embedding": row["embedding"],
                    }
                    f.write(json.dumps(out_row, separators=(",", ":")) + "\n")
                else:
                    f.write(raw.decode() + "\n")
                total += 1
                if total % 1000 == 0:
                    print(f"  {total} points written", end="\r", flush=True)

    print(f"\ndone — {total} points -> {args.out}")

if __name__ == "__main__":
    main()
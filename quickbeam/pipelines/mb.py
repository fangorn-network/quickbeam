import os
import re
import json
import argparse
import hashlib
import threading
import requests
import tarfile
import lzma
from typing import Iterator, Dict, Any

# ===========================================================================
# 1. CONFIGURATION
# ===========================================================================
NETWORK_CHUNK_SIZE = 2 * 1024 * 1024    # 2 MB download chunks
DOWNLOAD_TIMEOUT   = (15, 120)           # (connect, read) seconds

MB_DUMP_INDEX = "https://data.metabrainz.org/pub/musicbrainz/data/json-dumps/"


def _latest_dump_url() -> str:
    """Scrape the MusicBrainz dump index and return the URL of the newest release.tar.xz."""
    resp = requests.get(MB_DUMP_INDEX, timeout=15)
    resp.raise_for_status()
    # Directory listing contains lines like: href="20260603-001002/"
    dirs = re.findall(r'href="(\d{8}-\d{6})/"', resp.text)
    if not dirs:
        raise RuntimeError(
            f"Could not find any dated dump directories at {MB_DUMP_INDEX}\n"
            "Pass --dump-url explicitly with the full release.tar.xz URL."
        )
    latest = sorted(dirs)[-1]
    url = f"{MB_DUMP_INDEX}{latest}/release.tar.xz"
    print(f"[mb] latest dump: {url}")
    return url


def parse_args():
    parser = argparse.ArgumentParser(
        description="Process a MusicBrainz JSON dump into Fangorn-compatible JSONL.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dump-url", default=None,
        help=f"URL of release.tar.xz. Omit to auto-discover the latest from {MB_DUMP_INDEX}",
    )
    parser.add_argument("--volume",       type=int,   default=1,         help="Output volume number suffix")
    parser.add_argument("--output-dir",               default="./stage_volumes", help="Output directory")
    parser.add_argument("--target-count", type=int,   default=1_000_000, help="Max tracks to extract")
    parser.add_argument("--min-tag-votes",type=int,   default=1,         help="Minimum tag vote count to include a tag")
    parser.add_argument("--no-require-taxonomy", action="store_true", default=False,
                        help="Include tracks that have no genre tags (excluded by default)")
    parser.add_argument("--connections", type=int, default=4,
                        help="Parallel HTTP connections for the initial dump download (1 = single-connection)")
    return parser.parse_args()


# ===============================r============================================
# 2. RESUMABLE DOWNLOADER
#    Parallel-chunk mode (default, --connections N): pre-allocates the file
#    and downloads N byte-ranges simultaneously. A .parts sidecar tracks which
#    chunks finished so an interrupted run can skip them on restart.
#    Single-connection fallback (--connections 1): plain streaming with Range
#    resume, same as before.
# ===========================================================================
def _progress_bar(written: int, total: int) -> None:
    pct   = written / total * 100
    filled = int(pct / 2)
    bar   = "█" * filled + "░" * (50 - filled)
    print(f"   [{bar}] {pct:5.1f}%  {written/1e9:.3f}/{total/1e9:.2f} GB",
          end="\r", flush=True)


def _download_chunk(
    url: str, dest: str, start: int, end: int,
    chunk_idx: int, total: int,
    shared: dict, lock: threading.Lock,
) -> None:
    headers = {"User-Agent": "Fangorn-Pipeline/1.0", "Range": f"bytes={start}-{end}"}
    try:
        with requests.get(url, headers=headers, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
            if r.status_code not in (200, 206):
                print(f"\n⚠️  chunk {chunk_idx} HTTP {r.status_code}", flush=True)
                with lock: shared["failed"].add(chunk_idx)
                return
            with open(dest, "r+b") as f:
                f.seek(start)
                for data in r.iter_content(chunk_size=NETWORK_CHUNK_SIZE):
                    if data:
                        f.write(data)
                        with lock:
                            shared["written"] += len(data)
                            _progress_bar(shared["written"], total)
        with lock:
            shared["done"].add(chunk_idx)
    except Exception as exc:
        print(f"\n⚠️  chunk {chunk_idx} error: {exc}", flush=True)
        with lock:
            shared["failed"].add(chunk_idx)


def download_with_resume(url: str, dest: str, connections: int = 4) -> None:
    headers  = {"User-Agent": "Fangorn-Pipeline/1.0"}
    existing = os.path.getsize(dest) if os.path.exists(dest) else 0

    head = requests.head(url, headers=headers, timeout=DOWNLOAD_TIMEOUT, allow_redirects=True)
    if head.status_code != 200:
        raise RuntimeError(f"HEAD {url} → HTTP {head.status_code}")

    total          = int(head.headers.get("Content-Length", 0))
    accepts_ranges = head.headers.get("Accept-Ranges", "none").lower() == "bytes"

    if total and existing == total:
        print(f"✅ Already downloaded ({total/1e9:.2f} GB): {dest}")
        return

    # ── Parallel chunked path ────────────────────────────────────────────────
    if accepts_ranges and total and connections > 1:
        state_path = dest + ".parts"
        done_set: set[int] = set()
        if os.path.exists(state_path):
            try:
                with open(state_path) as f:
                    done_set = set(json.load(f).get("done", []))
            except Exception:
                pass

        chunk_size = total // connections
        bounds = [
            (i * chunk_size, (i + 1) * chunk_size - 1 if i < connections - 1 else total - 1)
            for i in range(connections)
        ]
        pending = [(i, s, e) for i, (s, e) in enumerate(bounds) if i not in done_set]

        if not pending:
            print(f"✅ Already downloaded ({total/1e9:.2f} GB): {dest}")
            return

        # Pre-allocate a sparse file; on resume just verify the size matches
        if not os.path.exists(dest) or os.path.getsize(dest) != total:
            if done_set:
                print("⚠️  File size mismatch — resetting chunk state")
                done_set.clear()
                pending = list(enumerate(bounds))  # type: ignore[assignment]
                pending = [(i, s, e) for i, (s, e) in enumerate(bounds)]
            print(f"📥 Downloading {total/1e9:.2f} GB "
                  f"with {connections} parallel connections → {dest}")
            with open(dest, "wb") as f:
                f.truncate(total)
        else:
            print(f"⏩ Resuming {connections}-connection download "
                  f"({len(done_set)}/{connections} chunks done)...")

        bytes_done   = total - sum(e - s + 1 for _, s, e in pending)
        shared       = {"written": bytes_done, "done": set(), "failed": set()}
        lock         = threading.Lock()

        threads = [
            threading.Thread(
                target=_download_chunk,
                args=(url, dest, s, e, i, total, shared, lock),
            )
            for i, s, e in pending
        ]
        for t in threads: t.start()
        for t in threads: t.join()
        print()  # end progress line

        new_done = done_set | shared["done"]
        if shared["failed"]:
            with open(state_path, "w") as f:
                json.dump({"done": list(new_done)}, f)
            raise RuntimeError(
                f"Chunks {sorted(shared['failed'])} failed. Re-run to retry."
            )
        if os.path.exists(state_path):
            os.remove(state_path)

    # ── Single-connection fallback ───────────────────────────────────────────
    else:
        if existing and total and existing > total:
            print("⚠️  Local file larger than remote — deleting and restarting")
            os.remove(dest)
            existing = 0

        if existing and accepts_ranges:
            pct = existing / total * 100 if total else 0
            print(f"⏩ Resuming from {existing/1e9:.2f} GB / {total/1e9:.2f} GB ({pct:.1f}%)")
            req_headers = {**headers, "Range": f"bytes={existing}-"}
            mode = "ab"
        else:
            if existing and not accepts_ranges:
                print("⚠️  Server doesn't support Range — restarting from zero")
                os.remove(dest)
                existing = 0
            size_str = f"{total/1e9:.2f} GB" if total else "unknown size"
            print(f"📥 Downloading {size_str} → {dest}")
            req_headers = headers
            mode = "wb"

        with requests.get(url, headers=req_headers, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
            if r.status_code not in (200, 206):
                raise RuntimeError(f"GET → HTTP {r.status_code}")
            if r.status_code == 200 and mode == "ab":
                print("⚠️  Server sent 200 instead of 206 — restarting")
                os.remove(dest)
                existing = 0
                mode = "wb"
            written = existing
            with open(dest, mode) as f:
                for chunk in r.iter_content(chunk_size=NETWORK_CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
                        if total:
                            _progress_bar(written, total)
                        else:
                            print(f"   📡 {written/1e9:.3f} GB...", end="\r", flush=True)
        print()

    final = os.path.getsize(dest)
    if total and final != total:
        raise RuntimeError(f"Incomplete: {final:,} / {total:,} bytes. Re-run to resume.")
    print(f"✅ Download complete: {final/1e9:.2f} GB")

# ===========================================================================
# 3. BUFFERED STREAM WRAPPER
#    Used to pipe the local XZ file into lzma without loading it all into RAM.
#    lzma.open() calls read(n) with specific sizes — must honor exactly.
# ===========================================================================
class BufferedFileWrapper:
    """Thin wrapper that reports progress while lzma reads the local file."""
    def __init__(self, path: str):
        self._f = open(path, "rb")
        self._size = os.path.getsize(path)
        self._pos = 0
        self._last_report = 0

    def read(self, size=-1):
        data = self._f.read(size)
        self._pos += len(data)
        if self._pos - self._last_report >= 500 * 1024 * 1024:
            pct = self._pos / self._size * 100
            print(f"   🗜️  Decompressing: {self._pos/1e9:.2f}/{self._size/1e9:.2f} GB ({pct:.0f}%)",
                  flush=True)
            self._last_report = self._pos
        return data

    def readable(self):  return True
    def writable(self):  return False
    def seekable(self):  return False
    def close(self):     self._f.close()
    def __enter__(self): return self
    def __exit__(self, *a): self.close()

# ===========================================================================
# 4. RELEASE LINE ITERATOR
# ===========================================================================
def _iter_release_lines(xz_path: str) -> Iterator[bytes]:
    file_size = os.path.getsize(xz_path)
    print(f"📂 Opening: {xz_path}  ({file_size/1e9:.2f} GB)")

    if file_size < 1_000_000_000:
        raise RuntimeError(
            f"Local file is only {file_size/1e6:.0f} MB — expected ~23 GB for release.tar.xz.\n"
            f"Delete {xz_path} and re-run to trigger a fresh download."
        )

    with BufferedFileWrapper(xz_path) as raw:
        with lzma.open(raw, format=lzma.FORMAT_AUTO) as xz_stream:
            with tarfile.open(fileobj=xz_stream, mode="r|") as tar:
                for member in tar:
                    if member.name in ("mbdump/release", "release"):
                        print(f"   🎯 Member: '{member.name}' — streaming lines...")
                        f = tar.extractfile(member)
                        if f is None:
                            raise RuntimeError(f"extractfile returned None for {member.name}")
                        n = 0
                        for raw_line in f:
                            raw_line = raw_line.strip()
                            if raw_line:
                                n += 1
                                if n % 1_000_000 == 0:
                                    print(f"   📄 {n/1e6:.0f}M lines...", flush=True)
                                yield raw_line
                        print(f"\n   ✅ {n:,} raw release lines read")
                        return
                raise RuntimeError("mbdump/release not found in archive.")

# ===========================================================================
# 5. HELPERS
# ===========================================================================
def _parse_artist_credit(credits: list) -> str:
    parts = []
    for seg in credits:
        if not isinstance(seg, dict):
            continue
        if "artist" in seg:
            name = seg.get("name") or seg["artist"].get("name", "")
            if name:
                parts.append(name)
            jp = seg.get("joinphrase", "")
            if jp and parts:
                parts[-1] += jp
    return "".join(parts).strip()


def _pick_best_release(releases: list) -> dict:
    if not releases:
        return {}
    TYPE_RANK = {"Album": 0, "EP": 1, "Single": 2, "Other": 3, "": 4}
    def sort_key(r):
        rg = r.get("release-group") or {}
        score = TYPE_RANK.get(rg.get("primary-type", ""), 4)
        date  = (r.get("date") or "")[:10].ljust(10, "9")
        return (score, date)
    return sorted(releases, key=sort_key)[0]


def _collect_tags(obj: dict, min_tag_votes: int = 1) -> list:
    seen = {}
    for field in ("genres", "tags"):
        for tag_obj in (obj.get(field) or []):
            if not isinstance(tag_obj, dict):
                continue
            name = tag_obj.get("name", "").strip()
            if not name:
                continue
            seen[name] = seen.get(name, 0) + tag_obj.get("count", 1)
    filtered = [(n, c) for n, c in seen.items() if c >= min_tag_votes]
    filtered.sort(key=lambda x: -x[1])
    return [n.title() for n, _ in filtered]


def _collect_contributors(rec: dict, release: dict) -> list:
    contributors = []
    seen_ids: set = set()

    # Primary performing artists from recording artist-credit (fall back to release)
    for seg in (rec.get("artist-credit") or release.get("artist-credit") or []):
        if not isinstance(seg, dict) or "artist" not in seg:
            continue
        artist = seg["artist"]
        artist_id = artist.get("id")
        name = seg.get("name") or artist.get("name")
        if not name:
            continue
        if artist_id and artist_id in seen_ids:
            continue
        if artist_id:
            seen_ids.add(artist_id)
        contributors.append({"role": "artist", "name": name, "id": artist_id})

    # Additional contributors from recording relations (producers, engineers, etc.)
    for rel_entry in (rec.get("relations") or []):
        if not isinstance(rel_entry, dict):
            continue
        role = rel_entry.get("type")
        artist = rel_entry.get("artist")
        if not artist or not isinstance(artist, dict):
            continue
        artist_id = artist.get("id")
        name = artist.get("name")
        if not name:
            continue
        if artist_id and artist_id in seen_ids:
            continue
        if artist_id:
            seen_ids.add(artist_id)
        contributors.append({"role": role, "name": name, "id": artist_id})

    return contributors

# ===========================================================================
# 6. MAIN DATA STREAM  (release dump: release → media → tracks → recording)
# ===========================================================================
def fetch_raw_data_stream(
    xz_path: str,
    require_taxonomy: bool = True,
    min_tag_votes: int = 1,
    skip_releases: int = 0,
) -> Iterator[Dict[str, Any]]:
    counts = dict(releases=0, tracks=0, no_artist=0, no_tags=0, parse_err=0)

    if skip_releases:
        print(f"   ⏭  Skipping first {skip_releases:,} releases (previous volume boundary)...",
              flush=True)

    for raw_line in _iter_release_lines(xz_path):
        try:
            rel = json.loads(raw_line)
        except Exception:
            counts["parse_err"] += 1
            continue

        counts["releases"] += 1

        if counts["releases"] <= skip_releases:
            if counts["releases"] % 500_000 == 0:
                print(f"   ⏭  {counts['releases']:,} / {skip_releases:,} skipped...",
                      flush=True)
            continue

        if counts["releases"] % 500_000 == 0:
            print(
                f"\n   📊 {counts['releases']:,} releases | "
                f"{counts['tracks']:,} tracks seen | "
                f"no_artist={counts['no_artist']:,} no_tags={counts['no_tags']:,}",
                flush=True,
            )

        album_title = rel.get("title", "").strip()
        date_str    = rel.get("date", "") or ""
        year        = date_str[:4] if len(date_str) >= 4 else None

        # Tag fallback chain: release → release-group → artist-credit[0].artist
        release_tags = _collect_tags(rel, min_tag_votes)
        rg_tags      = _collect_tags(rel.get("release-group") or {}, min_tag_votes)
        ac           = rel.get("artist-credit") or []
        artist_tags  = _collect_tags((ac[0].get("artist") or {}) if ac and isinstance(ac[0], dict) else {}, min_tag_votes)
        album_tags   = release_tags or rg_tags or artist_tags

        for medium in (rel.get("media") or []):
            if not isinstance(medium, dict):
                continue
            for track in (medium.get("tracks") or []):
                if not isinstance(track, dict):
                    continue
                rec = track.get("recording")
                if not rec or not isinstance(rec, dict):
                    continue

                title = rec.get("title", "").strip()
                if not title:
                    continue

                counts["tracks"] += 1

                artist = _parse_artist_credit(
                    rec.get("artist-credit") or rel.get("artist-credit") or []
                )
                if not artist:
                    counts["no_artist"] += 1
                    continue

                # Recording tags first, fall back to release/album tags
                track_tags = _collect_tags(rec, min_tag_votes)
                genres = track_tags if track_tags else album_tags

                if require_taxonomy and not genres:
                    counts["no_tags"] += 1
                    continue

                isrcs = rec.get("isrcs") or []
                isrc  = isrcs[0] if isrcs else None
                length = rec.get("length")
                contributors = _collect_contributors(rec, rel)

                yield {
                    "title":        title,
                    "artist":       artist,
                    "isrc":         isrc,
                    "album":        album_title,
                    "year":         year,
                    "duration":     str(length) if length else None,
                    "genres":       genres[:5],
                    "contributors": contributors,
                    "_mbid":        rec.get("id"),
                    "_raw_index_checkpoint": counts["releases"],
                }

# ===========================================================================
# 7. STATE MANAGEMENT
# ===========================================================================
def load_pipeline_state(ledger_path: str) -> dict:
    if os.path.exists(ledger_path):
        try:
            with open(ledger_path) as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_processed_index": 0, "total_written_tracks": 0, "complete": False}


def save_pipeline_state(ledger_path: str, index: int, written: int, complete: bool = False):
    with open(ledger_path, "w") as f:
        json.dump({
            "last_processed_index": index,
            "total_written_tracks": written,
            "complete": complete,
        }, f)

# ===========================================================================
# 8. PIPELINE ENGINE
# ===========================================================================
def run_bounded_pipeline():
    args = parse_args()

    cache_dir     = os.path.join(args.output_dir, "cache")
    local_xz_path = os.path.join(cache_dir, "release.tar.xz")
    tracks_path   = os.path.join(args.output_dir, f"volume_{args.volume}_tracks.json")
    taxo_path     = os.path.join(args.output_dir, f"volume_{args.volume}_taxonomies.json")
    edges_path    = os.path.join(args.output_dir, f"volume_{args.volume}_edges.json")
    ledger_path   = os.path.join(args.output_dir, f"volume_{args.volume}_state.json")

    os.makedirs(args.output_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)

    cached_size = os.path.getsize(local_xz_path) if os.path.exists(local_xz_path) else 0
    if cached_size >= 1_000_000_000:
        print(f"✅ Using cached dump: {local_xz_path} ({cached_size/1e9:.2f} GB)")
    else:
        dump_url = args.dump_url or _latest_dump_url()
        download_with_resume(dump_url, local_xz_path, connections=args.connections)

    state = load_pipeline_state(ledger_path)
    if state["complete"]:
        print(f"🎉 Already complete ({state['total_written_tracks']:,} tracks).")
        return

    # Find where this volume should start in the dump.
    # Volume 1 always starts at 0; volume N starts where volume N-1 ended.
    start_release_index = 0
    if args.volume > 1:
        prev_ledger = os.path.join(args.output_dir, f"volume_{args.volume - 1}_state.json")
        prev_state  = load_pipeline_state(prev_ledger)
        if not prev_state.get("complete"):
            raise RuntimeError(
                f"Volume {args.volume - 1} is not marked complete. "
                f"Finish it before starting volume {args.volume}."
            )
        start_release_index = prev_state["last_processed_index"]
        print(f"📖 Volume {args.volume}: picking up from release {start_release_index:,} "
              f"(end of volume {args.volume - 1})")

    # Always start the output files fresh. Since we open in "w" mode we can't
    # reliably append to partial JSON arrays anyway, so resume = restart this volume
    # from its start_release_index (fast-forward through already-seen dump content).
    print("🧹 Clearing volume outputs for fresh write...")
    for p in (tracks_path, taxo_path, edges_path):
        if os.path.exists(p):
            os.remove(p)

    written_count = 0
    last_checkpoint = start_release_index
    seen_ids = set()

    f_tracks = open(tracks_path, "w", encoding="utf-8")
    f_taxo   = open(taxo_path,   "w", encoding="utf-8")
    f_edges  = open(edges_path,  "w", encoding="utf-8")
    for f in (f_tracks, f_taxo, f_edges):
        f.write("[\n")

    is_first = True

    print(f"\n🔥 Processing — target: {args.target_count:,} tracks")

    try:
        for item in fetch_raw_data_stream(
            local_xz_path,
            require_taxonomy=not args.no_require_taxonomy,
            min_tag_votes=args.min_tag_votes,
            skip_releases=start_release_index,
        ):
            tid = hashlib.sha256(
                f"{item['artist'].lower()}:{item['title'].lower()}".encode()
            ).hexdigest()[:24]

            if tid in seen_ids:
                continue
            seen_ids.add(tid)

            taxonomy_id = f"taxonomy:{tid}"

            duration_ms = None
            if item.get("duration"):
                try:
                    duration_ms = int(item["duration"])
                except (ValueError, TypeError):
                    pass

            track_node = {"name": tid, "fields": {
                "schemaVersion": 1,
                "trackId":       tid,
                "isrcCode":      item["isrc"],
                "title":         item["title"],
                "byArtist":      item["artist"],
                "albumName":     item["album"],
                "datePublished": item["year"],
                "durationMs":    duration_ms,
                "contributors":  item["contributors"],
            }}
            taxo_node = {"name": tid, "fields": {
                "schemaVersion": 1,
                "trackId": tid,
                "genres":  item["genres"],
                "moods":   [],
                "themes":  [],
                "contexts": [],
            }}
            edge = {"rel": "hasTaxonomy", "from": tid, "to": taxonomy_id}

            sep = "" if is_first else ",\n"
            f_tracks.write(f"{sep}  {json.dumps(track_node, ensure_ascii=False)}")
            f_taxo.write(f"{sep}  {json.dumps(taxo_node, ensure_ascii=False)}")
            f_edges.write(f"{sep}  {json.dumps(edge, ensure_ascii=False)}")
            if is_first:
                is_first = False

            written_count += 1
            print(
                f" 🚀 {written_count:>7,} | {item['artist'][:22]:<22} — "
                f"{item['title'][:22]:<22} | {item['genres']}",
                flush=True,
            )

            f_tracks.flush()
            f_taxo.flush()
            f_edges.flush()

            last_checkpoint = item["_raw_index_checkpoint"]
            if written_count % 500 == 0:
                save_pipeline_state(ledger_path, last_checkpoint, written_count)

            if written_count >= args.target_count:
                break

    except KeyboardInterrupt:
        print("\n🛑 Interrupted — progress saved.")
    finally:
        for f in (f_tracks, f_taxo, f_edges):
            f.write("\n]")
            f.close()
        save_pipeline_state(ledger_path, last_checkpoint, written_count, complete=(written_count >= args.target_count))
        print(f"\n📊 Done. Tracks written: {written_count:,}")


if __name__ == "__main__":
    run_bounded_pipeline()

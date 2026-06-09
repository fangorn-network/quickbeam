import os
import json
import hashlib
import re
import requests
import tarfile
import lzma
from typing import Iterator, Dict, Any

# ===========================================================================
# 1. CONFIGURATION
# ===========================================================================
REQUIRE_TAXONOMY   = True
MIN_TAG_VOTES      = 1
TARGET_TRACK_COUNT = 1000000
TARGET_VOLUME      = 1
OUTPUT_DIR         = "./stage_volumes"

NETWORK_CHUNK_SIZE = 2 * 1024 * 1024    # 2 MB download chunks
DOWNLOAD_TIMEOUT   = (15, 120)           # (connect, read) seconds

# ---------------------------------------------------------------------------
# Set this to the release.tar.xz URL from the dated directory listing.
# e.g. https://data.metabrainz.org/pub/musicbrainz/data/json-dumps/20260603-001002/release.tar.xz
# ---------------------------------------------------------------------------
DUMP_URL = "https://data.metabrainz.org/pub/musicbrainz/data/json-dumps/20260603-001002/release.tar.xz"

# ===========================================================================
# 2. PATHS
# ===========================================================================
CACHE_DIR         = os.path.join(OUTPUT_DIR, "cache")
LOCAL_XZ_PATH     = os.path.join(CACHE_DIR, "release.tar.xz")
CORE_FILE_PATH    = os.path.join(OUTPUT_DIR, f"volume_{TARGET_VOLUME}_core.json")
TAXO_FILE_PATH    = os.path.join(OUTPUT_DIR, f"volume_{TARGET_VOLUME}_taxonomy.json")
SOURCES_FILE_PATH = os.path.join(OUTPUT_DIR, f"volume_{TARGET_VOLUME}_sources.json")
LEDGER_PATH       = os.path.join(OUTPUT_DIR, f"volume_{TARGET_VOLUME}_state.json")

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(CACHE_DIR,  exist_ok=True)

# ===========================================================================
# 3. RESUMABLE DOWNLOADER
#    Downloads to disk first. Safe to Ctrl-C and restart — picks up from
#    the last byte using HTTP Range requests.
# ===========================================================================
def download_with_resume(url: str, dest: str) -> None:
    headers = {"User-Agent": "Fangorn-Pipeline/1.0"}
    existing = os.path.getsize(dest) if os.path.exists(dest) else 0

    head = requests.head(url, headers=headers, timeout=DOWNLOAD_TIMEOUT, allow_redirects=True)
    if head.status_code != 200:
        raise RuntimeError(f"HEAD {url} → HTTP {head.status_code}")

    total = int(head.headers.get("Content-Length", 0))
    accepts_ranges = head.headers.get("Accept-Ranges", "none").lower() == "bytes"

    if total and existing == total:
        print(f"✅ Already downloaded ({total/1e9:.2f} GB): {dest}")
        return

    if existing and total and existing > total:
        print(f"⚠️  Local file larger than remote — deleting and restarting")
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
            # Server ignored Range header
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
                        pct = written / total * 100
                        filled = int(pct / 2)
                        bar = "█" * filled + "░" * (50 - filled)
                        print(f"   [{bar}] {pct:5.1f}%  {written/1e9:.3f}/{total/1e9:.2f} GB",
                              end="\r", flush=True)
                    else:
                        print(f"   📡 {written/1e9:.3f} GB...", end="\r", flush=True)

    final = os.path.getsize(dest)
    if total and final != total:
        raise RuntimeError(
            f"Incomplete: {final:,} / {total:,} bytes. Re-run to resume."
        )
    print(f"\n✅ Download complete: {final/1e9:.2f} GB")

# ===========================================================================
# 4. BUFFERED STREAM WRAPPER
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
# 5. RELEASE LINE ITERATOR
# ===========================================================================
def _iter_release_lines(xz_path: str) -> Iterator[bytes]:
    file_size = os.path.getsize(xz_path)
    print(f"📂 Opening: {xz_path}  ({file_size/1e9:.2f} GB)")

    if file_size < 1_000_000_000:
        raise RuntimeError(
            f"Local file is only {file_size/1e6:.0f} MB — expected ~23 GB for release.tar.xz.\n"
            f"Check DUMP_URL and delete {xz_path} to re-download."
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
# 6. HELPERS
# ===========================================================================
def _extract_spotify_id(url_res: str) -> str:
    if not url_res:
        return None
    m = re.search(r'(?:spotify\.com/track/|spotify:track:)([a-zA-Z0-9]+)', url_res)
    return m.group(1) if m else None


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


def _collect_tags(obj: dict) -> list:
    seen = {}
    for field in ("genres", "tags"):
        for tag_obj in (obj.get(field) or []):
            if not isinstance(tag_obj, dict):
                continue
            name = tag_obj.get("name", "").strip()
            if not name:
                continue
            seen[name] = seen.get(name, 0) + tag_obj.get("count", 1)
    filtered = [(n, c) for n, c in seen.items() if c >= MIN_TAG_VOTES]
    filtered.sort(key=lambda x: -x[1])
    return [n.title() for n, _ in filtered]

# ===========================================================================
# 7. MAIN DATA STREAM  (release dump: release → media → tracks → recording)
# ===========================================================================
def fetch_raw_data_stream() -> Iterator[Dict[str, Any]]:
    counts = dict(releases=0, tracks=0, no_artist=0, no_tags=0, parse_err=0)

    for raw_line in _iter_release_lines(LOCAL_XZ_PATH):
        try:
            rel = json.loads(raw_line)
        except Exception:
            counts["parse_err"] += 1
            continue

        counts["releases"] += 1
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
        release_tags = _collect_tags(rel)
        rg_tags      = _collect_tags(rel.get("release-group") or {})
        ac           = rel.get("artist-credit") or []
        artist_tags  = _collect_tags((ac[0].get("artist") or {}) if ac and isinstance(ac[0], dict) else {})
        album_tags   = release_tags or rg_tags or artist_tags

        for medium in (rel.get("media") or []):  # KEY FIX: was "mediums", dump uses "media"
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
                track_tags = _collect_tags(rec)
                genres = track_tags if track_tags else album_tags

                if REQUIRE_TAXONOMY and not genres:
                    counts["no_tags"] += 1
                    continue

                isrcs = rec.get("isrcs") or []
                isrc  = isrcs[0] if isrcs else None

                spotify_id = None
                combined = (rel.get("relations") or []) + (rec.get("relations") or [])
                for relation in combined:
                    if isinstance(relation, dict):
                        sid = _extract_spotify_id(
                            (relation.get("url") or {}).get("resource", "")
                        )
                        if sid:
                            spotify_id = sid
                            break

                length = rec.get("length")

                yield {
                    "title":      title,
                    "artist":     artist,
                    "isrc":       isrc,
                    "album":      album_title,
                    "year":       year,
                    "duration":   str(length) if length else None,
                    "genres":     genres[:5],
                    "spotify_id": spotify_id,
                    "_mbid":      rec.get("id"),
                    "_raw_index_checkpoint": counts["releases"],
                }

# ===========================================================================
# 8. STATE MANAGEMENT
# ===========================================================================
def load_pipeline_state() -> dict:
    if os.path.exists(LEDGER_PATH):
        try:
            with open(LEDGER_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_processed_index": 0, "total_written_tracks": 0, "complete": False}


def save_pipeline_state(index: int, written: int, complete: bool = False):
    with open(LEDGER_PATH, "w") as f:
        json.dump({
            "last_processed_index": index,
            "total_written_tracks": written,
            "complete": complete,
        }, f)

# ===========================================================================
# 9. PIPELINE ENGINE
# ===========================================================================
def run_bounded_pipeline():
    download_with_resume(DUMP_URL, LOCAL_XZ_PATH)

    state = load_pipeline_state()
    if state["complete"]:
        print(f"🎉 Already complete ({state['total_written_tracks']:,} tracks).")
        return

    if os.path.exists(CORE_FILE_PATH) and state["total_written_tracks"] == 0:
        print("🧹 Clearing stale outputs...")
        for p in (CORE_FILE_PATH, TAXO_FILE_PATH, SOURCES_FILE_PATH, LEDGER_PATH):
            if os.path.exists(p):
                os.remove(p)

    written_count = state["total_written_tracks"]
    seen_ids = set()

    f_core = open(CORE_FILE_PATH, "w", encoding="utf-8")
    f_taxo = open(TAXO_FILE_PATH, "w", encoding="utf-8")
    f_src  = open(SOURCES_FILE_PATH, "w", encoding="utf-8")
    for f in (f_core, f_taxo, f_src):
        f.write("[\n")

    is_first_core = True
    is_first_src  = True

    print(f"\n🔥 Processing — target: {TARGET_TRACK_COUNT:,} tracks")

    try:
        for item in fetch_raw_data_stream():
            tid = hashlib.sha256(
                f"{item['artist'].lower()}:{item['title'].lower()}".encode()
            ).hexdigest()[:24]

            if tid in seen_ids:
                continue
            seen_ids.add(tid)

            core = {"name": tid, "fields": {
                "schemaVersion": 1,   "trackId": tid,
                "isrcCode":  item["isrc"],   "title":     item["title"],
                "byArtist":  item["artist"], "albumName": item["album"],
                "datePublished": item["year"],
                "durationMs": int(item["duration"]) if item.get("duration") and item["duration"].isdigit() else None,
                "contributors": [], "_mbid": item["_mbid"],
            }}
            taxo = {"name": tid, "fields": {
                "schemaVersion": 1, "trackId": tid,
                "genres": item["genres"], "moods": [], "themes": [], "contexts": [],
            }}

            sep = "" if is_first_core else ",\n"
            f_core.write(f"{sep}  {json.dumps(core, ensure_ascii=False)}")
            f_taxo.write(f"{sep}  {json.dumps(taxo, ensure_ascii=False)}")
            if is_first_core:
                is_first_core = False

            if item["spotify_id"]:
                src = {"trackId": tid, "platform": "spotify", "platformId": item["spotify_id"]}
                src_sep = "" if is_first_src else ",\n"
                f_src.write(f"{src_sep}  {json.dumps(src, ensure_ascii=False)}")
                if is_first_src:
                    is_first_src = False

            written_count += 1
            print(
                f" 🚀 {written_count:>7,} | {item['artist'][:22]:<22} — "
                f"{item['title'][:22]:<22} | {item['genres']}",
                flush=True,
            )

            f_core.flush()
            f_taxo.flush()
            f_src.flush()

            if written_count % 500 == 0:
                save_pipeline_state(item["_raw_index_checkpoint"], written_count)

            if written_count >= TARGET_TRACK_COUNT:
                break

    except KeyboardInterrupt:
        print("\n🛑 Interrupted — progress saved.")
    finally:
        for f in (f_core, f_taxo, f_src):
            f.write("\n]")
            f.close()
        save_pipeline_state(0, written_count, complete=(written_count >= TARGET_TRACK_COUNT))
        print(f"\n📊 Done. Tracks written: {written_count:,}")


if __name__ == "__main__":
    run_bounded_pipeline()
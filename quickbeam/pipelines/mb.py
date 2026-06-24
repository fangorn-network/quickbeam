import os
import re
import json
import math
import time
import argparse
import hashlib
import threading
import requests
import tarfile
import lzma
from typing import Iterator, Dict, Any, Optional

# ===========================================================================
# 1. CONFIGURATION
# ===========================================================================
NETWORK_CHUNK_SIZE = 2 * 1024 * 1024    # 2 MB download chunks
DOWNLOAD_TIMEOUT   = (15, 120)           # (connect, read) seconds

MB_DUMP_INDEX = "https://data.metabrainz.org/pub/musicbrainz/data/json-dumps/"

# ── ListenBrainz popularity enrichment ────────────────────────────────────
LB_POPULARITY_ARTIST = "https://api.listenbrainz.org/1/popularity/artist"
LB_BATCH             = 1000      # max artist_mbids per POST (MAX_ITEMS_PER_GET)
LB_TIMEOUT           = (10, 60)

# ── Quality scoring weights (tunable) ──────────────────────────────────────
# Artist listen count is the dominant "is this on the charts" signal.
W_ARTIST_POP = 3.0    # × log10(1 + total_listen_count)
W_TAG_VOTES  = 1.0    # × log10(1 + summed tag vote count)
W_CROSS_REL  = 1.2    # × log10(1 + cross-release frequency)  (needs --cross-release-freq)
W_ISRC       = 1.5    # commercial release marker
W_OFFICIAL   = 1.0    # Official status (vs bootleg/promo/pseudo)
W_TYPE       = 0.5    # Album/Single primary-type bonus

# Tiering thresholds (on total artist listen count, the chart proxy)
HIGH_ARTIST_LISTENS = 50_000     # marquee / charting artists → always kept first
# Release statuses & types we down-rank or treat as non-commercial noise.
GOOD_STATUSES = {"official"}
NOISE_GENRE_HINTS = ("audiobook", "spoken word", "field recording", "test tone")


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
    parser.add_argument("--force-download", action="store_true", default=False,
                        help="Discard any cached dump and download a fresh copy.")

    # ── Quality gate ──────────────────────────────────────────────────────
    qg = parser.add_argument_group("quality gate")
    qg.add_argument("--quality-gate", dest="quality_gate", action="store_true", default=True,
                    help="Rank by popularity & filter junk (default). Tracks the top of the "
                         "charts first, then fills to --target-count for breadth.")
    qg.add_argument("--no-quality-gate", dest="quality_gate", action="store_false",
                    help="Disable the gate — emit tracks in raw dump (≈random) order, as before.")
    qg.add_argument("--popularity", dest="popularity", action="store_true", default=True,
                    help="Enrich with ListenBrainz artist listen counts (default).")
    qg.add_argument("--no-popularity", dest="popularity", action="store_false",
                    help="Skip the ListenBrainz API; score from in-dump signals only.")
    qg.add_argument("--lb-token", default=os.environ.get("LISTENBRAINZ_TOKEN"),
                    help="ListenBrainz user token for higher API rate limits (or env LISTENBRAINZ_TOKEN).")
    qg.add_argument("--cross-release-freq", dest="cross_release_freq", action="store_true", default=False,
                    help="Also score by how many releases each recording appears on. Strong hit "
                         "signal but holds a per-recording counter in RAM (multi-GB on full dumps).")
    qg.add_argument("--per-artist-cap", type=int, default=400,
                    help="Max tracks kept per low-popularity artist (variety lever; popular "
                         "artists above the high-tier threshold are uncapped).")
    qg.add_argument("--min-tag-votes-floor", type=int, default=1,
                    help="A track with no ISRC and an unknown artist must have at least this "
                         "many summed tag votes to clear the eligibility floor.")
    return parser.parse_args()


# ===============================r============================================
# 2. RESUMABLE DOWNLOADER
#    Parallel-chunk mode (default, --connections N): pre-allocates the file
#    and downloads N byte-ranges simultaneously. A .parts sidecar tracks which
#    chunks finished so an interrupted run can skip them on restart.
#    Single-connection fallback (--connections 1): plain streaming with Range
#    resume, same as before.
#
#    Integrity: a `.incomplete` sentinel is created before the first byte is
#    written and removed only after the final size check passes. Combined with
#    the xz magic-byte check, this lets the caller reject a half-written /
#    zero-holed dump instead of feeding garbage into the decompressor.
# ===========================================================================
_XZ_MAGIC = b"\xfd7zXZ\x00"   # first 6 bytes of every .xz container


def _has_xz_magic(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(6) == _XZ_MAGIC
    except OSError:
        return False


def dump_cache_is_valid(path: str) -> bool:
    """A cached dump is trustworthy only if it's a sizeable, complete .xz file:
    no in-flight `.incomplete`/`.parts` sidecar and a valid xz magic header
    (catches the zero-hole-at-offset-0 left by an interrupted parallel download)."""
    if not os.path.exists(path) or os.path.getsize(path) < 1_000_000_000:
        return False
    if os.path.exists(path + ".incomplete") or os.path.exists(path + ".parts"):
        return False
    return _has_xz_magic(path)


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
    headers   = {"User-Agent": "Fangorn-Pipeline/1.0"}
    sentinel  = dest + ".incomplete"
    has_parts = os.path.exists(dest + ".parts")

    # A full-size file that's missing its xz header (or whose last run died
    # mid-flight without a resumable .parts sidecar) can only be garbage —
    # start over rather than try to "resume" holes we can't locate.
    if os.path.exists(dest) and not has_parts and not _has_xz_magic(dest):
        print("⚠️  Cached dump is corrupt (bad xz header) — restarting download from scratch")
        os.remove(dest)
        if os.path.exists(sentinel):
            os.remove(sentinel)

    existing = os.path.getsize(dest) if os.path.exists(dest) else 0

    head = requests.head(url, headers=headers, timeout=DOWNLOAD_TIMEOUT, allow_redirects=True)
    if head.status_code != 200:
        raise RuntimeError(f"HEAD {url} → HTTP {head.status_code}")

    total          = int(head.headers.get("Content-Length", 0))
    accepts_ranges = head.headers.get("Accept-Ranges", "none").lower() == "bytes"

    if total and existing == total and not os.path.exists(sentinel) and _has_xz_magic(dest):
        print(f"✅ Already downloaded ({total/1e9:.2f} GB): {dest}")
        return

    # Mark the download in-flight until the final size check below clears it.
    open(sentinel, "w").close()

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

        if not pending and _has_xz_magic(dest) and os.path.getsize(dest) == total:
            print(f"✅ Already downloaded ({total/1e9:.2f} GB): {dest}")
            if os.path.exists(sentinel):
                os.remove(sentinel)
            return
        if not pending:  # .parts claimed completion but the file is bad — redo
            done_set.clear()
            pending = [(i, s, e) for i, (s, e) in enumerate(bounds)]

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
    if not _has_xz_magic(dest):
        raise RuntimeError(
            f"Downloaded file at {dest} is not a valid .xz container (bad magic). "
            "Delete it and re-run."
        )
    if os.path.exists(sentinel):
        os.remove(sentinel)
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

    if not _has_xz_magic(xz_path):
        raise RuntimeError(
            f"{xz_path} is not a valid .xz container (likely a half-finished or zero-holed "
            f"download). Delete it (and any .parts/.incomplete sidecar) and re-run, or pass "
            f"--force-download."
        )

    try:
        with BufferedFileWrapper(xz_path) as raw:
            with lzma.open(raw, format=lzma.FORMAT_AUTO) as xz_stream:
                with tarfile.open(fileobj=xz_stream, mode="r|") as tar:
                    yield from _iter_tar_release(tar)
    except (tarfile.ReadError, lzma.LZMAError, EOFError) as exc:
        raise RuntimeError(
            f"Corrupt dump while reading {xz_path} ({type(exc).__name__}: {exc}). "
            f"The cached file has bad/missing bytes — re-run with --force-download."
        ) from exc


def _iter_tar_release(tar) -> Iterator[bytes]:
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


def _tag_vote_sum(*objs: dict) -> int:
    """Total community tag/genre vote count across the given objects — an engagement proxy."""
    total = 0
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        for field in ("genres", "tags"):
            for tag_obj in (obj.get(field) or []):
                if isinstance(tag_obj, dict):
                    total += tag_obj.get("count", 1)
    return total


def _artist_ids(*credit_lists: list) -> list:
    """Distinct artist MBIDs from one or more artist-credit lists (recording, then release)."""
    ids, seen = [], set()
    for credits in credit_lists:
        for seg in (credits or []):
            if not isinstance(seg, dict):
                continue
            artist = seg.get("artist")
            aid = artist.get("id") if isinstance(artist, dict) else None
            if aid and aid not in seen:
                seen.add(aid)
                ids.append(aid)
        if ids:  # prefer the recording-level credit; only fall back if it had none
            break
    return ids


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
        rel_status  = (rel.get("status") or "").strip().lower()
        rg          = rel.get("release-group") or {}
        primary_type = (rg.get("primary-type") or "").strip()

        # Tag fallback chain: release → release-group → artist-credit[0].artist
        release_tags = _collect_tags(rel, min_tag_votes)
        rg_tags      = _collect_tags(rg, min_tag_votes)
        ac           = rel.get("artist-credit") or []
        ac0_artist   = (ac[0].get("artist") or {}) if ac and isinstance(ac[0], dict) else {}
        artist_tags  = _collect_tags(ac0_artist, min_tag_votes)
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

                # Raw quality signals (consumed by the scorer; harmless otherwise).
                tag_votes = _tag_vote_sum(rec) or _tag_vote_sum(rel, rg, ac0_artist)
                artist_mbids = _artist_ids(rec.get("artist-credit"), rel.get("artist-credit"))

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
                    "_artist_mbids": artist_mbids,
                    "_tag_votes":   tag_votes,
                    "_status":      rel_status,
                    "_primary_type": primary_type,
                }

# ===========================================================================
# 6b. PASS 1 — INDEX BUILD
#     One cheap streaming pass to collect the distinct artist MBIDs (so we can
#     batch them through the ListenBrainz popularity API) and, optionally, a
#     per-recording cross-release frequency counter (how many releases each
#     recording shows up on — a strong "this is a hit" signal).
# ===========================================================================
def build_indices(xz_path: str, cross_release_freq: bool = False):
    artist_ids: set = set()
    rec_freq: Dict[str, int] = {} if cross_release_freq else None  # type: ignore[assignment]
    releases = 0

    print("\n🔎 Pass 1/2 — indexing artists"
          + (" + cross-release frequency" if cross_release_freq else "") + " ...")

    for raw_line in _iter_release_lines(xz_path):
        try:
            rel = json.loads(raw_line)
        except Exception:
            continue
        releases += 1
        if releases % 500_000 == 0:
            extra = f" | {len(rec_freq):,} recordings" if rec_freq is not None else ""
            print(f"   📊 {releases:,} releases | {len(artist_ids):,} artists{extra}", flush=True)

        for seg in (rel.get("artist-credit") or []):
            if isinstance(seg, dict):
                a = seg.get("artist")
                if isinstance(a, dict) and a.get("id"):
                    artist_ids.add(a["id"])

        for medium in (rel.get("media") or []):
            if not isinstance(medium, dict):
                continue
            for track in (medium.get("tracks") or []):
                if not isinstance(track, dict):
                    continue
                rec = track.get("recording")
                if not isinstance(rec, dict):
                    continue
                for seg in (rec.get("artist-credit") or []):
                    if isinstance(seg, dict):
                        a = seg.get("artist")
                        if isinstance(a, dict) and a.get("id"):
                            artist_ids.add(a["id"])
                if rec_freq is not None:
                    rid = rec.get("id")
                    if rid:
                        rec_freq[rid] = rec_freq.get(rid, 0) + 1

    print(f"   ✅ Indexed {len(artist_ids):,} distinct artists"
          + (f", {len(rec_freq):,} recordings" if rec_freq is not None else ""))
    return artist_ids, rec_freq

# ===========================================================================
# 6c. LISTENBRAINZ POPULARITY ENRICHMENT
#     Batch the distinct artist MBIDs through /1/popularity/artist (1000 per
#     POST). Results are cached to disk and the batch loop is resumable, so a
#     re-run costs nothing. The API is rate-limited via X-RateLimit-* headers;
#     we honour them by sleeping until the window resets.
# ===========================================================================
def _lb_post(mbids: list, token: Optional[str]) -> list:
    headers = {"Content-Type": "application/json", "User-Agent": "Fangorn-Pipeline/1.0"}
    if token:
        headers["Authorization"] = f"Token {token}"
    while True:
        resp = requests.post(LB_POPULARITY_ARTIST, headers=headers,
                             json={"artist_mbids": mbids}, timeout=LB_TIMEOUT)
        if resp.status_code == 429:
            wait = int(resp.headers.get("X-RateLimit-Reset-In", 5)) + 1
            print(f"   ⏳ rate-limited, sleeping {wait}s...", flush=True)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        # Proactively throttle when the window is nearly spent.
        if int(resp.headers.get("X-RateLimit-Remaining", 1)) <= 0:
            time.sleep(int(resp.headers.get("X-RateLimit-Reset-In", 5)) + 1)
        return resp.json()


def fetch_artist_popularity(artist_ids: set, cache_path: str,
                            token: Optional[str] = None) -> Dict[str, int]:
    pop: Dict[str, int] = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                pop = json.load(f)
            print(f"   ♻️  Loaded {len(pop):,} cached artist popularities")
        except Exception:
            pop = {}

    todo = [a for a in artist_ids if a not in pop]
    if not todo:
        return pop

    total_batches = (len(todo) + LB_BATCH - 1) // LB_BATCH
    print(f"🎧 Enriching {len(todo):,} artists via ListenBrainz "
          f"({total_batches:,} batches of {LB_BATCH})...")

    for bi in range(0, len(todo), LB_BATCH):
        batch = todo[bi:bi + LB_BATCH]
        try:
            rows = _lb_post(batch, token)
        except Exception as exc:
            print(f"   ⚠️  batch failed ({exc}); saving progress and stopping enrichment.")
            break
        for row in rows:
            aid = row.get("artist_mbid")
            if aid:
                pop[aid] = row.get("total_listen_count") or 0
        # Record misses as 0 so we don't re-query them on resume.
        for aid in batch:
            pop.setdefault(aid, 0)

        done = bi // LB_BATCH + 1
        if done % 25 == 0 or done == total_batches:
            with open(cache_path, "w") as f:
                json.dump(pop, f)
            print(f"   🎧 {done:,}/{total_batches:,} batches | {len(pop):,} artists cached",
                  flush=True)

    with open(cache_path, "w") as f:
        json.dump(pop, f)
    return pop

# ===========================================================================
# 6d. QUALITY SCORING
# ===========================================================================
def artist_popularity(item: dict, artist_pop: Dict[str, int]) -> int:
    """Best (max) ListenBrainz listen count across the track's credited artists."""
    if not artist_pop:
        return 0
    return max((artist_pop.get(a, 0) for a in item.get("_artist_mbids") or []), default=0)


def score_track(item: dict, artist_pop: Dict[str, int],
                rec_freq: Optional[Dict[str, int]]) -> float:
    score = 0.0
    pop = artist_popularity(item, artist_pop)
    score += W_ARTIST_POP * math.log10(1 + pop)
    score += W_TAG_VOTES  * math.log10(1 + max(0, item.get("_tag_votes") or 0))
    if item.get("isrc"):
        score += W_ISRC
    if item.get("_status") in GOOD_STATUSES:
        score += W_OFFICIAL
    if item.get("_primary_type") in ("Album", "Single", "EP"):
        score += W_TYPE
    if rec_freq is not None:
        freq = rec_freq.get(item.get("_mbid") or "", 1)
        score += W_CROSS_REL * math.log10(freq)
    # Down-rank obvious non-music / noise by genre hint.
    blob = " ".join(item.get("genres") or []).lower()
    if any(h in blob for h in NOISE_GENRE_HINTS):
        score -= 2.0
    return score


def classify_tier(item: dict, score: float, artist_pop: Dict[str, int],
                  min_tag_votes_floor: int) -> str:
    """Returns 'high' (charting), 'mid' (commercial corpus), 'low' (eligible filler),
    or 'reject' (below the junk floor)."""
    pop      = artist_popularity(item, artist_pop)
    has_isrc = bool(item.get("isrc"))
    official = item.get("_status") in GOOD_STATUSES
    votes    = item.get("_tag_votes") or 0

    if pop >= HIGH_ARTIST_LISTENS:
        return "high"
    if has_isrc or official:
        return "mid"
    # No commercial marker, low-profile artist: keep only with real engagement.
    if pop > 0 or votes >= min_tag_votes_floor:
        return "low"
    return "reject"

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
# 8. OUTPUT HELPERS  (shared by the legacy and quality-gated paths)
# ===========================================================================
def _track_id(artist: str, title: str) -> str:
    return hashlib.sha256(f"{artist.lower()}:{title.lower()}".encode()).hexdigest()[:24]


def _build_nodes(item: dict, tid: str):
    """Build the (track, taxonomy, edge) JSON nodes for one track item."""
    duration_ms = None
    if item.get("duration"):
        try:
            duration_ms = int(item["duration"])
        except (ValueError, TypeError):
            pass
    track_node = {"name": tid, "fields": {
        "schemaVersion": 1,
        "trackId":       tid,
        "isrcCode":      item.get("isrc"),
        "title":         item["title"],
        "byArtist":      item["artist"],
        "albumName":     item.get("album"),
        "datePublished": item.get("year"),
        "durationMs":    duration_ms,
        "contributors":  item.get("contributors") or [],
    }}
    taxo_node = {"name": tid, "fields": {
        "schemaVersion": 1,
        "trackId": tid,
        "genres":  item.get("genres") or [],
        "moods":   [],
        "themes":  [],
        "contexts": [],
    }}
    edge = {"rel": "hasTaxonomy", "from": tid, "to": f"taxonomy:{tid}"}
    return track_node, taxo_node, edge


class ArrayWriter:
    """Streams objects into three parallel JSON-array files (tracks/taxo/edges)."""
    def __init__(self, tracks_path, taxo_path, edges_path):
        self.f_tracks = open(tracks_path, "w", encoding="utf-8")
        self.f_taxo   = open(taxo_path,   "w", encoding="utf-8")
        self.f_edges  = open(edges_path,  "w", encoding="utf-8")
        for f in self._files:
            f.write("[\n")
        self._first = True

    @property
    def _files(self):
        return (self.f_tracks, self.f_taxo, self.f_edges)

    def write(self, item: dict, tid: str):
        track_node, taxo_node, edge = _build_nodes(item, tid)
        sep = "" if self._first else ",\n"
        self.f_tracks.write(f"{sep}  {json.dumps(track_node, ensure_ascii=False)}")
        self.f_taxo.write(f"{sep}  {json.dumps(taxo_node, ensure_ascii=False)}")
        self.f_edges.write(f"{sep}  {json.dumps(edge, ensure_ascii=False)}")
        self._first = False

    def close(self):
        for f in self._files:
            f.write("\n]")
            f.close()


# ===========================================================================
# 9. QUALITY-GATED PIPELINE
#     Phase 1  index   → distinct artist MBIDs (+ optional cross-release freq)
#     Phase 2  enrich  → ListenBrainz artist listen counts
#     Phase 3  shard   → score every track, write to high/mid/low/reject shards
#     Phase 4  assemble→ high first, then mid, then low; per-artist cap + dedup;
#                        top up from reject only if short of --target-count.
# ===========================================================================
def _shard_path(cache_dir: str, volume: int, tier: str) -> str:
    return os.path.join(cache_dir, f"volume_{volume}_shard_{tier}.jsonl")


def run_quality_gated(args, cache_dir, local_xz_path, tracks_path, taxo_path, edges_path):
    pop_cache = os.path.join(cache_dir, "artist_popularity.json")
    tiers = ("high", "mid", "low", "reject")
    shard_paths = {t: _shard_path(cache_dir, args.volume, t) for t in tiers}

    # ── Phase 1: index ────────────────────────────────────────────────────
    artist_ids, rec_freq = build_indices(local_xz_path, args.cross_release_freq)

    # ── Phase 2: enrich ─────────────────────────────────────────────────────
    artist_pop: Dict[str, int] = {}
    if args.popularity:
        artist_pop = fetch_artist_popularity(artist_ids, pop_cache, args.lb_token)
    else:
        print("⏭  Skipping ListenBrainz enrichment (--no-popularity).")
    del artist_ids  # free memory before pass 2

    # ── Phase 3: score & shard ──────────────────────────────────────────────
    print("\n🏷️  Pass 2/2 — scoring & sharding tracks by quality tier...")
    shard_files = {t: open(p, "w", encoding="utf-8") for t, p in shard_paths.items()}
    stats = {t: 0 for t in tiers}
    # Cap the reject shard: we only ever need it to top up toward --target-count.
    reject_cap = args.target_count * 2
    seen = 0
    try:
        for item in fetch_raw_data_stream(
            local_xz_path, require_taxonomy=False, min_tag_votes=args.min_tag_votes,
        ):
            seen += 1
            score = score_track(item, artist_pop, rec_freq)
            tier = classify_tier(item, score, artist_pop, args.min_tag_votes_floor)
            stats[tier] += 1
            if tier == "reject" and stats["reject"] > reject_cap:
                continue
            # Stash the bits assembly needs (score + popularity for the cap rule).
            item["_score"] = round(score, 4)
            item["_pop"] = artist_popularity(item, artist_pop)
            shard_files[tier].write(json.dumps(item, ensure_ascii=False) + "\n")
            if seen % 1_000_000 == 0:
                print(f"   📊 {seen:,} tracks | high={stats['high']:,} mid={stats['mid']:,} "
                      f"low={stats['low']:,} reject={stats['reject']:,}", flush=True)
    finally:
        for f in shard_files.values():
            f.close()
    print(f"   ✅ Sharded: high={stats['high']:,} mid={stats['mid']:,} "
          f"low={stats['low']:,} reject={stats['reject']:,} (of {seen:,} tracks)")

    # ── Phase 4: assemble ───────────────────────────────────────────────────
    target = args.target_count
    skip   = (args.volume - 1) * target   # rank offset so volumes are disjoint
    print(f"\n📦 Assembling volume {args.volume}: target {target:,} tracks "
          f"(rank offset {skip:,})...")

    writer = ArrayWriter(tracks_path, taxo_path, edges_path)
    seen_tids: set = set()
    artist_counts: Dict[str, int] = {}
    rank = 0          # eligible tracks passed (across all volumes) — drives the offset
    written = 0

    def consume(tier: str) -> bool:
        """Emit from one shard; returns True once the volume target is filled."""
        nonlocal rank, written
        path = shard_paths[tier]
        if not os.path.exists(path):
            return False
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                tid = _track_id(item["artist"], item["title"])
                if tid in seen_tids:
                    continue

                # Per-artist cap for variety; marquee artists are left uncapped
                # so their full catalogue can come through.
                mbids = item.get("_artist_mbids") or []
                key = mbids[0] if mbids else item["artist"].lower()
                if item.get("_pop", 0) < HIGH_ARTIST_LISTENS and \
                   artist_counts.get(key, 0) >= args.per_artist_cap:
                    continue

                seen_tids.add(tid)
                artist_counts[key] = artist_counts.get(key, 0) + 1
                rank += 1
                if rank <= skip:
                    continue

                writer.write(item, tid)
                written += 1
                if written % 50_000 == 0:
                    print(f"   🚀 {written:,}/{target:,} "
                          f"(tier={tier}, last={item['artist'][:30]} — {item['title'][:30]})",
                          flush=True)
                if written >= target:
                    return True
        return False

    try:
        for tier in ("high", "mid", "low"):
            if consume(tier):
                break
        else:
            if written < target:
                print(f"   ⚠️  Only {written:,} tracks cleared the quality floor; "
                      f"topping up from rejects to reach {target:,}...")
                consume("reject")
    except KeyboardInterrupt:
        print("\n🛑 Interrupted during assembly.")
    finally:
        writer.close()

    if written < target:
        print(f"   ⚠️  Dump only yielded {written:,} distinct tracks (< target {target:,}).")
    print(f"\n📊 Done. Tracks written: {written:,}")


# ===========================================================================
# 10. PIPELINE ENGINE  (entry point: dispatches legacy vs quality-gated)
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

    if args.force_download and os.path.exists(local_xz_path):
        print("🗑️  --force-download: discarding cached dump")
        for suffix in ("", ".incomplete", ".parts"):
            if os.path.exists(local_xz_path + suffix):
                os.remove(local_xz_path + suffix)

    if dump_cache_is_valid(local_xz_path):
        print(f"✅ Using cached dump: {local_xz_path} "
              f"({os.path.getsize(local_xz_path)/1e9:.2f} GB)")
    else:
        if os.path.exists(local_xz_path):
            print("⚠️  Cached dump is incomplete or corrupt — re-downloading.")
        dump_url = args.dump_url or _latest_dump_url()
        download_with_resume(dump_url, local_xz_path, connections=args.connections)

    print("🧹 Clearing volume outputs for fresh write...")
    for p in (tracks_path, taxo_path, edges_path):
        if os.path.exists(p):
            os.remove(p)

    if args.quality_gate:
        run_quality_gated(args, cache_dir, local_xz_path,
                          tracks_path, taxo_path, edges_path)
        return

    # ── Legacy path: sequential scan, ≈random order, multi-volume by dump offset ──
    state = load_pipeline_state(ledger_path)
    if state["complete"]:
        print(f"🎉 Already complete ({state['total_written_tracks']:,} tracks).")
        return

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

    written_count = 0
    last_checkpoint = start_release_index
    seen_ids = set()
    writer = ArrayWriter(tracks_path, taxo_path, edges_path)

    print(f"\n🔥 Processing — target: {args.target_count:,} tracks")

    try:
        for item in fetch_raw_data_stream(
            local_xz_path,
            require_taxonomy=not args.no_require_taxonomy,
            min_tag_votes=args.min_tag_votes,
            skip_releases=start_release_index,
        ):
            tid = _track_id(item["artist"], item["title"])
            if tid in seen_ids:
                continue
            seen_ids.add(tid)

            writer.write(item, tid)
            written_count += 1
            print(
                f" 🚀 {written_count:>7,} | {item['artist'][:22]:<22} — "
                f"{item['title'][:22]:<22} | {item['genres']}",
                flush=True,
            )

            last_checkpoint = item["_raw_index_checkpoint"]
            if written_count % 500 == 0:
                save_pipeline_state(ledger_path, last_checkpoint, written_count)

            if written_count >= args.target_count:
                break
    except KeyboardInterrupt:
        print("\n🛑 Interrupted — progress saved.")
    finally:
        writer.close()
        save_pipeline_state(ledger_path, last_checkpoint, written_count,
                            complete=(written_count >= args.target_count))
        print(f"\n📊 Done. Tracks written: {written_count:,}")


if __name__ == "__main__":
    run_bounded_pipeline()

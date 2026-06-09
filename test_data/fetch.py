import argparse
import hashlib
import json
import os
import time
import requests
import musicbrainzngs

# ---------------------------------------------------------------------------
# CONFIG & INITIALIZATION
# ---------------------------------------------------------------------------

USER_AGENT = "MusicIngestor/3.0"
LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
SCHEMA_VERSION = 1

musicbrainzngs.set_useragent("MusicIngestor", "3.0", "https://example.com")

DEFAULT_SEED_ARTISTS = [
    # Classic Rock & Metal
    "The Beatles", "Led Zeppelin", "Pink Floyd", "Queen", "David Bowie", 
    "Fleetwood Mac", "Metallica", "Black Sabbath", "Iron Maiden", "Jimi Hendrix",
    "AC/DC", "The Rolling Stones", "The Who", "Deep Purple", "Guns N' Roses", 
    "System of a Down", "Slipknot", "Between the Buried and Me", "Animals as Leaders",
    "Meshuggah", "Lorna Shore", "Periphery",
    # Alternative, Grunge & Indie
    "Nirvana", "Radiohead", "The Cure", "Depeche Mode", "The Smiths", 
    "Pixies", "Pearl Jam", "Soundgarden", "Linkin Park", "Gorillaz",
    # Hip-Hop & R&B
    "Kendrick Lamar", "Kanye West", "Outkast", "The Notorious B.I.G.", "Tupac Shakur", 
    "Eminem", "Jay-Z", "Nas", "A Tribe Called Quest", "Wu-Tang Clan", "Dr. Dre",
    "Michael Jackson", "Prince", "Stevie Wonder", "Marvin Gaye", "Lauryn Hill",
    # Electronic & Dance
    "Daft Punk", "Kraftwerk", "The Prodigy", "Chemical Brothers", "Aphex Twin",
    "Massive Attack", "Portishead", "LCD Soundsystem", "Justice", "New Order",
    # Jazz, Blues & Soul
    "Miles Davis", "John Coltrane", "Bill Evans", "Charles Mingus", "Nina Simone",
    "B.B. King", "Muddy Waters", "Ray Charles", "Aretha Franklin", "Amy Winehouse"
]

TAG_CLASSIFIER = {
    "genres": [
        "rock", "pop", "hip-hop", "rap", "jazz", "electronic", "metal", "blues", 
        "folk", "punk", "soul", "r&b", "progressive rock", "classic rock", "synthpop",
        "grunge", "hard rock", "alternative rock", "funk", "ambient", "indie",
        "trip-hop", "idm", "techno", "house", "shoegaze", "lo-fi",
        "deathcore", "djent", "progressive metal", "math metal", "death metal", "symphonic metal"
    ],
    "moods": [
        "melancholy", "sad", "happy", "chill", "energetic", "dark", "uplifting", 
        "angry", "romantic", "peaceful", "aggressive", "somber", "intense", "gloomy",
        "mellow", "calm", "beautiful", "hypnotic", "hyper", "nostalgic", "ethereal",
        "trippy", "haunting", "brooding", "brutal", "chaotic", "heavy"
    ],
    "themes": [
        "love", "heartbreak", "nostalgia", "rebellion", "futuristic", "psychedelic", 
        "night", "summer", "space", "existential", "political", "death", "drugs", "spiritual",
        "sci-fi", "mythology", "apocalypse"
    ],
    "contexts": [
        "driving", "workout", "study", "relaxing", "party", "late night", "sleep", 
        "background", "focus", "headphone listening", "live", "club", "car", "chillout",
        "moshpit", "headbanging"
    ]
}

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def track_id(artist: str, title: str) -> str:
    return hashlib.sha256(f"{artist}:{title}".encode()).hexdigest()[:24]

def get_combined_file_size(*filepaths: str) -> int:
    total_bytes = 0
    for path in filepaths:
        if os.path.exists(path):
            total_bytes += os.path.getsize(path)
    return total_bytes

def extract_taxonomy_entity(tid: str, raw_tags: list[str]) -> dict:
    entity = {
        "name": tid,
        "fields": {
            "schemaVersion": SCHEMA_VERSION,
            "trackId": tid,
            "genres": [],
            "moods": [],
            "themes": [],
            "contexts": []
        }
    }
    for tag in raw_tags:
        for category, keywords in TAG_CLASSIFIER.items():
            if tag in keywords and tag not in entity["fields"][category]:
                entity["fields"][category].append(tag)
    return entity

# ---------------------------------------------------------------------------
# LAST.FM ENGINE
# ---------------------------------------------------------------------------

class LastFM:
    def __init__(self, key: str):
        self.key = key
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def call(self, params: dict) -> dict:
        params["api_key"] = self.key
        params["format"] = "json"
        try:
            res = self.session.get(LASTFM_BASE, params=params, timeout=20)
            return res.json() if res.status_code == 200 else {}
        except Exception:
            return {}

    def get_top_albums(self, artist: str, limit: int = 5) -> list[str]:
        data = self.call({"method": "artist.getTopAlbums", "artist": artist, "limit": limit})
        albums = data.get("topalbums", {}).get("album", [])
        if isinstance(albums, dict):
            albums = [albums]
        return [a["name"] for a in albums if a.get("name") and a["name"] != "(null)"]

    def get_album_payload(self, artist: str, album: str) -> dict:
        data = self.call({"method": "album.getInfo", "artist": artist, "album": album})
        return data.get("album", {})

    def track_info_fallback(self, artist: str, track: str) -> dict:
        data = self.call({"method": "track.getInfo", "artist": artist, "track": track})
        return data.get("track", {})

    def get_track_tags(self, artist: str, track: str) -> list[str]:
        data = self.call({"method": "track.getTopTags", "artist": artist, "track": track})
        tags_list = data.get("toptags", {}).get("tag", [])
        if isinstance(tags_list, dict):
            tags_list = [tags_list]
        return [t["name"].lower() for t in tags_list if t.get("name")]

# ---------------------------------------------------------------------------
# MUSICBRAINZ
# ---------------------------------------------------------------------------

def mb_enrich(artist: str, title: str) -> dict:
    result = {"isrcCode": None, "datePublished": None, "contributors": None}
    try:
        search = musicbrainzngs.search_recordings(recording=title, artist=artist, limit=1)
        recordings = search.get("recording-list", [])
        if not recordings:
            return result

        rec = recordings[0]
        full = musicbrainzngs.get_recording_by_id(
            rec["id"], includes=["isrcs", "artist-credits", "releases"]
        ).get("recording", {})

        isrcs = full.get("isrc-list", [])
        result["isrcCode"] = isrcs[0] if isrcs else None

        releases = full.get("release-list", [])
        dates = [r.get("date") for r in releases if r.get("date")]
        result["datePublished"] = min(dates) if dates else None

        credits = full.get("artist-credit", [])
        contributors = []
        for credit in credits:
            if isinstance(credit, dict) and "artist" in credit:
                contributors.append({
                    "role": "artist",
                    "name": credit["artist"].get("name"),
                    "id": credit["artist"].get("id")
                })
        result["contributors"] = contributors or None
    except Exception as e:
        print(f"    [MB] failed for {artist} - {title}: {e}")
    return result

# ---------------------------------------------------------------------------
# MAIN VOLUMETRIC EXECUTION
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--albums-per-artist", type=int, default=10, help="Deep search count")
    parser.add_argument("--no-mb", action="store_true")
    parser.add_argument("--artists-file", default=None)
    parser.add_argument("--volume", type=int, default=1, help="Current output split block number for Pinata isolation")
    parser.add_argument("--max-gb", type=float, default=9.5, help="Safety limit per volume to comply with Pinata 10GB tiers")
    args = parser.parse_args()

    api_key = os.getenv("LASTFM_API_KEY")
    if not api_key:
        print("Error: LASTFM_API_KEY environment variable not set.")
        return

    max_bytes = int(args.max_gb * 1024 * 1024 * 1024)

    # Establish Volumetric File Isolation Boundaries
    out_core = f"volume_{args.volume}_core.jsonl"
    out_taxo = f"volume_{args.volume}_taxonomy.jsonl"

    if args.artists_file and os.path.exists(args.artists_file):
        with open(args.artists_file, "r", encoding="utf-8") as f:
            artists = [line.strip() for line in f if line.strip()]
    else:
        artists = DEFAULT_SEED_ARTISTS

    lfm = LastFM(api_key)
    seen_tracks = set()
    total_scraped = 0
    taxonomy_saved = 0

    # DYNAMIC RESUME LOGIC: Scan the active volume space to reconstruct state map
    if os.path.exists(out_core):
        print(f"🔄 Existing file found for Volume {args.volume}. Scanning cache to resume seamlessly...")
        with open(out_core, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                    if "name" in obj:
                        seen_tracks.add(obj["name"])
                except Exception:
                    continue
        print(f"Loaded {len(seen_tracks)} tracks into active processing memory.")

    print(f"\n📊 TARGET DESTINATION: Pinata Deployment Cluster [Volume {args.volume}]")
    print(f"👉 Target Structural: {out_core}")
    print(f"👉 Target Taxonomy: {out_taxo}")
    print(f"📏 Current Combined Volume Size: {get_combined_file_size(out_core, out_taxo) / (1024**3):.4f} / {args.max_gb} GB\n" + "-"*60)

    volume_ceiling_tripped = False

    for artist in artists:
        if volume_ceiling_tripped:
            break
            
        print(f"\n🎸 Discography: {artist.upper()}")
        albums = lfm.get_top_albums(artist, limit=args.albums_per_artist)
        
        for album in albums:
            if volume_ceiling_tripped:
                break
                
            print(f"  💿 Album: {album}")
            album_payload = lfm.get_album_payload(artist, album)
            
            tracks = album_payload.get("tracks", {}).get("track", [])
            if isinstance(tracks, dict):
                tracks = [tracks]
                
            if not tracks:
                continue

            for t in tracks:
                # Volumetric Isolation Boundary Check
                current_size = get_combined_file_size(out_core, out_taxo)
                if current_size >= max_bytes:
                    print(f"\n⚠️ VOLUME CEILING HIT: Volume {args.volume} has hit {args.max_gb} GB.")
                    print(f"🛑 Execution paused. Upload these files to Pinata Account {args.volume}, then resume with flag: --volume {args.volume + 1}")
                    volume_ceiling_tripped = True
                    break

                title = t.get("name")
                if not title:
                    continue
                    
                tid = track_id(artist, title)
                if tid in seen_tracks:
                    continue
                seen_tracks.add(tid)

                duration_ms = None
                if t.get("duration"):
                    try:
                        duration_ms = int(t.get("duration")) * 1000 or None
                    except ValueError:
                        pass
                
                if not duration_ms:
                    info = lfm.track_info_fallback(artist, title)
                    duration_ms = int(info.get("duration") or 0) or None

                # 1. CORE STRUCTURAL LOG
                core_entry = {
                    "name": tid,
                    "fields": {
                        "schemaVersion": SCHEMA_VERSION,
                        "trackId": tid,
                        "isrcCode": None,
                        "title": title,
                        "byArtist": artist,
                        "albumName": album,
                        "datePublished": None,
                        "durationMs": duration_ms,
                        "contributors": None,
                    },
                }

                if not args.no_mb:
                    enriched = mb_enrich(artist, title)
                    core_entry["fields"].update(enriched)
                    time.sleep(0.4)

                # 2. TAXONOMY CLASSIFICATION
                raw_tags = lfm.get_track_tags(artist, title)
                taxo_entry = extract_taxonomy_entity(tid, raw_tags)

                fields = taxo_entry["fields"]
                has_taxonomy = any([fields["genres"], fields["moods"], fields["themes"], fields["contexts"]])

                # 3. ATOMIC DISK WRITE
                with open(out_core, "a", encoding="utf-8") as f_core:
                    f_core.write(json.dumps(core_entry, ensure_ascii=False) + "\n")

                if has_taxonomy:
                    with open(out_taxo, "a", encoding="utf-8") as f_taxo:
                        f_taxo.write(json.dumps(taxo_entry, ensure_ascii=False) + "\n")
                    taxonomy_saved += 1
                    print(f"    ✔ Processed: {title} (Core + Taxonomy)")
                else:
                    print(f"    ✔ Processed: {title} (Core only)")

                total_scraped += 1
                time.sleep(0.05)

    final_size_gb = get_combined_file_size(out_core, out_taxo) / (1024**3)
    print(f"\n🏁 Run complete for Volume {args.volume} (Size: {final_size_gb:.4f} GB)")
    print(f"   Processed this session: {total_scraped} tracks.")

if __name__ == "__main__":
    main()
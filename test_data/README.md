Markdown

# Ingestor: Volumetric Music Catalog Harvester 🎸🔊

This is a production-grade metadata collection pipeline designed to harvest deep music discographies, enrich them with semantic data structure variants, and segment the outputs into isolated storage volumes. 

The pipeline is explicitly optimized for horizontal data scaling across decentralized storage providers, such as **Pinata IPFS clusters**, by keeping files strictly bounded beneath strict tier thresholds (e.g., 10GB).

---

## 🏗️ Architecture & Data Layout

The ingestor isolates data mutations into two discrete, atomic, line-delimited JSON (`.jsonl`) files per volume block. This pattern prevents structural entity inflation and ensures clean node parsing for relational or document databases.

📁 Output Directory
├── 📝 volume_1_core.jsonl      <-- Structural metadata source of truth (Always written)
├── 📝 volume_1_taxonomy.jsonl  <-- Semantic classifications / Tag mappings (Skipped if empty)
├── 📝 volume_2_core.jsonl      <-- Automatically instantiated when volume rotates
└── 📝 volume_2_taxonomy.jsonl


### 1. The Core Schema (`volume_{X}_core.jsonl`)
Contains the immutable structural invariants of a track. Populated via Last.fm and enriched over upstream MusicBrainz lookups.
```json
{
  "name": "20243bbf517243affc575384",
  "fields": {
    "schemaVersion": 1,
    "trackId": "20243bbf517243affc575384",
    "isrcCode": "USSM11104847",
    "title": "Bleed",
    "byArtist": "Meshuggah",
    "albumName": "obZen",
    "datePublished": "2008-03-07",
    "durationMs": 442000,
    "contributors": [{"role": "artist", "name": "Meshuggah", "id": "9b1c7ec2-3cda-48fb-a3c3-d922ec313e9d"}]
  }
}
```

### 2. The Taxonomy Schema (volume_{X}_taxonomy.jsonl)

Contains dynamic, crowdsourced contextual metadata categorized by an optimized matching array. If a track carries no taxonomy correlations, no entry is written, removing dead disk bloat.
``` JSON
{
  "name": "20243bbf517243affc575384",
  "fields": {
    "schemaVersion": 1,
    "trackId": "20243bbf517243affc575384",
    "genres": ["metal", "djent", "progressive metal"],
    "moods": ["intense", "brutal", "chaotic"],
    "themes": ["existential", "sci-fi"],
    "contexts": ["headbanging", "workout"]
  }
}
```

- Key Features
  - Volumetric Circuit Breaker: Monitors real-time file footprints on disk. Automatically halts execution right before hitting storage constraints (e.g., hitting 9.5 GB to preserve a safety buffer under a 10 GB account limit)
  - Stateful Pause & Resume: Scans current volume fragments on startup. If execution is interrupted via network hiccups or explicit Ctrl+C commands, it builds an in-memory hash index of collected tracks and resumes extraction work smoothly without duplicates.
  - Heavy Music Subgenre Integration: Built-in semantic classification arrays mapped specifically to target contemporary extreme structures (Deathcore, Djent, Symphonic Metal, Mathcore).
  - API Bottleneck Optimization: Extracts tracking durations natively from contextual album tree payloads, reducing out-of-band standard REST round-trips by up to 80%.

## Setup & Execution
1. Prerequisites & Dependencies

Ensure your environment satisfies the network dependencies:
``` Bash
pip install requests musicbrainzngs
```

2. Set Environment Credentials

The script reads your access keys directly out of your local shell environment:

```Bash
export LASTFM_API_KEY="your_secret_lastfm_api_key_here"
```

3. Execution Commands
Standard Collection (Volume 1)

To begin harvesting the default master catalog into your first Pinata target file chunk:
``` Bash
python fetch.py --volume 1 --max-gb 9.5
```

### Interrupting and Resuming

If you close the terminal or lose connection, run the exact same command again. The engine detects volume_1_core.jsonl, repopulates its state tracking array, and pushes forward.
Rotating to a New Cluster (Volume 2)

When the console outputs ⚠️ VOLUME CEILING HIT, your local files have safely hit their target size limit.

- Upload volume_1_core.jsonl and volume_1_taxonomy.jsonl directly to your first Pinata IPFS gateway account.
- Initialize the pipeline's next secondary storage volume block by incrementing the runtime target flag:

``` Bash
python fetch.py --volume 2 --max-gb 9.5
```

## 🛠️ Advanced Customization

Utilizing Custom Seeding Documents

Instead of relying strictly on the hardcoded foundational artist seed array, you can pass an explicit line-separated text document (artists.txt) into the runtime sequence:

``` Bash
python fetch.py --volume 1 --artists-file artists.txt
``` 

### Disabling Upstream Metadata Enrichment

If you need to maximize pipeline speed and skip the rate-limited MusicBrainz verification lookups, pass the alternative command flag:

``` Bash
python fetch.py --volume 1 --no-mb
```
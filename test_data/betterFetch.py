import os
import json
import hashlib
import sys
import time
import queue
import threading
import multiprocessing
from typing import Iterator, Dict, Any

# ===========================================================================
# 1. YOUR DATA SOURCE HOOK (DROP YOUR SOURCE CODE HERE)
# ===========================================================================
def fetch_raw_data_stream() -> Iterator[Dict[str, Any]]:
    """
    This is the fuel line for the engine. Replace the placeholder loop below 
    with your actual data source (e.g., reading a CSV, querying a DB, etc.).
    
    CRITICAL: Use 'yield' instead of returning a massive list to keep 
    your local memory usage flat under 40MB.
    """
    # --- PLACEHOLDER SIMULATION (Replace this block with your actual data loop) ---
    print("🎸 Simulating raw music data stream...")
    for i in range(1000000):
        yield {
            "title": f"Song Title Tracks {i}",
            "artist": f"Artist Name {i}",
            "isrc": "USRC12345678",
            "album": "The Infinite Catalog Volume 1",
            "year": "2026",
            "duration": "180000",
            "genres": ["Electronic", "Ambient"],
            "moods": ["Cinematic"],
            "contexts": ["Studying"],
            "themes": ["Futuristic"],
            "contributors": [{"role": "Producer", "name": "AI Engine", "id": "0x99"}]
        }
    # ------------------------------------------------------------------------------

# ===========================================================================
# 2. PIPELINE CONFIGURATION & STORAGE LIMITS
# ===========================================================================
OUTPUT_DIR = "./stage_volumes"
MAX_FILE_SIZE_GB = 1  # Hard disk cap: Script pauses immediately when hit
TARGET_VOLUME = 3       # Current volume naming suffix

CORE_FILE_PATH = os.path.join(OUTPUT_DIR, f"volume_{TARGET_VOLUME}_core.json")
TAXO_FILE_PATH = os.path.join(OUTPUT_DIR, f"volume_{TARGET_VOLUME}_taxonomy.json")
LEDGER_PATH = os.path.join(OUTPUT_DIR, f"volume_{TARGET_VOLUME}_state.json")

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ===========================================================================
# 3. STATE LEDGER MANAGEMENT
# ===========================================================================
def load_pipeline_state() -> dict:
    if os.path.exists(LEDGER_PATH):
        try:
            with open(LEDGER_PATH, "r") as f:
                return json.load(f)
        except:
            pass
    return {"last_processed_index": 0, "total_written_tracks": 0, "complete": False}

def save_pipeline_state(index: int, written: int, complete: bool = False):
    with open(LEDGER_PATH, "w") as f:
        json.dump({
            "last_processed_index": index, 
            "total_written_tracks": written, 
            "complete": complete,
            "last_updated": time.time()
        }, f, indent=2)

# ===========================================================================
# 4. MULTI-CORE PROCESSING WORKER
# ===========================================================================
def processing_worker(chunk_data: list) -> list:
    """
    Runs on parallel CPU cores. Transforms a batch of raw records 
    into pre-formatted JSON blocks wrapped with 'name' and 'fields' keys.
    """
    processed_batch = []
    for item in chunk_data:
        try:
            if not isinstance(item, dict):
                continue
            artist = str(item.get("artist", "")).strip()
            title = str(item.get("title", "")).strip()
            if not artist or not title:
                continue

            # Compute collision-resistant 24-character track ID
            cleaned = f"{artist.lower()}:{title.lower()}"
            tid = hashlib.sha256(cleaned.encode()).hexdigest()[:24]
            
            # Map directly to wrapped Core Track Schema
            core_record = {
                "name": tid,
                "fields": {
                    "schemaVersion": 1,  # Strict Integer
                    "trackId": tid,
                    "isrcCode": item.get("isrc") if item.get("isrc") else None,
                    "title": title,
                    "byArtist": artist,
                    "albumName": item.get("album") if item.get("album") else None,
                    "datePublished": item.get("year") if item.get("year") else None,
                    "durationMs": int(item["duration"]) if str(item.get("duration", "")).isdigit() else None,
                    "contributors": item.get("contributors") if isinstance(item.get("contributors"), list) else []
                }
            }
            
            # Map directly to wrapped Taxonomy Schema
            taxo_record = {
                "name": tid,
                "fields": {
                    "schemaVersion": 1,  # Strict Integer
                    "trackId": tid,
                    "genres": [str(g) for g in item.get("genres", [])] if isinstance(item.get("genres"), list) else [],
                    "moods": [str(m) for m in item.get("moods", [])] if isinstance(item.get("moods"), list) else [],
                    "themes": [str(t) for t in item.get("themes", [])] if isinstance(item.get("themes"), list) else [],
                    "contexts": [str(c) for c in item.get("contexts", [])] if isinstance(item.get("contexts"), list) else []
                }
            }
            
            processed_batch.append((
                json.dumps(core_record, ensure_ascii=False),
                json.dumps(taxo_record, ensure_ascii=False)
            ))
        except:
            continue
    return processed_batch

# ===========================================================================
# 5. HIGH-SPEED DISK WRITER CONSUMER
# ===========================================================================
def disk_writer_consumer(write_queue: queue.Queue, stop_event: threading.Event, start_from_scratch: bool):
    max_bytes = MAX_FILE_SIZE_GB * 1024 * 1024 * 1024
    
    if start_from_scratch:
        f_core = open(CORE_FILE_PATH, "w", encoding="utf-8")
        f_taxo = open(TAXO_FILE_PATH, "w", encoding="utf-8")
        f_core.write("[\n")
        f_taxo.write("[\n")
        is_first = True
    else:
        # Resume mode: roll back trailing characters to keep array notation valid
        for path in (CORE_FILE_PATH, TAXO_FILE_PATH):
            if os.path.exists(path):
                with open(path, "r+", encoding="utf-8") as f:
                    f.seek(0, os.SEEK_END)
                    pos = f.tell() - 1
                    while pos > 0:
                        f.seek(pos)
                        if f.read(1) in ("]", "\n", " ", ","):
                            pos -= 1
                        else:
                            f.seek(pos + 1)
                            f.truncate()
                            break
        f_core = open(CORE_FILE_PATH, "a", encoding="utf-8")
        f_taxo = open(TAXO_FILE_PATH, "a", encoding="utf-8")
        is_first = False

    try:
        while True:
            try:
                batch = write_queue.get(timeout=0.5)
            except queue.Empty:
                if stop_event.is_set():
                    break
                continue

            if batch is None:
                break
                
            for core_str, taxo_str in batch:
                if f_core.tell() >= max_bytes or f_taxo.tell() >= max_bytes:
                    print(f"\n⚠️ Size Boundary Reached ({MAX_FILE_SIZE_GB} GB). Pausing pipeline gracefully...")
                    stop_event.set()
                    break
                    
                if not is_first:
                    f_core.write(",\n")
                    f_taxo.write(",\n")
                else:
                    is_first = False
                    
                f_core.write("  " + core_str)
                f_taxo.write("  " + taxo_str)
                
            write_queue.task_done()
            if stop_event.is_set():
                break
    finally:
        f_core.write("\n]")
        f_taxo.write("\n]")
        f_core.close()
        f_taxo.close()

# ===========================================================================
# 6. MAIN COORDINATION RUNNER
# ===========================================================================
def run_bounded_pipeline():
    state = load_pipeline_state()
    if state["complete"]:
        print(f"🎉 Volume {TARGET_VOLUME} is already complete! Update TARGET_VOLUME to continue.")
        return

    start_index = state["last_processed_index"]
    written_count = state["total_written_tracks"]
    start_fresh = (start_index == 0)

    print(f"🔥 Resuming Pipeline. Offset: {start_index} processed. Total written: {written_count}")

    write_queue = queue.Queue(maxsize=64)
    stop_event = threading.Event()
    
    writer_thread = threading.Thread(
        target=disk_writer_consumer, 
        args=(write_queue, stop_event, start_fresh)
    )
    writer_thread.start()

    num_cores = multiprocessing.cpu_count()
    pool = multiprocessing.Pool(processes=num_cores)
    
    CHUNK_SIZE = 2500
    current_chunk = []
    global_index = 0
    async_results = []

    try:
        # Connect directly to your data hook defined at the top of this file
        raw_data_generator = fetch_raw_data_stream()

        for item in raw_data_generator:
            if global_index < start_index:
                global_index += 1
                continue
                
            if stop_event.is_set():
                break

            current_chunk.append(item)
            global_index += 1

            if len(current_chunk) == CHUNK_SIZE:
                res = pool.apply_async(processing_worker, args=(current_chunk,))
                async_results.append((global_index, res))
                current_chunk = []

                while len(async_results) > num_cores * 2:
                    idx_anchor, r = async_results[0]
                    if r.ready() or stop_event.is_set():
                        if not stop_event.is_set():
                            data = r.get()
                            write_queue.put(data)
                            written_count += len(data)
                            save_pipeline_state(idx_anchor, written_count)
                        async_results.pop(0)
                    else:
                        time.sleep(0.01)

        if current_chunk and not stop_event.is_set():
            res = pool.apply_async(processing_worker, args=(current_chunk,))
            async_results.append((global_index, res))

        for idx_anchor, r in async_results:
            if not stop_event.is_set():
                data = r.get()
                write_queue.put(data)
                written_count += len(data)
                save_pipeline_state(idx_anchor, written_count)

    except KeyboardInterrupt:
        print("\n🛑 Manual pause received. Flushing queue and locking state...")
        stop_event.set()

    finally:
        pool.close()
        pool.join()
        
        write_queue.put(None)
        writer_thread.join()
        
        is_done = not stop_event.is_set() and (global_index > start_index)
        save_pipeline_state(global_index if is_done else load_pipeline_state()["last_processed_index"], written_count, complete=is_done)
        
        print("\n" + "="*60)
        print("📊 STATE LOCK REPORT")
        print(f"  Core File Size: {os.path.getsize(CORE_FILE_PATH) / (1024**2):.2f} MB")
        print(f"  Taxo File Size: {os.path.getsize(TAXO_FILE_PATH) / (1024**2):.2f} MB")
        print(f"  Next Resume Index: {load_pipeline_state()['last_processed_index']}")
        print(f"  Status: {'FINISHED VOLUME' if is_done else 'PAUSED - SIZE BOUNDARY HIT / INTERRUPTED'}")
        print("="*60)

if __name__ == "__main__":
    multiprocessing.freeze_support()
    run_bounded_pipeline()
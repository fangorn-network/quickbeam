"""The /events SSE feed: the wire format javascript-example.js parses, end to end.

Run against a real server on a real directory rather than by calling the generator, so
this also covers the parts that only exist over HTTP: the media type, the unbuffered
chunking, and the fact that the response never completes. The client in
javascript-example.js is the spec — event names and payload keys here must match what
its `follow()` switch reads, or the stream is silently useless to it.
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.request

import uvicorn

from quickbeam.cdn import build_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_manifest(cdn_dir: str, domain: str, shard_files: list) -> None:
    """Write a domain manifest naming `shard_files`, the way append_domain does."""
    d = os.path.join(cdn_dir, domain)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump({"domain": domain, "count": len(shard_files), "dim": 256,
                   "shards": [{"file": n} for n in shard_files]}, f)


def _events(body, timeout=20.0):
    """Yield {event, data} from an open SSE response, mirroring the JS client's parser."""
    buf = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        chunk = body.read1(4096)
        if not chunk:
            break
        buf += chunk.decode()
        while "\n\n" in buf:
            block, buf = buf.split("\n\n", 1)
            event, data = "message", ""
            for line in block.split("\n"):
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data += line[5:].strip()
            if data:  # `: ping` comments carry none — dropped, exactly as the client does
                yield {"event": event, "data": json.loads(data)}


def test_events_stream(tmp_dir):
    cdn_dir = tmp_dir
    # One domain already baked before the client connects, one not yet present.
    _write_manifest(cdn_dir, "app8-owner8-media", ["shard-aaa.ndjson.gz"])

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(build_app(cdn_dir), host="127.0.0.1",
                                           port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1).read()
            break
        except Exception:
            time.sleep(0.1)

    url = (f"http://127.0.0.1:{port}/events"
           f"?domain=app8-owner8-media&domain=app8-owner8-docs")
    resp = urllib.request.urlopen(url, timeout=20)
    assert resp.headers["content-type"].startswith("text/event-stream"), \
        "wrong media type — EventSource and the example client both reject it"

    try:
        stream = _events(resp)

        # 1. snapshot: fires per already-baked domain the instant you connect, so a
        #    client needs no separate "what is there?" request. The unbaked domain
        #    must NOT produce one.
        first = next(stream)
        assert first == {"event": "snapshot",
                         "data": {"domain": "app8-owner8-media"}}, first

        # 2. change: a commit appends a shard. The event names the new file, which is
        #    the whole point — the client fetches it without re-reading the manifest.
        time.sleep(0.3)
        _write_manifest(cdn_dir, "app8-owner8-media",
                        ["shard-aaa.ndjson.gz", "shard-bbb.ndjson.gz"])
        change = next(stream)
        assert change["event"] == "change", change
        assert change["data"] == {"domain": "app8-owner8-media",
                                  "added": ["shard-bbb.ndjson.gz"]}, change["data"]

        # 3. added: a domain baked for the first time while we were connected. The
        #    client treats this like a snapshot and pulls the whole manifest.
        _write_manifest(cdn_dir, "app8-owner8-docs", ["shard-ccc.ndjson.gz"])
        added = next(stream)
        assert added == {"event": "added",
                         "data": {"domain": "app8-owner8-docs"}}, added
    finally:
        resp.close()
        server.should_exit = True


def test_traversal_is_rejected(tmp_dir):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(build_app(tmp_dir), host="127.0.0.1",
                                           port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1).read()
            break
        except Exception:
            time.sleep(0.1)
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/events?domain=../../etc",
                               timeout=5)
        raise AssertionError("traversal in ?domain= was accepted")
    except urllib.error.HTTPError as e:
        assert e.code == 400, e.code
    finally:
        server.should_exit = True


if __name__ == "__main__":
    import tempfile
    for fn in (test_events_stream, test_traversal_is_rejected):
        with tempfile.TemporaryDirectory() as d:
            fn(d)
        print("ok", fn.__name__)
    sys.exit(0)

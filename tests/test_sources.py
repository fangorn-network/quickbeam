"""Wildcard (`*`) source parsing → app-level `fangorn subscribe` argv."""
from quickbeam.ingest.sources.fangorn import parse_sources, subscribe_cmd

OWNER = "0x7a7849231cF7Ab1EA003BcF0063CB89704D7Cce9"


def test_parse_sources_wildcards():
    assert parse_sources([f"{OWNER}:ns"]) == [(OWNER, "ns")]
    assert parse_sources([f"{OWNER}:*"]) == [(OWNER, None)]
    assert parse_sources(["*:ns"]) == [(None, "ns")]
    assert parse_sources(["*:*"]) == [(None, None)]


def test_subscribe_cmd_app_mode():
    # Pinned: the tightest topic filter, no --app.
    assert subscribe_cmd("fangorn", OWNER, "ns") == \
        ["fangorn", "subscribe", "ns", "--owner", OWNER]
    # Any wildcard side switches to the app-level filter, keeping the set side.
    assert subscribe_cmd("fangorn", OWNER, None) == \
        ["fangorn", "subscribe", "--all", "--owner", OWNER]
    assert subscribe_cmd("fangorn", None, "ns") == \
        ["fangorn", "subscribe", "--all", "ns"]
    assert subscribe_cmd("fangorn", None, None) == ["fangorn", "subscribe", "--all"]
    # --app is a GLOBAL fangorn option (it picks the app), so it precedes the subcommand.
    assert subscribe_cmd("fangorn", None, None, app="sond3r.test.1") == \
        ["fangorn", "--app", "sond3r.test.1", "subscribe", "--all"]
    # Replay: an explicit block wins over the genesis form.
    assert subscribe_cmd("fangorn", None, None, from_start=True) == \
        ["fangorn", "subscribe", "--all", "--from-start"]
    assert subscribe_cmd("fangorn", None, None, from_start=True, from_block=42) == \
        ["fangorn", "subscribe", "--all", "--from-block", "42"]
    # --fangorn-bin may be a full command, shell-split.
    assert subscribe_cmd("node cli.js", None, None)[:2] == ["node", "cli.js"]

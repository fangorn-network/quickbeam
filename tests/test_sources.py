"""Wildcard (`*`) source parsing → app-level `fangorn subscribe` argv."""
from quickbeam.ingest.sources.fangorn import app_env, parse_sources, subscribe_cmd

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
    # Replay: an explicit block wins over the genesis form.
    assert subscribe_cmd("fangorn", None, None, from_start=True) == \
        ["fangorn", "subscribe", "--all", "--from-start"]
    assert subscribe_cmd("fangorn", None, None, from_start=True, from_block=42) == \
        ["fangorn", "subscribe", "--all", "--from-block", "42"]
    # --fangorn-bin may be a full command, shell-split.
    assert subscribe_cmd("node cli.js", None, None)[:2] == ["node", "cli.js"]


def test_app_travels_in_the_env_not_argv():
    """No fangorn command defines a `--app` flag — putting the app in argv made every
    seed read die with "unknown option '--app'". The CLI reads FANGORN_APP_ID instead,
    and that outranks ~/.fangorn/config.json, so the daemon can watch another app
    without editing the local client's config."""
    import os

    env = app_env("sond3r.test.1")
    assert env["FANGORN_APP_ID"] == "sond3r.test.1"
    assert env["PATH"] == os.environ["PATH"], "must inherit the rest of the environment"
    # No app → inherit ours untouched, so `set-app` still decides.
    assert app_env(None) is None
    # And the app must never leak back into argv.
    assert "--app" not in subscribe_cmd("fangorn", None, None)


def test_watch_parses_both_source_branches():
    """`--sources-url`/`--sources-refresh` are read in four places (the watch-list poll
    loop) but defined only in parse_args. Dropping the add_argument calls while keeping
    the readers makes EVERY `quickbeam watch` die on `Namespace object has no attribute
    sources_url` — including the container's, which passes the flag. Guard both branches."""
    import sys
    from unittest.mock import patch
    from quickbeam.watcher import parse_args

    with patch.object(sys, "argv", ["watch", "--source", f"{OWNER}:media"]):
        args = parse_args()
    assert args.sources_url is None and args.sources_refresh == 60

    with patch.object(sys, "argv", ["watch", "--sources-url", "https://reg.test/watchlist",
                                    "--sources-refresh", "30"]):
        args = parse_args()
    assert args.sources_url == "https://reg.test/watchlist" and args.sources_refresh == 30

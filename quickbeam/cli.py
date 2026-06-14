import sys
import typer

app = typer.Typer(
    name="quickbeam",
    help="build and serve a vector search index from on-chain manifests.",
    no_args_is_help=True,
)
data_app = typer.Typer(
    help="Generate seed / test data from public data sources.",
    no_args_is_help=True,
)
app.add_typer(data_app, name="data", help="Generate seed / test data from public data sources.")

# All commands with existing argparse parsers use passthrough so their own --help
# and argument validation are preserved verbatim.
_PASSTHROUGH = dict(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)


def _fwd(name: str, extra: list[str]) -> None:
    sys.argv = [name] + extra


@app.command(**_PASSTHROUGH)
def build(ctx: typer.Context):
    """Build embeddings from subgraph / IPFS data into Qdrant."""
    import asyncio
    _fwd("quickbeam build", ctx.args)
    from quickbeam.embeddings import main
    asyncio.run(main())


@app.command(**_PASSTHROUGH)
def serve(ctx: typer.Context):
    """Start the Fangorn search API server.

    Pass `--watch` to also run the live embedding daemon alongside the server.
    Everything BEFORE `--watch` configures the server; everything AFTER it is
    forwarded to `quickbeam watch`. Example:

      quickbeam serve --x402-pay-to 0xRECV --watch --bundle fangorn=0xID --poll-interval 120
    """
    import sys, subprocess, atexit, signal

    args = list(ctx.args)
    watch_proc = None
    if "--watch" in args:
        idx        = args.index("--watch")
        serve_args = args[:idx]
        watch_args = args[idx + 1:]

        print(f"[serve] launching watcher: quickbeam watch {' '.join(watch_args)}")
        watch_proc = subprocess.Popen([sys.executable, "-m", "quickbeam.cli", "watch", *watch_args])

        def _stop_watcher():
            if watch_proc.poll() is None:
                print("[serve] stopping watcher…")
                watch_proc.terminate()
                try:
                    watch_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    watch_proc.kill()
        atexit.register(_stop_watcher)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: (_stop_watcher(), sys.exit(0)))
    else:
        serve_args = args

    _fwd("quickbeam serve", serve_args)
    from quickbeam.server import main
    main()


@app.command(**_PASSTHROUGH)
def mcp(ctx: typer.Context):
    """Run the MCP server exposing quickbeam search as agent tools (x402-aware)."""
    _fwd("quickbeam mcp", ctx.args)
    from quickbeam.mcp_server import main
    main()


@app.command(**_PASSTHROUGH)
def watch(ctx: typer.Context):
    """Live daemon: poll subgraph for new events and embed them automatically."""
    import asyncio
    _fwd("quickbeam watch", ctx.args)
    from quickbeam.watcher import main
    asyncio.run(main())


@app.command(**_PASSTHROUGH)
def export(ctx: typer.Context):
    """Export the Qdrant collection as an NDJSON bundle."""
    _fwd("quickbeam export", ctx.args)
    from quickbeam.export_bundle import main
    main()


@app.command()
def migrate():
    """Migrate a local Qdrant collection to Qdrant Cloud."""
    from quickbeam.migrate import run
    run()


@data_app.command(**_PASSTHROUGH)
def fetch(ctx: typer.Context):
    """Harvest music metadata via the Last.fm + MusicBrainz APIs."""
    _fwd("quickbeam data fetch", ctx.args)
    from quickbeam.pipelines.lastfm import main
    main()


@data_app.command(**_PASSTHROUGH)
def mb(ctx: typer.Context):
    """Process a MusicBrainz JSON dump into Fangorn-compatible JSONL."""
    _fwd("quickbeam data mb", ctx.args)
    from quickbeam.pipelines.mb import run_bounded_pipeline
    run_bounded_pipeline()


@data_app.command(**_PASSTHROUGH)
def osm(ctx: typer.Context):
    """Fetch recent OSM changesets for a bounding box."""
    _fwd("quickbeam data osm", ctx.args)
    from quickbeam.pipelines.osm import main
    main()


if __name__ == "__main__":
    app()

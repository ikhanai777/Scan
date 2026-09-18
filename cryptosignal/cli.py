"""Command line entry points.

    cryptosignal scan --once        one cycle, then exit
    cryptosignal scan               the loop, on the configured cadence
    cryptosignal serve              dashboard + API
    cryptosignal serve --scan       dashboard + API + the scan loop in one process
    cryptosignal screen             what the screen sees right now, fires nothing
    cryptosignal stats              the published track record
    cryptosignal config             the resolved tuning
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .alerts import build_notifiers
from .config import settings
from .exchange import CCXTFeed
from .scanner import Scanner
from .screen import setup_score, shortlist, stage1_universe
from .store import Store


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)-24s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("ccxt").setLevel(logging.WARNING)


def _build_scanner() -> tuple[Scanner, Store]:
    store = Store(settings.database_path)
    feed = CCXTFeed(settings)
    notifiers = build_notifiers(settings)
    if notifiers.names:
        logging.getLogger(__name__).info("alert channels: %s", ", ".join(notifiers.names))
    return Scanner(settings, feed, store, notifiers), store


def cmd_scan(args: argparse.Namespace) -> int:
    scanner, store = _build_scanner()
    try:
        if args.once:
            report = scanner.run_cycle()
            print(json.dumps(report.to_dict(), indent=2))
        else:
            print(f"scanning {settings.exchange_id} every {settings.scan_interval_seconds:.0f}s "
                  f"({settings.timeframe} candles, top {settings.universe_size}). ctrl-c to stop.")
            scanner.run_forever(max_cycles=args.cycles)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        store.close()
    return 0


def cmd_screen(args: argparse.Namespace) -> int:
    """Run the funnel and print the shortlist. Analyses nothing, fires nothing."""
    feed = CCXTFeed(settings)
    markets = feed.snapshots()
    universe = stage1_universe(markets, settings)
    print(f"{len(markets)} markets -> {len(universe)} through stage 1\n")

    from .features import compute_features

    candidates = []
    for market in universe:
        candles = feed.candles(market.symbol)
        if candles is None:
            continue
        features = compute_features(candles)
        if features is not None:
            candidates.append(setup_score(market, features, settings))

    print(f"{'symbol':<14}{'setup':>7}  {'vol.exp':>8}{'vol.anom':>9}{'action':>8}   why")
    print("-" * 78)
    for candidate in shortlist(candidates, settings):
        c = candidate.components
        print(f"{candidate.symbol:<14}{candidate.setup_score:>7.1f}  "
              f"{c.get('volatility', float('nan')):>8.0f}{c.get('volume', float('nan')):>9.0f}"
              f"{c.get('price_action', float('nan')):>8.0f}   {'; '.join(candidate.reasons)}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api.app import create_app, run_scanner_in_background

    store = Store(settings.database_path)
    scanner = None
    if args.scan:
        feed = CCXTFeed(settings)
        scanner = Scanner(settings, feed, store, build_notifiers(settings))
        run_scanner_in_background(scanner)

    app = create_app(settings, store, scanner)
    host = args.host or settings.api_host
    port = args.port or settings.api_port
    print(f"dashboard on http://{host}:{port}  (scanner {'attached' if scanner else 'not attached'})")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    store = Store(settings.database_path)
    performance = store.performance()
    print(json.dumps(performance, indent=2))
    if performance["closed"]:
        print(f"\n{settings.DISCLAIMER}")
    store.close()
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    print(json.dumps(settings.as_dict(), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cryptosignal", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="run the scan loop")
    scan.add_argument("--once", action="store_true", help="a single cycle, then exit")
    scan.add_argument("--cycles", type=int, default=None, help="stop after N cycles")
    scan.set_defaults(func=cmd_scan)

    screen = sub.add_parser("screen", help="show the current shortlist without analysing or firing")
    screen.set_defaults(func=cmd_screen)

    serve = sub.add_parser("serve", help="run the dashboard API")
    serve.add_argument("--scan", action="store_true", help="also run the scan loop in this process")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.set_defaults(func=cmd_serve)

    stats = sub.add_parser("stats", help="print the track record")
    stats.set_defaults(func=cmd_stats)

    config = sub.add_parser("config", help="print the resolved configuration")
    config.set_defaults(func=cmd_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

"""Command line entry points.

    cryptosignal doctor             prove every live source end to end, then exit
    cryptosignal scan --once        one cycle, then exit
    cryptosignal scan               the loop, on the configured cadence
    cryptosignal serve              dashboard + API
    cryptosignal serve --scan       dashboard + API + the scan loop in one process
    cryptosignal screen             what the screen sees right now, fires nothing
    cryptosignal stats              the published track record
    cryptosignal config             the resolved tuning
    cryptosignal backtest           replay the engine over real history
    cryptosignal sweep              tune parameters against real history
    cryptosignal record             capture real market data as test fixtures
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .alerts import build_notifiers
from .config import settings
from .context import MarketContext
from .exchange import CCXTFeed
from .execution import build_engine
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
    log = logging.getLogger(__name__)
    store = Store(settings.database_path)
    feed = CCXTFeed(settings)
    notifiers = build_notifiers(settings)
    if notifiers.names:
        log.info("alert channels: %s", ", ".join(notifiers.names))

    context = MarketContext(settings, exchange_client=feed._client)
    # build_engine raises on a half-configured live setup rather than starting
    # in a state the operator did not intend.
    execution = build_engine(settings, exchange_client=feed._client)
    if execution is not None:
        log.warning("execution is ON in %s mode", execution.risk.mode)

    return Scanner(settings, feed, store, notifiers, context=context, execution=execution), store


def _resolve_symbols(feed: CCXTFeed, raw: str | None, limit: int) -> list[str]:
    """An explicit list, or the top of the live universe by liquidity."""
    if raw:
        return [s.strip().upper() for s in raw.split(",") if s.strip()]
    universe = stage1_universe(feed.snapshots(), settings)
    return [m.symbol for m in universe[:limit]]


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


def cmd_backtest(args: argparse.Namespace) -> int:
    """Replay the engine over real exchange history."""
    from .backtest import backtest_many

    feed = CCXTFeed(settings)
    symbols = _resolve_symbols(feed, args.symbols, args.top)
    print(f"\n  pulling {args.bars} bars of {settings.timeframe} history for "
          f"{len(symbols)} symbol(s) from {settings.exchange_id}...\n")

    histories = []
    for symbol in symbols:
        candles = feed.history(symbol, args.bars)
        if candles is None or len(candles) < 100:
            print(f"  [skip] {symbol}: {0 if candles is None else len(candles)} bars returned")
            continue
        print(f"  [ok]   {symbol}: {len(candles)} real bars")
        histories.append(candles)

    if not histories:
        print("\n  No history came back. Run `cryptosignal doctor` first.\n")
        return 1

    summary = backtest_many(histories, settings).to_dict()
    print()
    print(json.dumps(summary, indent=2))
    if summary["trades"] == 0:
        print("\n  No signal fired over this window. That is a result, not a failure:\n"
              "  widen the window, lower the threshold band, or add symbols.\n")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    """Tune parameters against real history."""
    from .backtest import sweep

    grid = {
        "long_threshold": [55.0, 60.0, 65.0, 70.0],
        "short_threshold": [-55.0, -60.0, -65.0, -70.0],
        "stop_atr_multiple": [1.25, 1.5, 2.0],
    }
    if args.grid:
        try:
            grid = json.loads(args.grid)
        except ValueError as exc:
            print(f"--grid must be JSON: {exc}")
            return 1

    feed = CCXTFeed(settings)
    symbols = _resolve_symbols(feed, args.symbols, args.top)
    histories = [c for c in (feed.history(s, args.bars) for s in symbols) if c is not None]
    if not histories:
        print("No history came back. Run `cryptosignal doctor` first.")
        return 1

    print(f"\n  sweeping {len(grid)} parameter(s) over {len(histories)} symbol(s)...\n")
    rows = sweep(histories, settings, grid, min_trades=args.min_trades)

    print(f"  {'expectancy':>11}{'trades':>8}{'hit rate':>10}{'max DD':>9}   parameters")
    print("  " + "-" * 82)
    for row in rows[: args.show]:
        summary = row.summary
        flag = " " if row.trades >= args.min_trades else "*"
        print(f"{flag} {row.expectancy:>10.3f}R{summary['trades']:>8}"
              f"{(summary['hit_rate'] or 0):>9.0f}%{summary['max_drawdown_r']:>8.1f}R   "
              f"{row.overrides}")
    print(f"\n  * fewer than {args.min_trades} trades -- not enough to tune on.")
    print("  These results fit this window. A parameter set that only wins here\n"
          "  is fitted noise; re-run over a different period before trusting it.\n")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    """Capture real market data as test fixtures."""
    from .fixtures import FIXTURE_DIR, record

    feed = CCXTFeed(settings)
    symbols = _resolve_symbols(feed, args.symbols, args.top)
    print(f"\n  recording {args.bars} bars for {len(symbols)} symbol(s)...\n")

    written = record(feed, symbols, args.bars, settings.exchange_id)
    for path in written:
        print(f"  [ok] {path}")
    if not written:
        print("  Nothing recorded. Run `cryptosignal doctor` first.")
        return 1
    print(f"\n  {len(written)} fixture(s) in {FIXTURE_DIR}.\n"
          f"  `pytest` now runs the engine over this real data too.\n")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Every line is a live measurement. Exit 1 if the pipeline cannot run."""
    from .diagnostics import diagnose

    print(f"\n  cryptosignal doctor -- live check against {settings.exchange_id}\n")
    report = diagnose(settings)
    print(report.render())
    if report.ok:
        print("\n  Live data confirmed. `cryptosignal scan --once` runs a real cycle.\n")
        return 0
    print("\n  Not ready. Fix the failure above, then run doctor again.\n")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cryptosignal", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="prove the live feed end to end and exit")
    doctor.set_defaults(func=cmd_doctor)

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

    def add_history_args(sub_parser, default_bars: int):
        sub_parser.add_argument("--symbols", default=None,
                                help="comma-separated, e.g. BTC/USDT,ETH/USDT (default: the live universe)")
        sub_parser.add_argument("--top", type=int, default=5, help="how many of the universe to use")
        sub_parser.add_argument("--bars", type=int, default=default_bars, help="bars of real history")

    backtest = sub.add_parser("backtest", help="replay the engine over real exchange history")
    add_history_args(backtest, 1500)
    backtest.set_defaults(func=cmd_backtest)

    sweep_parser = sub.add_parser("sweep", help="tune parameters against real history")
    add_history_args(sweep_parser, 1500)
    sweep_parser.add_argument("--grid", default=None, help='JSON, e.g. {"long_threshold":[55,65]}')
    sweep_parser.add_argument("--min-trades", type=int, default=20,
                              help="below this a result is not evidence")
    sweep_parser.add_argument("--show", type=int, default=15, help="rows to print")
    sweep_parser.set_defaults(func=cmd_sweep)

    record_parser = sub.add_parser("record", help="capture real market data as test fixtures")
    add_history_args(record_parser, 600)
    record_parser.set_defaults(func=cmd_record)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

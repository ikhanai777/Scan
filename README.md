# cryptosignal

A market scanner that screens the crypto universe on a fixed cadence, ranks
live setups, and issues a long or short call with an entry zone, a stop, two
targets and a holding window.

```
$ cryptosignal scan --once

  15:02:41  INFO  cryptosignal.scanner  cycle: 412 markets -> 20 universe -> 10 shortlist -> 2 signals in 6.4s
  15:02:41  INFO  cryptosignal.scanner  FIRED LONG SOL/USDT @ 71% (composite +79.4)
  15:02:41  INFO  cryptosignal.scanner  FIRED SHORT AVAX/USDT @ 64% (composite -73.1)
```

**Not financial advice.** v1 generates and delivers signals. It does not place
orders and it does not size anything against an account balance. Short-horizon
crypto signals carry a high false-positive rate, which is why the app tracks
and publishes its own hit rate and drawdown rather than implying certainty.

## What it does

1. **Scans** every spot market on one exchange, on a cadence you set.
2. **Screens** in two stages — a cheap liquidity filter, then a ranking by how
   *live* each setup is — so deep analysis only runs on a shortlist.
3. **Scores** each finalist on a -100 (strong short) to +100 (strong long)
   scale, one score per analysis leg.
4. **Fuses** the legs into a composite, and fires above +60 or below -60.
5. **Delivers** the card to a live dashboard and to Telegram or a webhook.
6. **Grades** every signal it ever fired, so the win rate is measured, not
   claimed.

## Quick start

```bash
pip install -e ".[api]"

cryptosignal screen            # what the funnel sees right now; fires nothing
cryptosignal scan --once       # one full cycle
cryptosignal serve --scan      # dashboard on :8000, scan loop beside it
cryptosignal stats             # the track record so far
```

No credentials are needed to scan: the exchange market data this uses is
public. Telegram alerts need a bot token (see `.env.example`).

## The two-stage funnel

A cycle cannot afford a full indicator pass over 400 markets, and it does not
need one. Most markets are doing nothing at any given moment.

**Stage 1 — universe filter.** 24h quote volume above a floor, bid/ask spread
below a ceiling, no stablecoins or wrapped tokens, and nothing that already
has an open signal. Cheap, runs on the ticker sweep every market shares.

**Stage 2 — opportunity filter.** One indicator pass per survivor, scored on
how live the setup is, independent of direction:

| Component | What it measures |
|---|---|
| Volatility expansion | ATR against the median of its own last 50 bars |
| Volume anomaly | This bar's volume against its trailing average |
| Price action | A fresh break of a pivot level, or coiling against one |
| Catalyst | *(phase 3 — the news/sentiment trigger)* |

The top N advance. The score is deliberately direction-agnostic: "something is
happening here" is a different question from "which way", and mixing them would
let a grinding downtrend crowd out a coiling breakout.

## Scoring

Three legs, each reporting -100..+100, weighted per the spec:

| Leg | Weight | Status |
|---|---|---|
| Technical | 50% | **shipped** |
| Fundamental / on-chain | 25% | phase 2 |
| News / sentiment | 25% | phase 3 |

**Only legs that actually reported get a vote, and the weights are
renormalised over those.** That is what lets phase 1 run technical-only at full
strength without pretending the other two legs said "neutral" — a leg with no
data is *silent*, not neutral, and the two are very different things. Adding
the on-chain leg later is a new module and one line in the scanner, not a
reweighting.

The technical leg breaks down as:

| Component | Weight | Inputs |
|---|---|---|
| Trend | 30% | EMA 9/21/50/200 stack, distance from the long EMA, DI spread, scaled by ADX |
| Momentum | 25% | RSI, MACD histogram level and slope, stochastic |
| Volatility | 10% | Bollinger position, multiplied by whether the range is expanding |
| Volume | 15% | OBV slope for direction, relative volume for conviction |
| Structure | 20% | Distance from rolling VWAP, fresh break of a pivot level |

Two details worth knowing:

- **Oscillators decay past the extremes.** An RSI of 95 is a worse entry than
  an RSI of 75. Without the decay both clip to full marks and the card rates
  the worst entry in the move exactly as highly as the best one.
- **A component with no data abstains rather than voting zero.** A neutral
  vote drags a strong reading toward the middle; an abstention does not.

### Fusion and the card

A weighted sum produces the composite. Above +60 is a long, below -60 a short,
anything between fires nothing. When the legs disagree — bullish technicals,
bearish on-chain flow — **the signal still fires, flagged "reduced confidence"
rather than suppressed.** Suppressing disagreement hides exactly the cases most
worth looking at. In phase 1 every card carries the flag, because one leg is
thinner evidence than the three-leg design assumes, and the card says so.

Each card carries direction, confidence %, entry zone, stop, two targets, the
suggested holding window, and the top three factors that drove the score —
ranked by how much each actually moved its leg, not by raw score.

### Levels

Everything is derived from ATR and the nearest pivot, so the numbers scale with
each coin's own volatility instead of assuming a fixed percentage:

- **Entry zone** reaches back against the trade by 0.25 ATR — a pullback entry
  — and only 0.1 ATR beyond current price, so chasing is bounded.
- **Stop** is 1.5 ATR, widened to clear a nearby pivot, then hard-capped at
  2.5 ATR so a distant support cannot quietly turn a scalp into an open-ended
  bet.
- **Targets** sit at 1.5R and 2.5R.
- **Holding window** is kinematic: how long the far target takes at the coin's
  recent pace, clamped to the spec's 15-minute-to-72-hour band.

## Outcome tracking

Every signal is written down the moment it fires, with the levels it fired at,
and only its outcome columns are ever updated — grading a call against levels
you edited afterwards is grading nothing. The accounting is deliberately
pessimistic:

- **The stop is checked before the target.** With one price per cycle we cannot
  know which came first inside the bar, and assuming the good one is how a
  backtest flatters itself.
- **An expiry is marked to market**, not to the best price the signal ever saw.
  Peak R is recorded separately, as information, never as a result.

`cryptosignal stats` and `/api/performance` report closed count, hit rate,
expectancy in R, total R, max drawdown, average hold, and a long/short split.

## The kill-switch

New signals stop when the feed is degraded: too many OHLCV fetch failures in a
cycle, or too many charts that arrived but have stopped updating. A chart that
has frozen is just as degraded as one that failed to arrive — counting only
hard failures would let a venue that has halted a market keep producing signals
off an hour-old chart.

**The kill-switch never stops the tracker.** Halting new calls but continuing
to grade open ones is the whole point; halting both would leave open positions
ungraded, which is worse than firing nothing.

## Dashboard and alerts

`cryptosignal serve --scan` puts the dashboard on `:8000`: a live-scrolling
feed of signal cards with a countdown to each holding window's end, filters by
status, direction and confidence, the current watchlist with its setup scores,
and the track record. It updates over server-sent events, so a new signal
appears the moment it fires rather than on a refresh.

| Endpoint | |
|---|---|
| `GET /` | the dashboard |
| `GET /health` | liveness, last cycle, halt state |
| `GET /api/signals` | the feed, filterable by `status`, `direction`, `symbol`, `min_confidence` |
| `GET /api/signals/{id}` | one card plus its event history |
| `GET /api/candidates` | the current shortlist and why each coin is on it |
| `GET /api/performance` | the track record |
| `GET /api/status` | last cycle plus the resolved tuning |
| `GET /api/features/{symbol}` | the indicator readings behind a score |
| `GET /api/stream` | server-sent events |

Telegram is the fastest channel and ships first; a generic JSON webhook takes
the same card as structured data. A channel that is not configured is simply
absent, and **a channel that throws is logged and skipped** — a Telegram outage
must never stop a scan cycle or lose a signal. The database is the record;
alerts are a copy.

## Configuration

Every tunable is an environment variable with a working default — see
`.env.example` for the full list and `cryptosignal config` for what resolved.
Nothing in the scoring path is a literal buried in the code, because a weight
you cannot find is a weight you cannot backtest. The weights are validated at
startup: leg weights, technical component weights and setup weights each have
to sum to 1.0, or the process refuses to start.

## Deploying

```bash
docker build -t cryptosignal .
docker run -p 8000:8000 -v cryptosignal-data:/data --env-file .env cryptosignal
```

The `Procfile` covers Railway and Render. SQLite needs a writable path that
survives a redeploy — mount a volume at `/data`, or the track record resets
every deploy.

## Architecture

```
exchange.py   ingestion    ccxt -> MarketSnapshot / Candles, cached and throttled
features.py   one pass     every indicator reading both stages need
screen.py     stage 1+2    the funnel
legs/         analysis     one module per leg, each -100..+100 with reasons
fusion.py     the call     renormalised weighted sum -> direction + confidence
levels.py     the numbers  entry / stop / targets / window, all ATR-relative
tracker.py    grading      stop, target or expiry -> a realised R
store.py      persistence  SQLite: signals, events, cycles
scanner.py    the cycle    order of operations, capacity, kill-switch
alerts/       delivery     Telegram, webhook
api/          delivery     FastAPI + the single-file dashboard
```

The scoring path never touches a network or a database, which is why the test
suite can drive it against synthetic charts with a known shape.

```bash
pip install -e ".[api,dev]"
pytest                    # 174 tests
ruff check .
```

## Status against the phased plan

| Phase | Scope | |
|---|---|---|
| 1 — MVP | one exchange, technical-only scoring, top-20 universe, signal feed, Telegram alerts | **done** |
| 2 | on-chain leg (netflow, funding, OI), wider universe, outcome tracking | outcome tracking **done**; the leg is the next module |
| 3 | news/sentiment leg, three-leg fusion | fusion is already leg-agnostic |
| 4 | backtesting harness to tune the weights | every weight is already external and named |
| 5 | exchange auto-execution, opt-in | out of scope for v1 by design |

## Before you point this at anything real

- The default universe is 20 coins on one venue. The spec's 200-300 needs a
  rate budget this has not been measured against.
- The thresholds and weights are reasoned defaults, **not backtested ones**.
  That is phase 4, and until then the published hit rate is the only evidence
  the tuning works.
- Distributing "trading signals" may need local financial-services
  registration depending on where you are. That is a one-time check worth
  doing before any launch beyond personal use.

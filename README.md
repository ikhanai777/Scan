# cryptosignal

A market scanner that screens the crypto universe on a fixed cadence, ranks
live setups, and issues a long or short call with an entry zone, a stop, two
targets and a holding window.

All five phases of the spec are built: three-leg scoring, outcome tracking, a
backtest harness that replays real history, and opt-in execution.

**Not financial advice.** Short-horizon crypto signals carry a high
false-positive rate, which is why this app measures and publishes its own hit
rate and drawdown rather than implying certainty. Execution is off by default,
and turning it on takes two independent switches.

---

## Run it on localhost

```bash
git clone https://github.com/ikhanai777/Scan.git && cd Scan
make install          # venv + deps + .env from the template
make doctor           # prove every live source, end to end
make serve            # dashboard on http://localhost:8000
```

Or with Docker:

```bash
cp .env.example .env
docker compose up --build        # http://localhost:8000
```

`make help` lists every target. **No API keys are needed.** Price, funding rate,
open interest and the order book come from the exchange; TVL, the Fear & Greed
index and the news feeds are public and keyless. Keys only buy extra sources.

### Start with `doctor`

"It runs" and "it is reading a live market" are different claims, and only the
second one matters. `doctor` walks the whole pipeline against the configured
venue and every data provider, and prints what it actually measured:

```
  [ok]   exchange       binance answered in 240ms
  [ok]   markets        2,183 live spot markets, 512 against USDT
  [ok]   timeframe      15m candles supported
  [ok]   live price     BTC/USDT 63,204.5  912M 24h volume, 0.16bps spread
  [ok]   stage 1        20 of 512 markets clear the floor
  [ok]   candles        BTC/USDT: 300 bars, newest opened 4.2 min ago
  [ok]   indicators     ATR 0.41% of price, RSI 54, ADX 21, rel volume 0.87x
  [ok]   scoring        BTC/USDT: setup 31, technical +18, composite +18 -> no signal
  [ok]   funding / OI   BTC/USDT: funding +0.0100%, open interest 78,412
  [ok]   order book     BTC/USDT: +12% imbalance on 41.2M resting near mid
  [ok]   DefiLlama      Aave $11,204M TVL
  [ok]   Fear & Greed   61 (Greed)
  [ok]   news feeds     87 headline(s) from 4/4 feed(s) in the last 48h
  [ok]   rate budget    21 calls per cycle, ~11s at 500ms spacing
  [ok]   execution      disabled -- signals only, no orders (the default)
  [ok]   database       cryptosignal.db: 0 open, 0 closed signals
```

*(Shape of the output, not a recorded run — the numbers are whatever your venue
says when you run it.)*

It exits non-zero when the pipeline cannot run, and every failure names the
change that fixes it. **The first failure most people hit is geographic:**
Binance answers HTTP 451 from several jurisdictions. `doctor` says so and lists
venues that will serve you (`CS_EXCHANGE=kraken`, `coinbase`, `kucoin`,
`bybit`, `okx`, `gateio`). A different venue may also need `CS_QUOTE=USD` and a
lower `CS_MIN_VOLUME_24H`; `doctor` catches both.

A secondary provider that stays silent is a **warning, never a failure** — a
dead news feed costs the sentiment leg some evidence, it does not stop the app.

---

## The two-stage funnel

A cycle cannot afford a full indicator pass over 400 markets, and does not need
one. Most markets are doing nothing at any moment.

**Stage 1 — universe filter.** 24h quote volume above a floor, spread below a
ceiling, no stablecoins or wrapped tokens, nothing that already has an open
signal. Runs on the ticker sweep every market shares.

**Stage 2 — opportunity filter.** One indicator pass per survivor, scored on how
*live* the setup is, independent of direction:

| Component | What it measures |
|---|---|
| Volatility expansion | ATR against the median of its own last 50 bars |
| Volume anomaly | This bar's volume against its trailing average |
| Price action | A fresh break of a pivot level, or coiling against one |

The top N advance. The score is direction-agnostic on purpose: "something is
happening here" is a different question from "which way", and mixing them lets
a grinding downtrend crowd out a coiling breakout.

---

## Scoring: three legs

| Leg | Weight | Sources | Key needed |
|---|---|---|---|
| Technical | 50% | exchange OHLCV | no |
| Fundamental / on-chain | 25% | funding, open interest, order book, DefiLlama | no |
| News / sentiment | 25% | RSS (+ CryptoPanic), Fear & Greed | no (yes for CryptoPanic) |

**Only legs that actually reported get a vote, and the weights are renormalised
over those.** A leg with no data is *silent*, not neutral, and the two are very
different things — a neutral vote drags a strong reading toward the middle, an
abstention does not. The same rule runs one level down: a component whose
inputs never warmed up abstains rather than voting zero.

This is what makes a missing API key cost accuracy instead of correctness, and
it is visible on every card: each shows `TECH`, `FUND`, `SENT` chips, greyed
with a dash where a leg had nothing.

### Technical (50%)

| Component | Weight | Inputs |
|---|---|---|
| Trend | 30% | EMA 9/21/50/200 stack, distance from the long EMA, DI spread, scaled by ADX |
| Momentum | 25% | RSI, MACD histogram level and slope, stochastic |
| Volatility | 10% | Bollinger position, multiplied by whether the range is expanding |
| Volume | 15% | OBV slope for direction, relative volume for conviction |
| Structure | 20% | Distance from rolling VWAP, fresh break of a pivot level |

**Oscillators decay past the extremes.** An RSI of 95 is a worse entry than 75.
Without the decay both clip to full marks and the card rates the worst entry in
the move exactly as highly as the best one.

### Fundamental / on-chain (25%)

| Component | Weight | Source |
|---|---|---|
| Funding rate | 30% | the venue's perpetual, via ccxt |
| Open interest | 20% | the venue's perpetual, via ccxt |
| Book pressure | 25% | the venue's order book |
| TVL trend | 25% | DefiLlama public API |

**Funding is read contrarian at the extremes.** Heavy positive funding means the
crowd is levered long and paying to stay there — crowded, and the side that gets
liquidated first. So it scores *bearish*. That sign surprises people, and it is
the whole point of the component.

**Open interest has no direction on its own.** Rising OI with rising price is
new longs; rising OI with falling price is new shorts; falling OI is unwinding,
which reads against the move at half strength.

**What is deliberately missing:** the spec also lists exchange netflow and whale
wallet activity. Every source for those is paid and keyed, and there is no free
equivalent. Rather than approximate them with something that is not them, those
components are absent and the leg renormalises over the four that reported.
Order-book depth imbalance is included as its own measurement, not as a stand-in
for netflow.

### News / sentiment (25%)

| Component | Weight | Source |
|---|---|---|
| News impact | 50% | public RSS, plus CryptoPanic when keyed |
| Mention velocity | 25% | the same headlines, against the coin's own baseline |
| Market regime | 25% | Fear & Greed index (alternative.me) |

Headlines are classified against an event lexicon and weighted by a **recency
decay** — impact halves every `CS_NEWS_HALF_LIFE_H` hours. A coin no headline
mentions produces no reading at all.

Two details that keep keyword matching honest:

- **A bare ticker matches case-sensitively.** "ONE", "GAS", "SUN" and "NEAR" are
  real tickers and also ordinary English; a case-insensitive match turns "One
  more reason gas fees will fall" into coverage of three coins.
- **A headline the lexicon cannot call scores zero.** "Coinbase lists token days
  after exploit drained the treasury" nets to a mildly *bullish* +5 if you just
  subtract. Admitting the classifier cannot read it is better.

### Fusion and the card

Above +60 is a long, below −60 a short, between fires nothing. When legs
disagree — bullish technicals, bearish on-chain flow — **the signal still fires,
flagged "reduced confidence" rather than suppressed.** Suppressing disagreement
hides exactly the cases most worth looking at.

Each card carries direction, confidence, entry zone, stop, two targets, the
holding window, the per-leg scores, and the top three factors that drove it —
ranked by how much each actually moved its leg.

### Levels

Everything derives from ATR and the nearest pivot, so the numbers scale with
each coin's own volatility:

- **Entry zone** reaches back against the trade by 0.25 ATR and only 0.1 ATR
  beyond current price, so chasing is bounded.
- **Stop** is 1.5 ATR, widened to clear a nearby pivot, hard-capped at 2.5 ATR.
- **Targets** at 1.5R and 2.5R.
- **Holding window** is kinematic: how long the far target takes at the coin's
  recent pace, clamped to the spec's 15-minute-to-72-hour band.

---

## Outcome tracking

Every signal is written down the moment it fires, with the levels it fired at,
and only its outcome columns are ever updated. The accounting is deliberately
pessimistic:

- **The stop is checked before the target.** With one price per cycle we cannot
  know which came first inside the bar, and assuming the good one is how a
  backtest flatters itself.
- **An expiry is marked to market**, never to the best price the signal saw.
  Peak R is recorded separately, as information, never as a result.

`cryptosignal stats` and `/api/performance` report closed count, hit rate,
expectancy in R, total R, max drawdown, average hold, and a long/short split.

---

## Backtesting (phase 4)

```bash
cryptosignal backtest --top 5 --bars 2000      # real history from your venue
cryptosignal sweep --min-trades 30             # tune against it
```

Both run on OHLCV paged from the exchange. There is no simulated price series:
if the venue will not serve the history, the backtest does not run.

**Three rules keep it from flattering itself:**

1. **No lookahead.** A signal at bar *t* uses bars `0..t` only and is entered at
   bar `t+1`'s **open** — the first price actually tradeable after the decision.
2. **The stop is checked before the target, inside every bar**, against the real
   high and low.
3. **An expiry is marked to the close** of the bar where the window ran out.

`sweep` ranks configurations by expectancy but sorts anything below
`--min-trades` beneath everything that clears it. Four trades and a perfect
record is not evidence, and ranking it first is how a sweep talks you into
overfitting.

**What it can and cannot tune.** It replays the technical leg, the screen and
the level logic, so it can tune their weights, the threshold band, the stop
distance and the target multiples. It cannot tune the *leg* weights — that needs
historical funding, order books and headlines aligned to each bar, and none are
available free at bar resolution. The leg split stays at the spec's 50/25/25.

---

## Execution (phase 5)

Off by default. `CS_EXECUTION_MODE` takes `disabled`, `paper` or `live`.

**Paper** records orders against real prices and sends nothing anywhere. It does
not model slippage, queue position or partial fills — a paper fill is the *best
case*, and the gap between it and a live fill is the cost of finding out for
real.

**Live places real orders with real money**, and needs two independent switches:

```bash
CS_EXECUTION_MODE=live
CS_LIVE_CONFIRM=I understand this places real orders
```

One environment variable is too easy to set by accident in a deploy config. A
live configuration missing the phrase or the API keys **refuses to start** — it
never falls back to paper silently, because paper-trading someone who believes
they are live is its own kind of failure.

Every order passes one gate, which denies by default:

| Limit | Default |
|---|---|
| Risk per trade | 0.5% of equity, sized from the distance to the stop |
| Max per order | 250 |
| Max open positions | 3 |
| Max orders per day | 10 |
| Daily loss limit | 100, and it **latches** for the rest of the UTC day |
| Min confidence | 70% |
| Reduced-confidence signals | refused unless opted in |

Entries are **post-only limit orders** inside the entry zone — a market order on
a thin book is how a scalp becomes a donation. Exits are market orders, because
getting out is worth the spread. **An exit that fails engages the kill switch**,
which is one-way within a process: a human restarts to clear it.

---

## Dashboard and alerts

`make serve` puts the dashboard on `:8000`: a live-scrolling feed of signal
cards with per-leg scores and a countdown to each holding window, filters by
status, direction and confidence, the watchlist with setup scores, the track
record, live data-source health, and the execution panel with its limits. It
updates over server-sent events.

| Endpoint | |
|---|---|
| `GET /` | the dashboard |
| `GET /health` | liveness, last cycle, halt state |
| `GET /api/signals` | the feed, filterable |
| `GET /api/signals/{id}` | one card plus its event history |
| `GET /api/candidates` | the current shortlist and why each coin is on it |
| `GET /api/performance` | the track record |
| `GET /api/sources` | which providers answered on the last cycle |
| `GET /api/execution` | order state and the risk limits |
| `GET /api/status` | last cycle plus the resolved tuning |
| `GET /api/features/{symbol}` | the indicator readings behind a score |
| `GET /api/stream` | server-sent events |

Telegram ships first; a JSON webhook takes the same card as structured data. A
channel that throws is logged and skipped — an outage must never stop a cycle or
lose a signal. The database is the record; alerts are a copy.

---

## The kill-switch

New signals stop when the feed is degraded: too many OHLCV fetch failures in a
cycle, or too many charts that arrived but stopped updating. A frozen chart is
as degraded as one that failed to arrive.

**It never stops the tracker.** Halting new calls while continuing to grade open
ones is the point; halting both would leave open positions ungraded.

---

## Testing, and what is real

```bash
make dev && make test        # 337 tests, 14 skipped until you record fixtures
make lint
```

Being precise about this, because it matters:

**Nothing synthetic reaches a database, a signal card, the dashboard, the
backtest or an order.** Every number those produce comes from a live provider.

**The test suite uses two kinds of stand-in, both deliberate:**

- *Deterministic price series*, so "an uptrend scores long" is an assertion
  rather than an anecdote. No live market gives you a path whose right answer is
  known in advance.
- *Stub transports* (a fake ccxt client, a mock HTTP layer), so failure paths —
  a null `quoteVolume`, a 451, a dead feed, a thin book — can be exercised on
  demand. You cannot ask a real venue to return a malformed ticker.

**For real data in the loop:**

```bash
cryptosignal record --top 5 --bars 600
make test          # the 14 skipped tests now run against real market history
```

Those tests assert **invariants**, not outcomes — nobody knows what BTC *should*
have scored last Tuesday, so asserting an outcome would be inventing one. They
check that scores stay in range, no NaN escapes, levels stay ordered, and the
engine survives shapes a synthetic series never produces: gaps, halted bars,
repeated closes, volume spikes.

**What has not happened here:** the machine this was built on has no outbound
route to any exchange — thirteen were tried, all blocked by its egress policy —
so no part of this has met a live order book. `doctor` is how you close that gap
in one command.

---

## Architecture

```
exchange.py      ingestion    ccxt -> snapshots / candles / history, cached and throttled
sources/         ingestion    funding, open interest, order book, TVL, news, Fear & Greed
features.py      one pass     every indicator reading both screen stages need
screen.py        stage 1+2    the funnel
legs/            analysis     technical, fundamental, sentiment -- each -100..+100 with reasons
context.py       analysis     the shared providers, fetched once per cycle
fusion.py        the call     renormalised weighted sum -> direction + confidence
levels.py        the numbers  entry / stop / targets / window, all ATR-relative
tracker.py       grading      stop, target or expiry -> a realised R
backtest.py      phase 4      replay over real history, and the parameter sweep
execution/       phase 5      risk gate, paper and live brokers
store.py         persistence  SQLite: signals, events, cycles
scanner.py       the cycle    order of operations, capacity, kill-switch
alerts/          delivery     Telegram, webhook
api/             delivery     FastAPI + the single-file dashboard
```

The scoring path touches no network and no database. That is what makes it
testable, and it is why the legs take readings as arguments rather than fetching
their own.

---

## Configuration

Every tunable is an environment variable with a working default — see
`.env.example`, and `cryptosignal config` for what resolved. Nothing in the
scoring path is a literal buried in the code, because a weight you cannot find
is a weight you cannot backtest. Leg weights, each leg's component weights and
the setup weights must each sum to 1.0 or the process refuses to start.

---

## Before you point this at anything real

- **No part of this has met a live order book yet.** `doctor` first, then a real
  cycle. That is the first real evidence.
- **The weights and the lexicon are reasoned, not fitted.** `sweep` against your
  own venue's history is what turns them into something evidenced — and a
  configuration that only wins on one window is fitted noise.
- **Paper before live**, for long enough that the track record means something.
  The published hit rate is the only evidence the tuning works.
- Distributing "trading signals" may need local financial-services registration
  depending on where you are. Worth a one-time check before any launch beyond
  personal use.

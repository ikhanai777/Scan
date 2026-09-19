# Runbook: stand this up on a PC, unattended

Written for an autonomous agent with a shell on the target machine. A human can
follow it too — it is just a checklist — but every step is phrased so an agent
can act on it and then *prove* the step worked before moving on.

**On Nous Hermes specifically.** This was written for a Hermes agent at
`hermes-agent.nousresearch.com`, and I could not reach that host from the
machine I built this on — outbound requests to it are blocked by that
environment's network policy, so I never saw its interface and cannot tell you
which buttons it has. So nothing below assumes Hermes. It assumes only: a shell,
a working directory, and the ability to read a command's exit code. Paste this
file to Hermes (or any other agent) as the task; it needs no other context.

---

## What this is

A crypto market scanner. It screens a coin universe on a real exchange feed,
ranks live setups, and publishes long/short calls with entry, stop, two targets
and a holding window, plus its own hit rate. It reads markets and writes a
dashboard. **Order placement is off by default and stays off unless a human
turns it on deliberately** — see [Do not touch](#do-not-touch).

Every number it shows comes from a live source. There is no demo mode and no
synthetic fallback: if a feed is unreachable the app says so rather than
inventing a value, which is why step 4 exists.

## Before you start

| Requirement | Check | If missing |
|---|---|---|
| Python **3.11 or newer** | `python3 --version` | install 3.11+; 3.10 will fail at install |
| `git` | `git --version` | install it |
| Outbound HTTPS to an exchange | step 4 proves it | see [When the exchange refuses](#when-the-exchange-refuses) |
| ~400 MB disk | `df -h .` | free some |

No API keys are needed. Price, funding, open interest and the order book come
from the exchange's public endpoints; TVL, Fear & Greed and the news feeds are
public and keyless. Keys only add sources.

On Windows, run every command below inside **WSL** or **Git Bash**. The
`Makefile` targets are POSIX shell. (A Windows-native path exists — see
[Without make](#without-make).)

---

## The five steps

### 1. Clone

```bash
git clone https://github.com/ikhanai777/Scan.git cryptosignal
cd cryptosignal
```

**Proves it worked:** `ls Makefile pyproject.toml cryptosignal/` lists all three.

### 2. Install

```bash
make install
```

This creates `.venv/`, installs the app and its API extras into it, and copies
`.env.example` to `.env` if you have no `.env` yet. It takes well under a minute
on a normal connection and prints `installed. next:  make doctor`.

**Proves it worked:** `.venv/bin/cryptosignal --help` exits 0.

> Do not `pip install` into the system Python. Everything below calls
> `.venv/bin/...` explicitly, so an activated shell is never required.

### 3. Configure — usually nothing to do

`.env` already has a working value for every setting, and it is commented. Read
it before changing anything. The two you may actually need:

```ini
CS_EXCHANGE=binance     # the venue to read
CS_QUOTE=USDT           # kraken and coinbase quote in USD, not USDT
```

Leave `CS_EXECUTION_MODE=disabled` alone.

### 4. Prove the data is real — **do not skip this**

```bash
.venv/bin/cryptosignal doctor
```

`doctor` calls every live source end to end and prints a PASS/FAIL line each.
**It exits `0` only when the app is ready, and `1` otherwise** — that exit code
is the gate. Do not continue on a `1`.

A healthy run ends with a readiness line and exit 0. A blocked one looks like
this, and is the single most common failure:

```
  [FAIL] exchange       binance did not answer
                        Could not reach binance at all (NetworkError). Check
                        outbound HTTPS from this machine -- a sandbox or
                        corporate proxy that allowlists hosts will block
                        exchange APIs.

  Not ready. Fix the failure above, then run doctor again.
```

That message means the machine cannot reach the exchange — not that the app is
broken. See [When the exchange refuses](#when-the-exchange-refuses).

A `[FAIL]` on the exchange is fatal; the app has nothing to scan. A `[WARN]` or
silent source on TVL, news or Fear & Greed is not fatal — the scoring
renormalises over whatever legs did report, and the dashboard greys out the ones
that went quiet, so a missing leg is visible instead of being quietly counted as
neutral.

### 5. Run it

```bash
make serve
```

Scanner and dashboard in one process. It listens on **<http://localhost:8000>**.

**Proves it worked**, from a second shell:

```bash
curl -s localhost:8000/health
```

Expect JSON. Read `status`:

- `"ok"` — scanning, and the last cycle finished cleanly. Done.
- `"degraded"` — reachable but no clean cycle yet. Normal for the first ~2
  minutes; `CS_SCAN_INTERVAL_S` defaults to 120, so the first cycle has to land.
  Still `degraded` after 5 minutes, or `scanner_attached: false`, means step 4
  was skipped or `make serve` was started without `--scan`.
- no answer at all — the process died. Read its output.

An empty feed is not a fault. The thresholds are deliberately strict
(`CS_LONG_THRESHOLD=60`), so a quiet market produces zero calls and a
watchlist. The **watching** panel on the right fills first; signal cards appear
only when something clears the bar. If you want to see the machinery work
immediately, run one cycle in the foreground and read the log:

```bash
.venv/bin/cryptosignal scan --once
```

---

## Report back

When you are done, tell the human exactly this much:

1. The `doctor` verdict — ready, or which source failed and the reason it gave.
2. The URL, and the `status` field from `/health`.
3. `CS_EXCHANGE` in use, and whether you had to change it from the default.
4. Open signals and watchlist size right now:
   `curl -s localhost:8000/api/signals | head -c 400`
5. That execution is `disabled` (confirm with
   `curl -s localhost:8000/api/execution`), unless the human told you otherwise.

Do not report "set up successfully" on a `doctor` exit of 1. A dashboard that
loads with no live feed behind it is the one outcome this app is built to make
impossible, and saying otherwise wastes the human's money.

---

## When the exchange refuses

Two different failures, two different fixes.

**HTTP 451, or doctor naming a geographic restriction.** Binance blocks whole
jurisdictions, including the US. Switch venues in `.env`, then re-run `doctor`:

```ini
CS_EXCHANGE=kraken
CS_QUOTE=USD          # kraken and coinbase quote USD; kucoin/bybit/okx/gateio use USDT
```

Working alternatives: `kraken`, `coinbase`, `kucoin`, `bybit`, `okx`, `gateio`.

A smaller venue has thinner books, so the default liquidity floor may screen out
everything and leave the watchlist empty. Lower it:

```ini
CS_MIN_VOLUME_24H=5000000
CS_MAX_SPREAD_BPS=15
```

**NetworkError, timeout, or a proxy error.** The machine itself cannot get out.
Corporate networks, VPNs and agent sandboxes commonly allowlist hosts, and
exchange APIs are rarely on the list. This is not something to work around:
**do not disable TLS verification and do not route around a policy block.**
Report the host that was refused and let the human decide.

## Other things that go wrong

| Symptom | Cause | Fix |
|---|---|---|
| `make install` fails compiling a wheel | Python < 3.11, or no build tools | install 3.11+; on Debian/Ubuntu `apt install build-essential python3-dev` |
| `Address already in use` | something holds :8000 | `CS_API_PORT=8010 make serve` |
| Dashboard loads, panels read `--` | API up, scanner not attached | use `make serve` (it passes `--scan`), not a bare `serve` |
| Feed empty for hours | strict thresholds, or a thin venue | expected in chop; lower `CS_MIN_VOLUME_24H`, or check the watchlist is populating |
| Hit rate looks incredible | tiny sample | the header prints `n=` beside it for exactly this reason; under 30 closed trades the panel says "too few to conclude" |
| Track record reset after a redeploy | the SQLite file was deleted | `CS_DB_PATH` must point somewhere persistent; Docker already uses a named volume |

## Do not touch

`CS_EXECUTION_MODE` has three values: `disabled` (default), `paper`, `live`.

**An agent must never set this to `live`.** `live` places real orders with real
money. It additionally requires a second variable, `CS_LIVE_CONFIRM`, set to an
exact confirmation phrase, plus exchange API keys with trade permission — two
independent switches, deliberately, because one environment variable is far too
easy to set by accident in a deploy config. Both switches are a human decision,
made by the person whose money it is, never inferred from an instruction to
"set it up" or "make it trade".

`paper` records orders against real prices and sends nothing anywhere. It is
safe, but still set it only if the human asks for it by name.

Likewise, do not put API keys, tokens or a Telegram bot token into `.env`
unless the human hands them to you for that purpose, and never paste the
contents of `.env` into a chat, a log, an issue or a commit.

## Keeping it running

`make serve` stops when its shell closes. For a box that should hold it up
across reboots, use Docker — same app, restart policy included, SQLite on a
named volume so the published track record survives a rebuild:

```bash
make up       # builds and starts, http://localhost:8000
make logs     # follow
make down     # stop
```

Docker needs the daemon running and is the better choice on Windows.

## Without make

Same five steps, no `make`, works in PowerShell with the paths adjusted
(`.venv\Scripts\` instead of `.venv/bin/`):

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[api]"
cp .env.example .env
.venv/bin/cryptosignal doctor
.venv/bin/cryptosignal serve --scan --host 0.0.0.0
```

## Leaving the machine

`make serve` binds `0.0.0.0`, so the dashboard is reachable from other devices
on the same network. That is intentional for a phone on the same wifi, and it
means **anyone on that network can read it**. There is no login. Do not port
forward it to the internet. To keep it strictly local, bind the loopback:

```bash
.venv/bin/cryptosignal serve --scan --host 127.0.0.1
```

## What you are looking at

Each signal card carries direction, symbol, market regime, confidence, the entry
band, stop and both targets, a sparkline framed on the trade's own levels, the
per-leg scores, the drivers that moved the score, and a timestamped milestone
line for every price event — fired, target 1, target 2, stop, expiry. A leg with
no data shows `--` and is greyed, never scored as neutral. `j`/`k` move through
cards, `Enter` expands one, `/` searches, `Esc` clears the filters.

The right rail holds the published track record and equity curve, the
watchlist, correlation-weighted portfolio risk, execution state, which sources
answered on the last cycle, and the last cycle's timings.

It is decision support, not an auto-trader, and short-horizon crypto signals
carry a high false-positive rate. The disclaimer on the page is not decoration.

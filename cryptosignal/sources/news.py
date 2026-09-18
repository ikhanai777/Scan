"""Crypto news headlines, from public RSS and optionally CryptoPanic.

The RSS feeds are keyless and public, which is why they are the default. Their
URLs live in `DEFAULT_FEEDS` but are fully overridable with `CS_NEWS_FEEDS`,
because publishers move their feeds and a hardcoded dead URL is worse than a
configurable one.

**These URLs have not been reached from the machine this was built on** -- it
has no outbound route to them. `cryptosignal doctor` fetches every configured
feed and reports which ones answered, how many items they returned, and how
recent those items are. Trust that output, not this docstring.

Classification is keyword-based with a recency decay, per the spec: an event's
impact halves every `news_half_life_hours`. Keyword matching is a blunt
instrument and this module does not pretend otherwise -- a headline that
matches nothing scores zero and says so, and a coin that no headline mentions
produces no reading at all rather than a neutral one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from xml.etree import ElementTree

from . import HTTPSource

log = logging.getLogger(__name__)

DEFAULT_FEEDS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://bitcoinmagazine.com/feed",
)
CRYPTOPANIC_URL = "https://cryptopanic.com/api/v1/posts/"
CACHE_SECONDS = 300.0
MAX_ITEMS_PER_FEED = 120

# Event lexicons. The weights are a judgement about how hard each event type
# moves a short-horizon price, not a measurement -- phase 4's backtest is what
# turns them from reasoned into evidenced.
BULLISH_TERMS: dict[str, float] = {
    "lists": 85, "listing": 85, "will list": 95, "coinbase lists": 100,
    "binance lists": 100, "spot etf": 95, "etf approved": 100, "etf approval": 95,
    "partnership": 55, "partners with": 55, "integration": 45, "integrates": 45,
    "mainnet": 60, "upgrade": 50, "launch": 45, "launches": 45,
    "buyback": 70, "burn": 55, "staking rewards": 40, "airdrop": 50,
    "acquisition": 60, "acquires": 60, "funding round": 55, "raises": 50,
    "adoption": 50, "institutional": 45, "accumulating": 55, "whale buys": 60,
    "record high": 55, "breaks out": 50, "rally": 40, "surges": 45, "soars": 50,
}
BEARISH_TERMS: dict[str, float] = {
    "hack": 100, "hacked": 100, "exploit": 95, "exploited": 95, "drained": 95,
    "rug pull": 100, "rugpull": 100, "scam": 80, "fraud": 85, "ponzi": 85,
    "delist": 90, "delisting": 90, "delisted": 90, "halts": 70, "suspends": 65,
    "lawsuit": 75, "sues": 75, "sec charges": 90, "indicted": 90, "arrested": 85,
    "investigation": 60, "subpoena": 65, "ban": 75, "banned": 75, "crackdown": 70,
    "insolvent": 95, "bankruptcy": 95, "liquidated": 60, "liquidation": 55,
    "outflows": 50, "dumps": 55, "plunges": 55, "crashes": 65, "sell-off": 50,
    "exploit attempt": 80, "vulnerability": 65, "downtime": 50, "outage": 50,
}

# Symbol -> the words an article would actually use. Matching on the ticker
# alone produces nonsense: "ONE", "SUN" and "GAS" are all real tickers.
COIN_ALIASES: dict[str, tuple[str, ...]] = {
    "BTC": ("bitcoin",), "ETH": ("ethereum", "ether"), "SOL": ("solana",),
    "XRP": ("ripple", "xrp"), "ADA": ("cardano",), "AVAX": ("avalanche",),
    "DOGE": ("dogecoin",), "DOT": ("polkadot",), "MATIC": ("polygon",),
    "LINK": ("chainlink",), "LTC": ("litecoin",), "BCH": ("bitcoin cash",),
    "UNI": ("uniswap",), "ATOM": ("cosmos",), "XLM": ("stellar",),
    "NEAR": ("near protocol",), "APT": ("aptos",), "ARB": ("arbitrum",),
    "OP": ("optimism",), "INJ": ("injective",), "SUI": ("sui network", "sui"),
    "SEI": ("sei network",), "TIA": ("celestia",), "FIL": ("filecoin",),
    "ICP": ("internet computer",), "HBAR": ("hedera",), "VET": ("vechain",),
    "ALGO": ("algorand",), "AAVE": ("aave",), "MKR": ("maker", "makerdao"),
    "LDO": ("lido",), "CRV": ("curve finance", "curve dao"), "SNX": ("synthetix",),
    "RUNE": ("thorchain",), "GRT": ("the graph",), "SAND": ("the sandbox",),
    "MANA": ("decentraland",), "AXS": ("axie infinity",), "IMX": ("immutable",),
    "STX": ("stacks",), "TON": ("toncoin", "ton network"), "SHIB": ("shiba inu",),
    "PEPE": ("pepe coin", "pepe"), "WIF": ("dogwifhat",), "BONK": ("bonk",),
    "TRX": ("tron",), "ETC": ("ethereum classic",), "XMR": ("monero",),
}


@dataclass(frozen=True)
class NewsItem:
    title: str
    url: str
    published_at: datetime
    source: str

    def age_hours(self, now: datetime | None = None) -> float:
        now = now or datetime.now(UTC)
        return max(0.0, (now - self.published_at).total_seconds() / 3600.0)


@dataclass(frozen=True)
class NewsReading:
    """Everything the sentiment leg needs about one coin's coverage."""

    symbol: str
    impact: float                    # -100..+100, recency-weighted
    mentions_recent: int
    mentions_baseline: int
    top_items: tuple[tuple[NewsItem, float], ...]   # (item, its signed impact)

    @property
    def velocity(self) -> float | None:
        """Recent mention rate over its own baseline. None when there is no baseline."""
        if self.mentions_baseline <= 0:
            return None
        return self.mentions_recent / self.mentions_baseline


class NewsSource(HTTPSource):
    """Pulls every configured feed once per cycle, then answers per coin."""

    name = "news"

    def __init__(self, feeds: tuple[str, ...] = DEFAULT_FEEDS,
                 cryptopanic_token: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.feeds = feeds
        self.cryptopanic_token = cryptopanic_token
        #: Populated by the last `items()` call, so `doctor` can report per-feed health.
        self.feed_health: dict[str, str] = {}

    # -- fetching ---------------------------------------------------------

    def items(self, window_hours: float) -> list[NewsItem]:
        """Every headline from every configured source inside the window."""
        cached = self.cached("items")
        if cached is not None:
            return cached

        cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
        collected: list[NewsItem] = []
        self.feed_health = {}

        for feed in self.feeds:
            parsed = self._rss(feed)
            if parsed is None:
                self.feed_health[feed] = "unreachable"
                continue
            fresh = [item for item in parsed if item.published_at >= cutoff]
            self.feed_health[feed] = f"{len(parsed)} items, {len(fresh)} inside the window"
            collected.extend(fresh)

        if self.cryptopanic_token:
            panic = self._cryptopanic(cutoff)
            if panic is None:
                self.feed_health["cryptopanic"] = "unreachable or token rejected"
            else:
                self.feed_health["cryptopanic"] = f"{len(panic)} items"
                collected.extend(panic)

        # The same story runs on several outlets; counting it once per outlet
        # would turn one event into a velocity spike.
        deduped = _dedupe(collected)
        self.store("items", deduped, CACHE_SECONDS)
        return deduped

    def _rss(self, url: str) -> list[NewsItem] | None:
        text = self.get_text(url)
        if not text:
            return None
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as exc:
            log.warning("news: %s is not parseable XML (%s)", url, exc)
            return None

        out: list[NewsItem] = []
        # RSS 2.0 uses item/pubDate; Atom uses entry/published. Handle both,
        # because a publisher switching format should not silently zero a feed.
        for node in list(root.iter("item"))[:MAX_ITEMS_PER_FEED]:
            item = _from_rss_item(node, url)
            if item:
                out.append(item)
        if not out:
            for node in list(root.iter("{http://www.w3.org/2005/Atom}entry"))[:MAX_ITEMS_PER_FEED]:
                item = _from_atom_entry(node, url)
                if item:
                    out.append(item)
        return out

    def _cryptopanic(self, cutoff: datetime) -> list[NewsItem] | None:
        payload = self.get_json(CRYPTOPANIC_URL, params={
            "auth_token": self.cryptopanic_token, "public": "true", "kind": "news",
        })
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            return None
        out = []
        for entry in payload["results"]:
            if not isinstance(entry, dict):
                continue
            published = _parse_time(entry.get("published_at") or entry.get("created_at"))
            title = str(entry.get("title") or "").strip()
            if not title or published is None or published < cutoff:
                continue
            out.append(NewsItem(title, str(entry.get("url") or ""), published, "cryptopanic"))
        return out

    # -- reading ----------------------------------------------------------

    def read(self, base_symbol: str, window_hours: float, half_life_hours: float,
             now: datetime | None = None) -> NewsReading | None:
        """Coverage for one coin, or None when nothing mentioned it."""
        now = now or datetime.now(UTC)
        everything = self.items(window_hours)
        if not everything:
            return None

        patterns = _patterns_for(base_symbol)
        matched = [item for item in everything if _mentions(item.title, patterns)]
        if not matched:
            return None

        # Recent = the freshest third of the window, baseline = the rest, so
        # velocity is a coin's coverage against its own normal, not a constant.
        recent_cutoff = now - timedelta(hours=window_hours / 3.0)
        recent = [i for i in matched if i.published_at >= recent_cutoff]
        baseline = [i for i in matched if i.published_at < recent_cutoff]

        scored: list[tuple[NewsItem, float]] = []
        numerator = denominator = 0.0
        for item in matched:
            raw = _classify(item.title)
            decay = 0.5 ** (item.age_hours(now) / half_life_hours)
            scored.append((item, raw))
            numerator += raw * decay
            denominator += decay

        impact = numerator / denominator if denominator > 0 else 0.0
        scored.sort(key=lambda pair: abs(pair[1]), reverse=True)

        return NewsReading(
            symbol=base_symbol.upper(),
            impact=max(-100.0, min(100.0, impact)),
            mentions_recent=len(recent),
            # Scaled to the same window length as `recent`, so the ratio is a
            # rate against a rate rather than a count against a longer count.
            mentions_baseline=round(len(baseline) / 2.0),
            top_items=tuple(scored[:3]),
        )


# ---- parsing ---------------------------------------------------------------


def _from_rss_item(node, source: str) -> NewsItem | None:
    title = (node.findtext("title") or "").strip()
    published = _parse_time(node.findtext("pubDate") or node.findtext("date"))
    if not title or published is None:
        return None
    return NewsItem(title, (node.findtext("link") or "").strip(), published, source)


def _from_atom_entry(node, source: str) -> NewsItem | None:
    ns = "{http://www.w3.org/2005/Atom}"
    title = (node.findtext(f"{ns}title") or "").strip()
    published = _parse_time(node.findtext(f"{ns}published") or node.findtext(f"{ns}updated"))
    if not title or published is None:
        return None
    link_node = node.find(f"{ns}link")
    url = link_node.get("href", "") if link_node is not None else ""
    return NewsItem(title, url, published, source)


_RFC822 = "%a, %d %b %Y %H:%M:%S"


def _parse_time(raw: str | None) -> datetime | None:
    """RSS dates come in several shapes; an unparseable one drops the item."""
    if not raw:
        return None
    text = raw.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass
    from email.utils import parsedate_to_datetime
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _dedupe(items: list[NewsItem]) -> list[NewsItem]:
    """One story per headline, keeping the earliest sighting."""
    best: dict[str, NewsItem] = {}
    for item in items:
        key = re.sub(r"[^a-z0-9]+", " ", item.title.lower()).strip()
        existing = best.get(key)
        if existing is None or item.published_at < existing.published_at:
            best[key] = item
    return sorted(best.values(), key=lambda i: i.published_at, reverse=True)


# ---- matching and classification -------------------------------------------


def _patterns_for(base_symbol: str) -> tuple[re.Pattern, ...]:
    """Word-boundary patterns for a coin's ticker and its common names.

    Names match case-insensitively; a bare ticker matches **case-sensitively**,
    upper case only. "ONE", "GAS", "SUN" and "NEAR" are all real tickers and
    also ordinary English, and a case-insensitive ticker match turns a headline
    like "One more reason gas fees will fall" into coverage of three coins.
    """
    symbol = base_symbol.strip().upper()
    patterns = [
        re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
        for name in COIN_ALIASES.get(symbol, ()) if name
    ]
    if len(symbol) >= 3:
        patterns.append(re.compile(rf"\b{re.escape(symbol)}\b"))
    return tuple(patterns)


def _mentions(title: str, patterns: tuple[re.Pattern, ...]) -> bool:
    return any(pattern.search(title) for pattern in patterns)


#: Two opposing terms closer than this are a headline the lexicon cannot call.
AMBIGUITY_MARGIN = 30.0


def _classify(title: str) -> float:
    """-100..+100 for one headline. Zero means the lexicon has no call to make.

    Zero covers two honest cases: no term matched at all, and both lexicons
    matched at comparable strength. The second one matters --
    "Coinbase lists token days after exploit drained the treasury" nets to a
    mildly *bullish* +5 if you simply subtract, which is worse than admitting
    that a keyword classifier cannot read that headline. Only a clear winner
    scores.
    """
    lowered = title.lower()
    bullish = max((weight for term, weight in BULLISH_TERMS.items() if term in lowered), default=0.0)
    bearish = max((weight for term, weight in BEARISH_TERMS.items() if term in lowered), default=0.0)
    if bullish == 0.0 and bearish == 0.0:
        return 0.0
    if bullish > 0.0 and bearish > 0.0 and abs(bullish - bearish) < AMBIGUITY_MARGIN:
        return 0.0
    return float(max(-100.0, min(100.0, bullish - bearish)))


def parse_feed_list(raw: str) -> tuple[str, ...]:
    """Split the CS_NEWS_FEEDS override; empty falls back to the defaults."""
    urls = tuple(part.strip() for part in raw.split(",") if part.strip())
    return urls or DEFAULT_FEEDS


def decay_weight(age_hours: float, half_life_hours: float) -> float:
    """Exported for the tests and for anyone tuning the half-life."""
    if half_life_hours <= 0:
        raise ValueError("half_life_hours must be positive")
    return float(0.5 ** (max(0.0, age_hours) / half_life_hours))

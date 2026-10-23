"""News desk: crypto + macro headlines from public RSS feeds, and the Fear & Greed index.

Headlines are tagged (macro, regulation, ETF, hack...), linked to the coins
they mention and given a rough keyword sentiment. Claude reads them and makes
the real call; with web access on it can also search for more.
"""
from __future__ import annotations

import asyncio
import calendar
import hashlib
import logging
import re
import time

import httpx

try:
    import feedparser
except ImportError:  # pragma: no cover
    feedparser = None

log = logging.getLogger("tushar.news")

FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "The Block": "https://www.theblock.co/rss.xml",
    "Bitcoin Magazine": "https://bitcoinmagazine.com/feed",
    "CryptoSlate": "https://cryptoslate.com/feed/",
    "CNBC Economy": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
}
FNG_URL = "https://api.alternative.me/fng/?limit=30"

TAGS = {
    "macro": r"\b(fed|fomc|powell|rate cut|rate hike|interest rate|inflation|cpi|ppi|jobs report|payrolls?|"
             r"tariffs?|recession|treasur(y|ies)|yields?|dollar|gdp|ecb|boj)\b",
    "regulation": r"\b(sec|cftc|regulat\w*|lawsuit|sued|court|judge|ban(s|ned)?|bill|senate|congress|"
                  r"license|compliance|enforcement)\b",
    "etf": r"\b(etfs?|inflows?|outflows?)\b",
    "hack": r"\b(hack(ed|er|ers)?|exploit(ed)?|drain(ed)?|breach|stolen|attack(er)?|scam|rug ?pull)\b",
    "listing": r"\b(list(s|ing|ed)?|delist\w*|launchpool)\b",
    "liquidations": r"\bliquidat\w*\b",
    "institutional": r"\b(blackrock|fidelity|microstrategy|strategy inc|treasury compan\w*|institution\w*|"
                     r"acquir\w*|buys? (\$|bitcoin|btc|eth))\b",
    "tech": r"\b(upgrade|mainnet|hard fork|testnet|airdrop|staking|layer[- ]?2)\b",
    "whales": r"\b(whales?|large transfer|moved \$)\b",
}
COINS = {
    "BTCUSDT": r"\b(bitcoin|btc)\b", "ETHUSDT": r"\b(ethereum|ether|eth)\b", "SOLUSDT": r"\b(solana|sol)\b",
    "BNBUSDT": r"\b(bnb|binance coin)\b", "XRPUSDT": r"\b(xrp|ripple)\b", "DOGEUSDT": r"\b(dogecoin|doge)\b",
    "ADAUSDT": r"\b(cardano|ada)\b", "AVAXUSDT": r"\b(avalanche|avax)\b", "LINKUSDT": r"\b(chainlink|link)\b",
    "SUIUSDT": r"\bsui\b", "LTCUSDT": r"\b(litecoin|ltc)\b", "TONUSDT": r"\b(toncoin|ton)\b",
    "DOTUSDT": r"\b(polkadot|dot)\b", "TRXUSDT": r"\b(tron|trx)\b", "1000SHIBUSDT": r"\b(shiba inu|shib)\b",
    "1000PEPEUSDT": r"\bpepe\b", "APTUSDT": r"\baptos\b", "ARBUSDT": r"\barbitrum\b", "OPUSDT": r"\boptimism\b",
    "NEARUSDT": r"\bnear protocol\b", "INJUSDT": r"\binjective\b", "HYPEUSDT": r"\bhyperliquid\b",
    "ENAUSDT": r"\bethena\b", "WIFUSDT": r"\bdogwifhat\b", "UNIUSDT": r"\buniswap\b", "AAVEUSDT": r"\baave\b",
}
BULL = r"\b(surg\w*|soar\w*|rall(y|ies|ied)|record high|all-time high|ath|inflows?|approv\w*|bullish|gains?|" \
       r"jump\w*|ris(e|es|ing)|breakout|adopt\w*|buy(s|ing)?|upgrade|partnership|beats?)\b"
BEAR = r"\b(plung\w*|crash\w*|drop\w*|fall(s|ing)?|fell|outflows?|hack\w*|exploit\w*|lawsuit|ban\w*|bearish|" \
       r"sell-?off|liquidat\w*|slump\w*|fears?|reject\w*|delay\w*|tumbl\w*|sink\w*|loss(es)?|warns?)\b"

_TAG_RE = {k: re.compile(v, re.I) for k, v in TAGS.items()}
_COIN_RE = {k: re.compile(v, re.I) for k, v in COINS.items()}
_BULL_RE, _BEAR_RE = re.compile(BULL, re.I), re.compile(BEAR, re.I)
_HTML_RE = re.compile(r"<[^>]+>")


def analyze_headline(title: str, summary: str = "") -> dict:
    text = f"{title}. {summary}"
    tags = [k for k, rx in _TAG_RE.items() if rx.search(text)]
    symbols = [k for k, rx in _COIN_RE.items() if rx.search(title) or rx.search(summary[:300])]
    bull, bear = len(_BULL_RE.findall(text)), len(_BEAR_RE.findall(text))
    sentiment = 0.0 if bull + bear == 0 else round((bull - bear) / (bull + bear), 2)
    return {"tags": tags, "symbols": symbols, "sentiment": sentiment}


def parse_feed(source: str, text: str) -> list[dict]:
    if feedparser is None:
        return []
    feed = feedparser.parse(text)
    items = []
    for e in feed.entries[:40]:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        summary = _HTML_RE.sub(" ", e.get("summary") or e.get("description") or "")
        summary = re.sub(r"\s+", " ", summary).strip()[:400]
        ts = None
        for key in ("published_parsed", "updated_parsed"):
            if e.get(key):
                ts = calendar.timegm(e[key])
                break
        items.append({
            "id": hashlib.sha1(link.encode()).hexdigest()[:16], "ts": ts or time.time(), "source": source,
            "title": title, "link": link, "summary": summary, **analyze_headline(title, summary),
        })
    return items


class NewsDesk:
    def __init__(self, db, bus=None):
        self.db = db
        self.bus = bus
        self.fng: dict | None = db.get("fng")
        self.status: dict[str, str] = {}
        self.last_fetch = 0.0

    async def refresh(self) -> int:
        new = 0
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (tushar.ai news reader)"}) as client:
            async def fetch(name: str, url: str) -> list[dict]:
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    items = parse_feed(name, resp.text)
                    self.status[name] = f"ok ({len(items)})"
                    return items
                except Exception as e:  # noqa: BLE001
                    self.status[name] = f"error: {type(e).__name__}"
                    return []

            results = await asyncio.gather(*(fetch(n, u) for n, u in FEEDS.items()))
            fresh = []
            for items in results:
                for it in items:
                    if self.db.one("SELECT id FROM news WHERE id=?", (it["id"],)):
                        continue
                    self.db.insert("news", it)
                    fresh.append(it)
                    new += 1
            try:
                resp = await client.get(FNG_URL)
                data = resp.json().get("data", [])
                if data:
                    self.fng = {
                        "value": int(data[0]["value"]), "label": data[0]["value_classification"],
                        "history": [int(d["value"]) for d in reversed(data)],
                        "updated": time.time(),
                    }
                    self.db.set("fng", self.fng)
                    self.status["Fear & Greed"] = "ok"
            except Exception as e:  # noqa: BLE001
                self.status["Fear & Greed"] = f"error: {type(e).__name__}"
        self.db.execute("DELETE FROM news WHERE ts < ?", (time.time() - 4 * 86400,))
        self.last_fetch = time.time()
        if self.bus and fresh:
            fresh.sort(key=lambda x: x["ts"], reverse=True)
            self.bus.publish("news", {"items": fresh[:20], "fng": self.fng})
        return new

    def latest(self, hours: float = 24, symbol: str | None = None, limit: int = 40) -> list[dict]:
        rows = self.db.query("SELECT * FROM news WHERE ts >= ? ORDER BY ts DESC LIMIT 400",
                             (time.time() - hours * 3600,))
        if symbol:
            rows = [r for r in rows if symbol in (r.get("symbols") or [])]
        return rows[:limit]

    @property
    def healthy(self) -> bool:
        return any(v.startswith("ok") for v in self.status.values())

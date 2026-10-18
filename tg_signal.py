"""Best-effort parser for trading-signal style posts. LOG ONLY: never used to trade.

parse_signal(text) -> dict with only the fields that were found:
  tickers: [str], direction: "long"|"short", entry: [low] or [low, high], stop: float,
  targets: [float], leverage: float
Always returns {} when nothing is recognised. The raw text is kept by the caller; parsing is advisory
and can be wrong, so downstream consumers must read the raw text before relying on any field.
"""
import re

_NUM = r"\d+(?:,\d{3}(?!\d))*(?:\.\d+)?"
_STOP_TICKERS = {"TP", "SL", "USD", "USDT", "USDC", "PERP", "LONG", "SHORT", "BUY", "SELL", "ENTRY", "STOP",
                 "TARGET", "TARGETS", "RR", "DCA", "ATH", "ETA", "SPOT", "FUT", "THE", "AND", "FOR", "NEW", "ALL"}
_QUOTES = r"(?:USDT|USDC|USD|PERP|BUSD)"


def _f(s):
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _tickers(text):
    found = []

    def add(t):
        t = t.upper()
        if 2 <= len(t) <= 10 and t not in _STOP_TICKERS and t not in found and not t.isdigit():
            found.append(t)

    for m in re.finditer(r"[$#]([A-Za-z][A-Za-z0-9]{1,9})\b", text):
        t = m.group(1)
        t = re.sub(r"(?i)" + _QUOTES + r"$", "", t) or t
        add(t)
    for m in re.finditer(r"\b([A-Z][A-Z0-9]{1,9})\s*/\s*" + _QUOTES + r"\b", text):
        add(m.group(1))
    for m in re.finditer(r"\b([A-Z]{2,9})" + _QUOTES + r"\b", text):
        add(m.group(1))
    return found[:8]


def _direction(text):
    m = re.search(r"(?i)(?<![\w])(long|short|buy|sell)(?![\w])", text)
    if not m:
        return None
    return "long" if m.group(1).lower() in ("long", "buy") else "short"


def _entry(text):
    m = re.search(r"(?i)\b(?:entry(?:\s*(?:price|zone|range))?|entries|enter(?:\s*(?:at|around))?|buy\s*(?:zone|at|around)|ep)\b"
                  r"\s*[:=@\-–]?\s*(" + _NUM + r")(?:\s*(?:-|–|—|to)\s*(" + _NUM + r"))?", text)
    if not m:
        return None
    vals = [_f(m.group(1))]
    if m.group(2):
        vals.append(_f(m.group(2)))
    vals = [v for v in vals if v is not None]
    return sorted(vals) if vals else None


def _stop(text):
    m = re.search(r"(?i)(?<![\w])(?:stop[\s-]*loss|stoploss|stop|sl)(?![\w])\s*[:=@\-–]?\s*(" + _NUM + r")", text)
    return _f(m.group(1)) if m else None


_TP_KEY = re.compile(r"(?i)(?<![\w])(?:take[\s-]*profits?|targets?|tps?)(?![a-z])(?:\d{1,2}(?![\d.,]))?\s*[:=@\-–]?")
_SEQ = re.compile(r"^\s*((?:" + _NUM + r")(?:(?:\s*(?:[,/;|\-–]|and)\s*|[ \t]+)(?:" + _NUM + r"))*)")


def _targets(text):
    t = re.sub(r"(?<![\d.,])\d{1,2}\)\s*", " ", text)  # drop "1) 2)" list markers
    keys = list(_TP_KEY.finditer(t))
    out = []
    for i, k in enumerate(keys):
        end = keys[i + 1].start() if i + 1 < len(keys) else len(t)
        seg = t[k.end():end]
        m = _SEQ.match(seg)
        if m:
            for n in re.findall(_NUM, m.group(1)):
                v = _f(n)
                if v is not None and v not in out:
                    out.append(v)
    return out[:10]


def _leverage(text):
    m = re.search(r"(?i)(?:lev(?:erage)?\s*[:=]?\s*(\d{1,3})\s*x?|(?<![\w.])(\d{1,3})\s*x\b)", text)
    if not m:
        return None
    return float(m.group(1) or m.group(2))


def parse_signal(text):
    if not text or not isinstance(text, str):
        return {}
    text = text[:6000]
    text = re.sub(r"(?<![\w.])(\d+(?:\.\d+)?)[kK](?![\w])", lambda m: "%d" % round(float(m.group(1)) * 1000), text)  # 65k -> 65000
    out = {}
    try:
        tk = _tickers(text)
        if tk:
            out["tickers"] = tk
        d = _direction(text)
        if d:
            out["direction"] = d
        e = _entry(text)
        if e:
            out["entry"] = e
        s = _stop(text)
        if s is not None:
            out["stop"] = s
        tg = _targets(text)
        if tg:
            out["targets"] = tg
        lv = _leverage(text)
        if lv:
            out["leverage"] = lv
    except Exception:  # parsing must never raise
        return {}
    # Only call it a "signal" if there is at least a price level or (ticker and direction)
    if not (("entry" in out) or ("stop" in out) or ("targets" in out) or ("tickers" in out and "direction" in out)):
        return {}
    return out

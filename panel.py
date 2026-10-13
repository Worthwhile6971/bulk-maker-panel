#!/usr/bin/env python3
"""Bulk maker panel. Local web UI at http://127.0.0.1:8787

Opens: post-only ALO limit (NOT reduce-only) at the touch, re-priced until filled.
Closes: post-only ALO limit, reduce-only, at the touch, re-priced until filled.
There is no market/IOC entry or close path. Optional SL/TP are exchange-side
conditional orders (reduce-only; they execute as TAKER when triggered).
Python 3.9+ compatible.
"""
import argparse, json, os, sys, threading, time, traceback, uuid, math, secrets, socket, hmac
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))

def load_env(path):
    if not os.path.exists(path): return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1)
        if k.startswith("export "): k = k[7:]
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_env(os.path.join(HERE, "agent.env"))
sys.path.insert(0, HERE)
import close as C  # noqa: E402

# make shared rate limiters thread-safe (5 req/s API limit; we run 4/s)
_rl_lock = threading.Lock()
def _wrap(orig):
    def w():
        with _rl_lock: orig()
    return w
for _rl in (C.REQ, C.ACT): _rl.wait = _wrap(_rl.wait)

ACCOUNT = os.environ.get("BULK_ACCOUNT")
DRY = False; DRY_ITERS = 5
JOBS = {}; JOBS_LOCK = threading.Lock()
MKT = {}; MKT_T = [0.0]
EQ_FILE = os.path.join(HERE, "equity_history.json")
CACHE = {}; CACHE_LOCK = threading.Lock()
MAJORS = ["BTC-USD", "ETH-USD", "SOL-USD"]

def cached(key, ttl, fn):
    now = time.time()
    with CACHE_LOCK:
        hit = CACHE.get(key)
        if hit and now - hit[0] < ttl: return hit[1]
    val = fn()
    with CACHE_LOCK: CACHE[key] = (now, val)
    return val

def markets():
    if not MKT or time.time() - MKT_T[0] > 600:
        for m in C.get("/exchangeInfo"): MKT[m["symbol"]] = m
        MKT_T[0] = time.time()
    return MKT

def market(sym):
    m = markets()
    if sym not in m: raise ValueError("unknown symbol " + sym)
    return m[sym]

def fnum(x, d=0.0):
    try: return float(x)
    except (TypeError, ValueError): return d

def account():
    return cached("fa", 2.0, lambda: C.full_account(ACCOUNT))

def acct_leverage(fa, sym):
    for s in fa.get("leverageSettings", []) or []:
        if s.get("symbol") == sym: return fnum(s.get("leverage"))
    return None

def is_protective(o):
    return str(o.get("orderType", "")).lower() in ("stop", "range", "trailing", "take_profit", "takeprofit", "trigger")

def is_stop(o):
    t = str(o.get("orderType", "")).lower()
    return t in ("stop", "range", "trailing")

def positions(fa=None):
    fa = fa or account()
    orders = fa.get("openOrders", []) or []
    out = []
    for p in fa.get("positions", []):
        sz = fnum(p.get("size"))
        if sz == 0: continue
        sym = p["symbol"]
        stop = C.has_stop(p) or any(o.get("symbol") == sym and is_stop(o) for o in orders)
        out.append({"symbol": sym, "side": "LONG" if sz > 0 else "SHORT", "size": abs(sz),
                    "entry": fnum(p.get("price")), "mark": fnum(p.get("fairPrice")),
                    "notional": abs(sz) * fnum(p.get("fairPrice")),
                    "upnl": fnum(p.get("unrealizedPnl")), "leverage": p.get("leverage"),
                    "liq": p.get("liquidationPrice"), "iso": bool(p.get("iso")), "has_stop": stop})
    return out

def open_orders(fa=None):
    fa = fa or account()
    out = []
    for o in fa.get("openOrders", []) or []:
        trig = o.get("trigger") or {}
        out.append({"id": o.get("orderId"), "symbol": o.get("symbol"), "side": o.get("side"),
                    "type": o.get("orderType"), "tif": o.get("tif"), "price": fnum(o.get("price")),
                    "size": fnum(o.get("originalSize", o.get("size"))),
                    "filled": fnum(o.get("executedSize", o.get("filledSize"))),
                    "reduceOnly": o.get("reduceOnly"), "trigger": trig.get("price", trig.get("triggerPrice")),
                    "conditional": is_protective(o)})
    return out

def history(kind, limit=1000):
    def f():
        r = C.post("/account", {"type": kind, "user": ACCOUNT, "limit": limit})
        return r.get("data", []) if isinstance(r, dict) else []
    return cached("h_" + kind, 15.0, f)

def midnight_ns():
    lt = time.localtime()
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) * 1e9)

def fills_view():
    """Fills newest-first, with realised (gross) PnL + net-of-fee per closing fill."""
    mid = midnight_ns()
    agg = {"maker_vol": 0.0, "taker_vol": 0.0, "fees_today": 0.0, "maker_vol_today": 0.0, "taker_vol_today": 0.0}
    raw = sorted(history("fills", 1000), key=lambda f: (int(f.get("timestamp", 0)), int(f.get("sequence", 0))))
    # Bulk fills carry no PnL field. Attribute each fill to a position from Bulk's closed-position
    # history (openTime..closeTime, avgOpenPrice) or to the currently open position.
    wins = {}
    for p in history("positions", 1000):
        wins.setdefault(p.get("symbol"), []).append([int(p.get("openTime", 0)), int(p.get("closeTime", 0)),
                                                      fnum(p.get("avgOpenPrice")), None, p])
    try: cur = {p["symbol"]: p for p in positions()}
    except Exception: cur = {}
    rows = []; parsed = []
    for f in raw:
        if f.get("maker") == ACCOUNT: maker = True
        elif f.get("taker") == ACCOUNT: maker = False
        else: maker = fnum(f.get("fee")) == 0   # e.g. isolated-account rows
        fee = fnum(f.get("makerFee") if maker else f.get("takerFee"))   # negative = paid
        # isBuy is this account's side (checked against Bulk position history for maker and taker fills)
        is_buy = bool(f.get("isBuy"))
        parsed.append((f, maker, fee, is_buy))
    # direction of each window = side of its first fill
    for sym, ws in wins.items():
        for w in ws:
            p = w[4]; dpx = fnum(p.get("avgClosePrice")) - fnum(p.get("avgOpenPrice")); rp = fnum(p.get("realizedPnl"))
            if abs(dpx) > 1e-9 and abs(rp) > 1e-9: w[3] = 1.0 if dpx * rp > 0 else -1.0   # authoritative
            else:
                for f, _, _, ib in parsed:
                    if f.get("symbol") == sym and w[0] <= int(f.get("timestamp", 0)) <= w[1]: w[3] = 1.0 if ib else -1.0; break
            if w[3] is None: w[3] = 1.0
    for f, maker, fee, is_buy in parsed:
        px = fnum(f.get("price")); qty = fnum(f.get("amount")); notional = qty * px
        oid = f.get("orderIdMaker") if maker else f.get("orderIdTaker")
        ts = int(f.get("timestamp", 0)); role = "maker" if maker else "taker"; sym = f.get("symbol")
        agg["maker_vol" if maker else "taker_vol"] += notional
        if ts >= mid:
            agg["fees_today"] += -fee
            agg["maker_vol_today" if maker else "taker_vol_today"] += notional
        dirn = entry = None
        for w in wins.get(sym, []):
            if w[0] <= ts <= w[1]: dirn, entry = w[3], w[2]; break
        if dirn is None and sym in cur and ts > max([w[1] for w in wins.get(sym, [])] or [0]):
            dirn = 1.0 if cur[sym]["side"] == "LONG" else -1.0; entry = fnum(cur[sym]["entry"])
        pnl = None
        if dirn is not None and entry and (dirn > 0) != is_buy:
            pnl = (px - entry) * qty * dirn
        rows.append({"t": ts // 1000000, "ts": ts, "symbol": sym, "side": "BUY" if is_buy else "SELL",
                     "price": px, "size": qty, "notional": notional, "fee": -fee, "role": role, "oid": oid,
                     "close": pnl is not None, "closedSize": qty if pnl is not None else 0.0,
                     "closedSide": None if pnl is None else ("LONG" if dirn > 0 else "SHORT"), "entry": entry if pnl is not None else None,
                     "pnl": None if pnl is None else round(pnl, 6),
                     "net": None if pnl is None else round(pnl + fee, 6)})
    rows.reverse()
    return rows, agg

def _group_close(fills, lev_of):
    g = {}
    for f in fills:
        if not f["close"]: continue
        k = (f["symbol"], f["oid"])
        x = g.setdefault(k, {"symbol": f["symbol"], "oid": f["oid"], "side": f["closedSide"], "size": 0.0,
                             "_xn": 0.0, "_en": 0.0, "gross": 0.0, "fees": 0.0, "roles": set(), "t": f["t"], "t0": f["t"]})
        x["size"] += f["closedSize"]; x["_xn"] += f["closedSize"] * f["price"]; x["_en"] += f["closedSize"] * f["entry"]
        x["gross"] += f["pnl"]; x["fees"] += f["fee"]; x["roles"].add(f["role"])
        x["t"] = max(x["t"], f["t"]); x["t0"] = min(x["t0"], f["t"])
    out = []
    for x in g.values():
        sz = x["size"] or 1e-12
        x["exit"] = x.pop("_xn") / sz; x["entry"] = x.pop("_en") / sz
        x["net"] = x["gross"] - x["fees"]
        lev = lev_of(x["symbol"]) or 1.0; margin = x["entry"] * sz / lev
        x["leverage"] = lev; x["roe"] = x["net"] / margin * 100 if margin else 0.0
        r = x.pop("roles"); x["role"] = r.pop() if len(r) == 1 else "mixed"
        out.append(x)
    return out

def closed_trades(fills=None):
    """One row per closing order (all its fills aggregated)."""
    if fills is None: fills, _ = fills_view()
    try: fa = account()
    except Exception: fa = {}
    lv = {}
    def lev_of(sym):
        if sym not in lv:
            try: lv[sym] = fnum(acct_leverage(fa, sym)) or fnum(market(sym).get("maxLeverage"), 1)
            except Exception: lv[sym] = 1.0
        return lv[sym]
    rows = sorted(_group_close(fills, lev_of), key=lambda x: -x["t"])
    mid = midnight_ns() // 1000000
    td = [r for r in rows if r["t"] >= mid]
    today = {"net": sum(r["net"] for r in td), "gross": sum(r["gross"] for r in td), "fees": sum(r["fees"] for r in td),
             "wins": sum(1 for r in td if r["net"] > 0), "losses": sum(1 for r in td if r["net"] <= 0), "count": len(td)}
    return rows, today

def job_result(j, fills=None):
    if j.kind != "close" or not j.oids: return None
    if fills is None: fills, _ = fills_view()
    mine = [f for f in fills if f["close"] and f["oid"] in j.oids and f["symbol"] == j.sym]
    if not mine: return None
    g = _group_close(mine, lambda s: 1.0)
    sz = sum(x["size"] for x in g); xn = sum(x["exit"] * x["size"] for x in g)
    gross = sum(x["gross"] for x in g); fees = sum(x["fees"] for x in g)
    return {"size": sz, "exit": xn / sz if sz else 0.0, "gross": gross, "fees": fees, "net": gross - fees,
            "text": "Closed %g %s @ avg %s \u2192 %s$%.2f (net)" % (round(sz, 8), j.sym.split("-")[0],
                    ("%.2f" % (xn / sz)) if sz else "-", "+" if gross - fees >= 0 else "-", abs(gross - fees))}

def pnl_view():
    rows = sorted(history("positions", 1000), key=lambda r: int(r.get("closeTime", 0)))
    cum = 0.0; series = []; today = 0.0; mid = midnight_ns()
    for r in rows:
        pnl = fnum(r.get("realizedPnl")) + fnum(r.get("funding"))
        cum += pnl
        ct = int(r.get("closeTime", 0))
        series.append({"t": ct // 1000000000, "v": round(cum, 4), "pnl": round(pnl, 4), "symbol": r.get("symbol")})
        if ct >= mid: today += pnl
    return series, today

# ---------------- equity history ----------------
EQ = []; EQ_LOCK = threading.Lock()
def eq_load():
    global EQ
    try: EQ = json.load(open(EQ_FILE))[-5000:]
    except Exception: EQ = []
def eq_record(v):
    with EQ_LOCK:
        if EQ and time.time() - EQ[-1][0] < 60: return
        EQ.append([int(time.time()), round(v, 4)])
        try:
            tmp = EQ_FILE + ".tmp"; json.dump(EQ[-5000:], open(tmp, "w")); os.replace(tmp, EQ_FILE)
        except Exception: pass

def summary():
    fa = account(); m = fa.get("margin", {}) or {}
    total = fnum(m.get("totalMargin")); upnl = fnum(m.get("unrealizedPnl"))
    equity = total  # Bulk totalMargin is collateral marked-to-market (matches app "Equity")
    eq_record(equity)
    try:
        _fl, agg = fills_view(); _, ctoday = closed_trades(_fl)
    except Exception: agg = {}; ctoday = None
    try: _, today = pnl_view()
    except Exception: today = None
    pos = positions(fa)
    return {"equity": equity, "available": fnum(m.get("availableMargin")), "marginUsed": fnum(m.get("marginUsed")),
            "upnl": upnl, "notional": fnum(m.get("notional")), "realizedTotal": fnum(m.get("realizedPnl")),
            "feesTotal": -fnum(m.get("fees")), "realizedToday": today, "closedToday": ctoday, "agg": agg,
            "positions": pos, "unprotected": [p["symbol"] for p in pos if not p["has_stop"]],
            "orders": open_orders(fa), "dry": DRY, "account": ACCOUNT[:4] + ".." + ACCOUNT[-4:],
            "equityHistory": EQ[-1500:]}

def symbols():
    ms = markets(); act = sorted(s for s, m in ms.items() if m.get("status") not in ("SUSPENDED", "DELISTED"))
    rest = [s for s in act if s not in MAJORS]
    fa = account()
    return [{"symbol": s, "maxLeverage": ms[s].get("maxLeverage"), "accountLeverage": acct_leverage(fa, s),
             "tick": ms[s].get("tickSize"), "status": ms[s].get("status")} for s in MAJORS + rest if s in ms]

def preview(sym, side, pct, lev):
    mi = market(sym); fa = account(); m = fa.get("margin", {}) or {}
    acct_lev = acct_leverage(fa, sym) or fnum(mi.get("maxLeverage"), 1)
    lev = min(fnum(lev, acct_lev) or acct_lev, acct_lev, fnum(mi.get("maxLeverage"), acct_lev))
    pct = fnum(pct)
    if not (0 < pct <= 100): raise ValueError("percent must be 1-100")
    avail = fnum(m.get("availableMargin"))
    buf = max(fnum(m.get("marginBufferRate"), 0.05), 0.03) + 0.02   # margin buffer + fees/slippage headroom
    bid, ask = C.touch(sym)
    if not bid: raise ValueError("no book for " + sym)
    px = bid if side == "long" else ask
    notional = avail * (1 - buf) * lev * pct / 100.0
    lot = fnum(mi.get("sizeIncrement") or mi.get("lotSize"), 1e-8)
    qty = C.floor_to(notional / px, lot); qty = round(qty, 8)
    mn = fnum(mi.get("minNotional"))
    warn = []
    if qty * px < mn: warn.append("below min notional $%g" % mn)
    if lev < acct_lev: warn.append("Bulk account leverage for %s is %gx; lower selector only shrinks size" % (sym, acct_lev))
    return {"symbol": sym, "side": side, "pct": pct, "leverage": lev, "accountLeverage": acct_lev,
            "maxLeverage": mi.get("maxLeverage"), "available": avail, "buffer": buf, "price": px,
            "bid": bid, "ask": ask, "qty": qty, "notional": qty * px, "margin": qty * px / lev,
            "minNotional": mn, "ok": qty > 0 and qty * px >= mn, "warn": warn}

# ---------------- jobs ----------------
class Job:
    def __init__(self, kind, sym, pct=None, side=None, qty=None, lev=None, sl=None, tp=None, reprice=2.0):
        self.id = uuid.uuid4().hex[:8]; self.kind = kind; self.sym = sym; self.pct = pct
        self.side = side; self.qty = qty; self.lev = lev; self.sl = sl; self.tp = tp; self.reprice = reprice
        self.state = "starting"; self.log = []; self.resting_px = None; self.filled = 0.0
        self.target = 0.0; self.cancel = threading.Event(); self.started = time.time()
        self.protection = None; self.oids = set()
        self.trader = C.Trader(not DRY, ACCOUNT)
    def L(self, *a):
        self.log.append(time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)); self.log = self.log[-200:]
    def view(self):
        d = {k: getattr(self, k) for k in ("id", "kind", "sym", "pct", "side", "state", "resting_px",
                                           "filled", "target", "sl", "tp", "protection", "started")}
        d["log"] = self.log[-40:]; d["dry"] = DRY
        return d

    def run(self):
        try:
            if self.kind == "open": self.run_open()
            else: self.run_close()
        except Exception as e:
            self.state = "error"; self.L("ERROR", repr(e)[:300]); traceback.print_exc()

    def _spec(self):
        mi = market(self.sym)
        if "ALO" not in mi.get("timeInForces", []): raise RuntimeError("symbol has no ALO")
        return (float(mi["tickSize"]), float(mi.get("sizeIncrement") or mi.get("lotSize")),
                float(mi.get("minNotional", 0)))

    def _quote_loop(self, is_buy, reduce_only, iso, remaining_fn):
        """Shared maker loop: rest ALO at touch on own side, reprice when touch moves,
        re-read fills each cycle. remaining_fn() -> remaining qty (<=0 => done) or None (abort)."""
        tick, lot, mn = self._spec()
        oid = None; my_px = my_sz = None; last = 0.0; cycles = 0
        try:
            while not self.cancel.is_set():
                rem = remaining_fn()
                if rem is None: break
                rem = C.floor_to(rem, lot); rem = round(rem, 8)
                if rem <= 0: self.L("target filled (%g)" % self.filled); break
                bid, ask = C.touch(self.sym)
                if bid is None: time.sleep(self.reprice); continue
                want = C.rnd(C.floor_to(bid, tick) if is_buy else C.ceil_to(ask, tick), tick)
                if rem * want < mn:
                    self.L("remaining %g below minNotional $%g; stopping" % (rem, mn)); break
                if oid is None or ((want != my_px or rem != my_sz) and time.time() - last >= self.reprice):
                    if oid is not None:
                        self.L("cancel %s -> %s" % (oid[:8], C.status_kinds(self.trader.cancel(self.sym, oid)) or "dry"))
                        oid = None; self.resting_px = None
                        if not DRY: continue  # re-read fills before re-quoting
                    if reduce_only:
                        r = self.trader.place(self.sym, is_buy, want, rem, iso)       # asserts ALO + reduce_only=True
                    else:
                        r = self.trader.place_open(self.sym, is_buy, want, rem, iso)  # asserts ALO + reduce_only=False
                    k = C.status_kinds(r); last = time.time()
                    self.L("%s %g @ %s ALO%s | bid %s ask %s -> %s" % ("BUY" if is_buy else "SELL", rem, want,
                           " RO" if reduce_only else "", bid, ask, k or "dry"))
                    if DRY: oid, my_px, my_sz = "DRYRUN00", want, rem; self.resting_px = want
                    elif any(x in ("resting", "working") for x in k):
                        oid, my_px, my_sz = r["oid"], want, rem; self.resting_px = want
                    if not DRY and isinstance(r, dict) and r.get("oid"): self.oids.add(r["oid"])
                    elif "rejectedCrossing" in k: self.L("rejected crossing (touch moved); re-quote")
                    elif any(x in ("filled", "partiallyFilled") for x in k): pass
                    else:
                        self.L("unexpected:", str(r.get("statuses") or r.get("raw"))[:200]); time.sleep(self.reprice)
                cycles += 1
                if DRY and DRY_ITERS and cycles >= DRY_ITERS: self.L("dry-run iterations done"); break
                time.sleep(max(0.5, self.reprice / 2))
        finally:
            if oid and not DRY:
                try: self.L("cancel resting", oid[:8], "->", C.status_kinds(self.trader.cancel(self.sym, oid)))
                except Exception as e: self.L("cancel failed!", repr(e)[:200])
            self.resting_px = None

    def run_close(self):
        _, lot, mn = self._spec()
        pos, row = C.position(ACCOUNT, self.sym)
        if pos == 0: self.state = "done"; self.L("no position"); return
        is_long = pos > 0; iso = bool(row.get("iso", False)); start = abs(pos)
        self.target = start if self.pct >= 100 else C.floor_to(start * self.pct / 100.0, lot)
        bid, ask = C.touch(self.sym)
        if self.target <= 0 or self.target * (bid or 0) < mn:
            self.state = "skipped"; self.L("size %g below lot/minNotional %g; skipped" % (self.target, mn)); return
        self.L("%sclosing %g of %+g (%g%%) via reduce-only ALO" % ("DRY-RUN " if DRY else "", self.target, pos, self.pct))
        self.state = "working"
        def rem():
            cur = pos if DRY else C.position(ACCOUNT, self.sym)[0]
            if cur == 0 or (cur > 0) != is_long: self.L("flat -> done"); return None
            self.filled = start - abs(cur)
            return min(abs(cur), self.target - self.filled)
        self._quote_loop(not is_long, True, iso, rem)
        self.state = "cancelled" if self.cancel.is_set() else "done"

    def run_open(self):
        _, lot, mn = self._spec()
        is_long = self.side == "long"; d = 1 if is_long else -1
        start, row = C.position(ACCOUNT, self.sym)
        if start != 0 and (start > 0) != is_long:
            self.state = "skipped"; self.L("existing %+g opposite position; close it first (no flipping)" % start); return
        iso = bool(row.get("iso", False)) if row else False
        self.target = self.qty
        bid, ask = C.touch(self.sym)
        if self.sl is not None:
            if (is_long and self.sl >= bid) or (not is_long and self.sl <= ask):
                self.state = "skipped"; self.L("stop %s is on the wrong side of the market; not opening" % self.sl); return
        if self.tp is not None:
            if (is_long and self.tp <= ask) or (not is_long and self.tp >= bid):
                self.state = "skipped"; self.L("take-profit %s is on the wrong side of the market; not opening" % self.tp); return
        self.L("%sopening %s %g %s (~$%.0f, %gx) via ALO post-only; SL %s TP %s" % (
            "DRY-RUN " if DRY else "", self.side.upper(), self.qty, self.sym, self.qty * (bid if is_long else ask),
            self.lev or 0, self.sl or "-", self.tp or "-"))
        self.state = "working"
        def rem():
            cur = start if DRY else C.position(ACCOUNT, self.sym)[0]
            self.filled = max(0.0, (cur - start) * d)
            return self.target - self.filled
        self._quote_loop(is_long, False, iso, rem)
        # protection for whatever filled (DRY: show what would be sent for the full target)
        filled = self.target if DRY else self.filled
        if not DRY:
            cur = C.position(ACCOUNT, self.sym)[0]; filled = abs(cur) if (cur > 0) == is_long else 0.0
        if filled > 0 and (self.sl is not None or self.tp is not None):
            kind = "range/OCO" if (self.sl and self.tp) else ("stop" if self.sl else "take-profit")
            self.L("placing exchange-side %s for %g (TAKER when triggered)" % (kind, filled))
            try:
                r = self.trader.place_protection(self.sym, is_long, round(filled, 8), self.sl, self.tp, iso)
                k = C.status_kinds(r)
                self.protection = "%s %s" % (kind, k or "dry")
                self.L("protection ->", k or "dry", "" if DRY else str(r.get("raw"))[:160])
                if not DRY and r.get("raw_status") != "ok":
                    self.L("!! PROTECTION MAY HAVE FAILED - add a stop in the Bulk app now")
            except Exception as e:
                self.protection = "FAILED"; self.L("!! protection FAILED:", repr(e)[:200], "- add a stop in the Bulk app now")
        elif filled > 0:
            self.L("no SL set - position is UNPROTECTED")
        self.state = "cancelled" if self.cancel.is_set() else "done"

def _launch(j):
    with JOBS_LOCK:
        for x in JOBS.values():
            if x.sym == j.sym and x.state in ("starting", "working"):
                raise ValueError("a job is already running on " + j.sym)
        JOBS[j.id] = j
    threading.Thread(target=j.run, daemon=True).start(); return j

def start_close(sym, pct):
    pct = float(pct)
    if not (0 < pct <= 100): raise ValueError("percent must be 1-100")
    return _launch(Job("close", sym, pct=pct))

def opt_px(v):
    if v in (None, "", 0, "0"): return None
    v = float(v)
    if v <= 0: raise ValueError("price must be > 0")
    return v

def start_open(body):
    sym = str(body["symbol"]); side = str(body["side"]).lower()
    if side not in ("long", "short"): raise ValueError("side must be long or short")
    pv = preview(sym, side, body["percent"], body.get("leverage"))
    if not pv["ok"]: raise ValueError("size too small: " + "; ".join(pv["warn"]))
    sl = opt_px(body.get("sl")); tp = opt_px(body.get("tp"))
    return _launch(Job("open", sym, pct=pv["pct"], side=side, qty=pv["qty"], lev=pv["leverage"], sl=sl, tp=tp))

def cancel_order(sym, oid):
    t = C.Trader(not DRY, ACCOUNT)
    return C.status_kinds(t.cancel(sym, oid)) or ["dry"]

def candles(sym, interval):
    secs = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}.get(interval)
    if not secs: raise ValueError("interval")
    now = int(time.time() * 1000)
    def f():
        r = C.get("/klines", symbol=sym, interval=interval, startTime=now - secs * 1000 * 300, endTime=now)
        return [{"time": int(k["t"] // 1000), "open": k["o"], "high": k["h"], "low": k["l"], "close": k["c"]} for k in r]
    return cached("k_%s_%s" % (sym, interval), 5.0, f)

# ---------------- HTTP ----------------
def html(name="ui.html"):
    return open(os.path.join(HERE, name), encoding="utf-8").read()

# ---- LAN / phone mode (token-protected) ----
PUBLIC = False; FAILS = {}; FAIL_LOCK = threading.Lock()
LAN = False; TOKEN = None; ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
TOKEN_FILE = os.path.join(HERE, "phone_token"); COOKIE = "bmc_t"

def load_token():
    if os.environ.get("PANEL_TOKEN"): return os.environ["PANEL_TOKEN"].strip()
    if os.path.exists(TOKEN_FILE):
        t = open(TOKEN_FILE).read().strip()
        if len(t) >= 20: os.chmod(TOKEN_FILE, 0o600); return t
    t = secrets.token_urlsafe(24)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f: f.write(t + "\n")
    os.chmod(TOKEN_FILE, 0o600)
    return t

def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]; s.close(); return ip
    except Exception: return "127.0.0.1"

def local_ips():
    ips = {lan_ip()}
    try:
        for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET): ips.add(i[4][0])
    except Exception: pass
    return ips

LOGIN_HTML = ("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
              "<meta name=apple-mobile-web-app-capable content=yes><title>Bulk Maker</title>"
              "<body style='background:#0b0f14;color:#e5e7eb;font:17px -apple-system,sans-serif;padding:24px'>"
              "<h2>Access token required</h2><p>Scan the QR from the launcher, or paste the token "
              "(from the <code>phone_token</code> file).</p><form action='/m'><input name=t autocomplete=off "
              "style='width:100%;font-size:18px;padding:14px;border-radius:12px;border:1px solid #334;background:#111827;color:#fff'>"
              "<button style='margin-top:12px;width:100%;padding:16px;font-size:18px;border-radius:12px;border:0;background:#22c55e'>"
              "Unlock</button></form></body>")
ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="96" fill="#0b0f14"/>'
            '<path d="M120 330 L210 230 L280 290 L392 170" stroke="#22c55e" stroke-width="40" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
            '<text x="256" y="440" font-family="Helvetica,Arial" font-size="92" font-weight="700" fill="#e5e7eb" text-anchor="middle">MAKER</text></svg>')
MANIFEST = {"name": "Bulk Maker Panel", "short_name": "Bulk Maker", "start_url": "/m", "scope": "/",
            "display": "standalone", "background_color": "#0b0f14", "theme_color": "#0b0f14",
            "icons": [{"src": "/icon.png", "sizes": "180x180", "type": "image/png"},
                      {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, obj, ctype="application/json"):
        b = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _ip(self):
        return (self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()
    def _blocked(self):
        now = time.time(); ip = self._ip()
        with FAIL_LOCK:
            l = [t for t in FAILS.get(ip, []) if now - t < 900]; FAILS[ip] = l
            return len(l) >= 10
    def _fail(self):
        with FAIL_LOCK: FAILS.setdefault(self._ip(), []).append(time.time())
    def _host_ok(self):
        if PUBLIC: return True
        return (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]") in ALLOWED_HOSTS
    def _authed(self, q):
        """Host check always; token check in LAN mode. Returns True or sends an error."""
        if self._blocked(): self.send(429, {"error": "too many failed attempts, wait 15 min"}); return False
        if not self._host_ok():
            self.send(403, {"error": "host not allowed"}); return False   # DNS-rebinding guard
        if not LAN: return True
        ck = ""
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE: ck = v
        if ck and hmac.compare_digest(ck.encode(), TOKEN.encode()): return True
        self._fail()
        if self.command == "GET" and not urlparse(self.path).path.startswith("/api/"):
            return self.send(401, LOGIN_HTML, "text/html; charset=utf-8") and False
        self.send(401, {"error": "unauthorized: open the URL with ?t=<token> from the launcher"}); return False
    def do_GET(self):
        u = urlparse(self.path); p = u.path; q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if LAN and "t" in q:
            if self._blocked(): return self.send(429, {"error": "too many failed attempts, wait 15 min"})
            if self._host_ok() and hmac.compare_digest(q["t"].encode(), TOKEN.encode()):
                sec = "; Secure" if (PUBLIC or self.headers.get("X-Forwarded-Proto") == "https") else ""
                self.send_response(302)
                self.send_header("Set-Cookie", "%s=%s; HttpOnly; SameSite=Lax; Path=/; Max-Age=31536000%s" % (COOKIE, TOKEN, sec))
                self.send_header("Location", p or "/m"); self.send_header("Content-Length", "0"); self.end_headers(); return
        if not self._authed(q): return
        try:
            if p == "/": return self.send(200, html(), "text/html; charset=utf-8")
            if p in ("/m", "/m/"): return self.send(200, html("m.html"), "text/html; charset=utf-8")
            if p == "/manifest.webmanifest": return self.send(200, json.dumps(MANIFEST), "application/manifest+json")
            if p == "/icon.svg": return self.send(200, ICON_SVG, "image/svg+xml")
            if p in ("/icon.png", "/apple-touch-icon.png"):
                b = open(os.path.join(HERE, "icon.png"), "rb").read()
                self.send_response(200); self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
            if p == "/api/summary": return self.send(200, summary())
            if p == "/api/positions": return self.send(200, {"positions": positions(), "dry": DRY})
            if p == "/api/symbols": return self.send(200, {"symbols": symbols()})
            if p == "/api/book":
                b, a = C.touch(q["symbol"]); return self.send(200, {"bid": b, "ask": a})
            if p == "/api/preview":
                return self.send(200, preview(q["symbol"], q.get("side", "long"), q.get("pct", "10"), q.get("lev")))
            if p == "/api/candles": return self.send(200, {"candles": candles(q["symbol"], q.get("interval", "15m"))})
            if p == "/api/fills":
                rows, agg = fills_view(); return self.send(200, {"fills": rows[:100], "agg": agg})
            if p == "/api/closed":
                rows, today = closed_trades(); return self.send(200, {"trades": rows[:100], "today": today})
            if p == "/api/pnl":
                s, today = pnl_view(); return self.send(200, {"series": s, "today": today})
            if p == "/api/jobs":
                js = sorted(JOBS.values(), key=lambda j: -j.started)[:20]; out = []
                fl = None
                if any(j.kind == "close" and j.oids for j in js):
                    try: fl, _ = fills_view()
                    except Exception: fl = None
                for j in js:
                    v = j.view()
                    try: v["result"] = job_result(j, fl) if fl is not None else None
                    except Exception: v["result"] = None
                    out.append(v)
                return self.send(200, {"jobs": out})
            self.send(404, {"error": "not found"})
        except (ValueError, KeyError) as e: self.send(400, {"error": str(e)})
        except Exception as e: self.send(500, {"error": repr(e)[:300]})
    def do_POST(self):
        p = urlparse(self.path).path
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send(415, {"error": "json only"})  # blocks simple cross-site form posts
        if not self._authed({}): return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 2) or "{}")
            if p == "/api/close": return self.send(200, start_close(str(body["symbol"]), body["percent"]).view())
            if p == "/api/open": return self.send(200, start_open(body).view())
            if p == "/api/cancel":
                j = JOBS.get(body.get("id"))
                if not j: return self.send(404, {"error": "no such job"})
                j.cancel.set(); j.L("cancel requested"); return self.send(200, {"ok": True})
            if p == "/api/cancel_order":
                return self.send(200, {"result": cancel_order(str(body["symbol"]), str(body["id"]))})
            self.send(404, {"error": "not found"})
        except (ValueError, KeyError) as e: self.send(400, {"error": str(e)})
        except Exception as e: self.send(500, {"error": repr(e)[:300]})

def main():
    global DRY, DRY_ITERS
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    ap.add_argument("--dry-run", action="store_true", help="never send orders; log what would be sent")
    ap.add_argument("--dry-iterations", type=int, default=5, help=argparse.SUPPRESS)
    ap.add_argument("--lan", action="store_true", help="bind 0.0.0.0 for phone access; requires token (phone_token)")
    ap.add_argument("--public", action="store_true", help="token mode on 127.0.0.1 behind an HTTPS tunnel; any Host, token mandatory")
    a = ap.parse_args(); DRY = a.dry_run; DRY_ITERS = a.dry_iterations
    global LAN, TOKEN, PUBLIC
    if a.public:
        LAN = True; PUBLIC = True; TOKEN = load_token()
        if len(TOKEN) < 40: sys.exit("phone_token too short for public mode")
        if a.host == "127.0.0.1" and os.environ.get("PORT"): a.host = "0.0.0.0"   # cloud host (Render)
    elif a.lan:
        LAN = True; TOKEN = load_token()
        if a.host == "127.0.0.1": a.host = "0.0.0.0"
        ALLOWED_HOSTS.update(local_ips())
        if a.host not in ("0.0.0.0", ""): ALLOWED_HOSTS.add(a.host)
    if not ACCOUNT: sys.exit("BULK_ACCOUNT missing (put agent.env next to panel.py)")
    if not DRY and not os.environ.get("BULK_AGENT_PRIVATE_KEY"): sys.exit("BULK_AGENT_PRIVATE_KEY missing")
    eq_load()
    print("Bulk maker panel %son http://%s:%d  (Ctrl+C to stop)" % ("(DRY-RUN) " if DRY else "", a.host, a.port), flush=True)
    if LAN and not PUBLIC:
        url = "http://%s:%d/m?t=%s" % (lan_ip(), a.port, TOKEN)
        print("PHONE MODE: token required. Open on your phone (same Wi-Fi):\n  " + url, flush=True)
        try:
            import qrcode
            qr = qrcode.QRCode(border=1); qr.add_data(url); qr.make(fit=True); qr.print_ascii(invert=True)
        except Exception:
            print("(install 'qrcode' for a terminal QR code)")
        sys.stdout.flush()
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()

if __name__ == "__main__": main()

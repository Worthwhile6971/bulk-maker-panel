#!/usr/bin/env python3
"""Bulk.trade maker-only position closer.

Closes an open perp position with reduce-only ALO (post-only) limit orders
resting at the touch on the opposite side. Never sends market or IOC orders.
Default is DRY RUN: reads public data only and logs the orders it would send.
"""
import argparse, math, os, sys, time, json, signal
import requests

API = os.environ.get("BULK_API", "https://mainnet-api1.bulk.trade/api/v1")
DOMAIN = os.environ.get("BULK_NETWORK", "mainnet")  # signature domain
TIF = "ALO"  # hard-coded: post-only. Never GTC/IOC/market.

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

class RateLimiter:
    """Stay under 5 req/s and 5 order actions/s (we use 4/s for headroom)."""
    def __init__(self, per_sec=4):
        self.gap = 1.0 / per_sec; self.last = 0.0
    def wait(self):
        d = self.last + self.gap - time.monotonic()
        if d > 0: time.sleep(d)
        self.last = time.monotonic()

REQ = RateLimiter(4); ACT = RateLimiter(4)
S = requests.Session()

def get(path, **params):
    REQ.wait(); r = S.get(API + path, params=params, timeout=10); r.raise_for_status(); return r.json()

def post(path, body):
    REQ.wait(); r = S.post(API + path, json=body, timeout=10); r.raise_for_status(); return r.json()

def market_info(symbol):
    for m in get("/exchangeInfo"):
        if m["symbol"] == symbol:
            return m
    sys.exit(f"unknown symbol {symbol}")

def touch(symbol):
    b = get("/l2book", type="l2book", coin=symbol, nlevels=1)
    bids, asks = b["levels"][0], b["levels"][1]
    if not bids or not asks: return None, None
    return float(bids[0]["px"]), float(asks[0]["px"])

def full_account(account):
    try:
        res = post("/account", {"type": "fullAccount", "user": account})
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            log("account not found on Bulk (404) - check BULK_ACCOUNT"); return {"positions": []}
        raise
    for item in res:
        if "fullAccount" in item: return item["fullAccount"]
    raise RuntimeError(f"unexpected account response: {str(res)[:200]}")

def position(account, symbol):
    """Signed size (+long / -short) of the base-account (non-iso) or iso row."""
    fa = full_account(account)
    rows = [p for p in fa.get("positions", []) if p.get("symbol") == symbol]
    if len(rows) > 1:
        log("WARN: several rows for", symbol, "(base + isolated?) using the largest")
    if not rows: return 0.0, None
    p = max(rows, key=lambda r: abs(float(r["size"])))
    return float(p["size"]), p

def has_stop(pos_row):
    try:
        return any(o.get("orderType") in ("stop", "range", "trailing")
                   for o in pos_row.get("protection", {}).get("orders", []))
    except Exception:
        return False

def floor_to(x, step): return math.floor(x / step + 1e-9) * step
def ceil_to(x, step):  return math.ceil(x / step - 1e-9) * step
def rnd(x, step):
    d = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(x, d)

class Trader:
    def __init__(self, live, account):
        self.live = live; self.account = account; self.signer = None
        if live:
            import bulk_keychain as bk
            self.bk = bk
            key = os.environ.get("BULK_AGENT_PRIVATE_KEY")
            if not key: sys.exit("BULK_AGENT_PRIVATE_KEY not set")
            self.signer = bk.Signer.from_base58(key, DOMAIN)
            del key
            log("signer (agent) pubkey:", self.signer.pubkey, "| account:", account)

    def _send(self, action):
        ACT.wait()
        if not self.live:
            log("DRY-RUN would send:", json.dumps(action)); return {"dry": True}
        prep = self.bk.prepare_order(action, DOMAIN, self.account, signer=self.signer.pubkey)
        tx = self.signer.sign_prepared(prep)
        oid = tx.pop("order_id", None) or prep.get("order_id")
        res = post("/order", tx)
        sts = res.get("response", {}).get("data", {}).get("statuses", [])
        if res.get("status") != "ok":
            log("ERROR response:", str(res)[:300])
        return {"oid": oid, "statuses": sts, "raw_status": res.get("status"), "raw": str(res)[:300]}

    def place(self, symbol, is_buy, px, sz, iso):
        a = {"type": "order", "symbol": symbol, "is_buy": is_buy, "price": px, "size": sz,
             "order_type": {"type": "limit", "tif": TIF}, "reduce_only": True, "iso": iso}
        assert a["order_type"]["tif"] == "ALO" and a["reduce_only"] is True
        return self._send(a)

    def place_open(self, symbol, is_buy, px, sz, iso):
        """Maker OPEN: post-only ALO, NOT reduce-only. Separate path from closes."""
        a = {"type": "order", "symbol": symbol, "is_buy": is_buy, "price": px, "size": sz,
             "order_type": {"type": "limit", "tif": TIF}, "reduce_only": False, "iso": iso}
        assert a["order_type"]["tif"] == "ALO" and a["reduce_only"] is False
        return self._send(a)

    def place_protection(self, symbol, is_long, sz, sl=None, tp=None, iso=False):
        """Exchange-side conditional exit for a position (reduce-only by design on Bulk).
        SL+TP -> range/OCO (rng); SL only -> stop (st); TP only -> take_profit (tp).
        Triggers on mark price and executes market-style => TAKER fill when triggered."""
        if sl is None and tp is None: return None
        if sl is not None and tp is not None:
            a = {"type": "range", "symbol": symbol, "is_buy": bool(is_long), "size": sz,
                 "pmin": float(sl if is_long else tp), "pmax": float(tp if is_long else sl), "iso": iso}
        elif sl is not None:   # long: fire when mark <= sl (d=false); short: mark >= sl (d=true)
            a = {"type": "stop", "symbol": symbol, "is_buy": not is_long, "size": sz,
                 "trigger_price": float(sl), "iso": iso}
        else:                  # long: fire when mark >= tp (d=true); short: mark <= tp
            a = {"type": "take_profit", "symbol": symbol, "is_buy": bool(is_long), "size": sz,
                 "trigger_price": float(tp), "iso": iso}
        assert a["type"] in ("range", "stop", "take_profit")
        return self._send(a)

    def cancel(self, symbol, oid):
        return self._send({"type": "cancel", "symbol": symbol, "order_id": oid})

def status_kinds(res):
    return [list(s.keys())[0] for s in res.get("statuses", []) if isinstance(s, dict)]

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", required=True, help="e.g. BTC-USD")
    ap.add_argument("--size", default="all", help="'all' or base-asset amount to close")
    ap.add_argument("--offset-ticks", type=int, default=0,
                    help="ticks away from the touch on your own side (0 = join best ask/bid)")
    ap.add_argument("--reprice-sec", type=float, default=2.0, help="min seconds between re-quotes")
    ap.add_argument("--max-minutes", type=float, default=0, help="give up after N minutes (0 = no limit)")
    ap.add_argument("--watch", type=float, default=None,
                    help="trigger price: wait until mid reaches it (>= for longs, <= for shorts) before closing")
    ap.add_argument("--live", action="store_true", help="actually sign and send orders (default: dry run)")
    ap.add_argument("--dry-run", action="store_true", help="explicit dry run (the default)")
    ap.add_argument("--sim-position", type=float, default=None,
                    help="dry run only: pretend position size (+long/-short) instead of reading the account")
    ap.add_argument("--iterations", type=int, default=0, help="dry run: stop after N quote cycles (0 = until --max-minutes)")
    args = ap.parse_args()

    live = args.live and not args.dry_run
    account = os.environ.get("BULK_ACCOUNT")
    if live and not account: sys.exit("BULK_ACCOUNT (main wallet pubkey) not set")
    if live and args.sim_position is not None: sys.exit("--sim-position is dry-run only")
    if not live and account is None and args.sim_position is None:
        sys.exit("dry run needs BULK_ACCOUNT (read-only lookup) or --sim-position")

    mi = market_info(args.symbol)
    if "ALO" not in mi.get("timeInForces", []): sys.exit(f"{args.symbol} does not support ALO")
    tick = float(mi["tickSize"]); lot = float(mi.get("sizeIncrement") or mi.get("lotSize"))
    min_notional = float(mi.get("minNotional", 0))
    log(f"{'LIVE' if live else 'DRY-RUN'} | {args.symbol} tick={tick} lot={lot} minNotional={min_notional} status={mi.get('status')}")

    tr = Trader(live, account)
    sim = args.sim_position

    def read_pos():
        if sim is not None: return sim, {"iso": False}
        return position(account, args.symbol)

    pos, row = read_pos()
    if pos == 0: log("no open position; nothing to do"); return
    is_long = pos > 0; iso = bool(row.get("iso", False)) if row else False
    target = abs(pos) if args.size == "all" else min(abs(pos), float(args.size))
    start_abs = abs(pos)
    log(f"position {pos:+} ({'LONG' if is_long else 'SHORT'}) iso={iso}; closing {target}")
    if row is not None and sim is None and not has_stop(row):
        log("WARNING: no stop-loss found on this position. This bot is NOT a stop; keep a hard SL on.")

    deadline = time.time() + args.max_minutes * 60 if args.max_minutes > 0 else None
    oid = None; my_px = None; my_sz = None; last_quote = 0.0; cycles = 0
    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("now", True))

    if args.watch is not None:
        log(f"watching: start maker close when mid {'>=' if is_long else '<='} {args.watch}")
        while not stop["now"]:
            bid, ask = touch(args.symbol)
            if bid and ask:
                mid = (bid + ask) / 2
                if (is_long and mid >= args.watch) or (not is_long and mid <= args.watch):
                    log(f"trigger hit (mid {mid})"); break
            if deadline and time.time() > deadline: log("max time reached while watching"); return
            time.sleep(1)

    try:
        while not stop["now"]:
            if deadline and time.time() > deadline: log("max time reached"); break
            cur, _ = read_pos()
            if (cur > 0) != is_long or cur == 0: log("flat (or side flipped) -> done"); break
            closed = start_abs - abs(cur)
            remaining = floor_to(min(abs(cur), target - closed), lot)
            if remaining <= 0: log(f"target closed ({closed}) -> done"); break
            bid, ask = touch(args.symbol)
            if bid is None: time.sleep(args.reprice_sec); continue
            if is_long:   # sell, rest at/above best ask
                want = rnd(ceil_to(ask + args.offset_ticks * tick, tick), tick)
            else:         # buy, rest at/below best bid
                want = rnd(floor_to(bid - args.offset_ticks * tick, tick), tick)
            if remaining * want < min_notional:
                log(f"remaining {remaining} below minNotional {min_notional}; stop (close dust manually)"); break
            moved = (want != my_px) or (remaining != my_sz)
            if oid is None or (moved and time.time() - last_quote >= args.reprice_sec):
                if oid is not None:
                    r = tr.cancel(args.symbol, oid); k = status_kinds(r)
                    log(f"cancel {oid} -> {k or 'dry'}")
                    oid = None
                    if live: continue  # re-read position (it may have filled before the cancel)
                r = tr.place(args.symbol, not is_long, want, remaining, iso)
                k = status_kinds(r); last_quote = time.time()
                log(f"{'SELL' if is_long else 'BUY'} {remaining} @ {want} ALO reduce-only | bid {bid} ask {ask} -> {k or 'dry'}")
                if not live:
                    oid, my_px, my_sz = "DRYRUN", want, remaining
                elif any(x in ("resting", "working") for x in k):
                    oid, my_px, my_sz = r["oid"], want, remaining
                elif "rejectedCrossing" in k:
                    log("rejected for crossing (touch moved); re-quoting"); oid = None
                elif any(x in ("filled", "partiallyFilled") for x in k):
                    oid = None  # loop re-reads position
                else:
                    log("unexpected:", r.get("statuses")); oid = None; time.sleep(args.reprice_sec)
            cycles += 1
            if not live and args.iterations and cycles >= args.iterations: log("dry-run iterations done"); break
            time.sleep(max(0.5, args.reprice_sec / 2))
    finally:
        if oid and live:
            log("cleaning up resting order", oid, "->", status_kinds(tr.cancel(args.symbol, oid)))

if __name__ == "__main__":
    main()

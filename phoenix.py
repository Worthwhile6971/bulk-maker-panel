"""Phoenix perps venue for the panel: READ-ONLY reads + dry-run post-only previews.
No signing, no RPC, no transaction code lives here. Live submit is refused unless
PHOENIX_TRADING_ENABLED=1 AND no Phoenix halt AND a signer service exists (none is built yet)."""
import os, json, time, math, requests
API = os.environ.get("PHOENIX_API", "https://perp-api.phoenix.trade")
TRADER = os.environ.get("PHOENIX_TRADER", "tr3H9VuLVv9VsccPTcv7YJHEomDKZZPhULSV4bzkkGf")   # public address only
SUB = int(os.environ.get("PHOENIX_SUBACCOUNT", "0"))
QUOTE = 1e6
_C = {}
def _get(path, ttl=5):
    v = _C.get(path)
    if v and time.time() - v[0] < ttl: return v[1]
    r = requests.get(API + path, timeout=10); r.raise_for_status(); d = r.json(); _C[path] = (time.time(), d); return d
def halt():
    """Halt if PHOENIX_HALT env set, or a PHX_HALT.json with entries exists (default on Render: PHOENIX_HALT=1 recommended)."""
    e = os.environ.get("PHOENIX_HALT")
    if e is None: return "Phoenix floor halt on by default (cleared only by setting PHOENIX_HALT=0 after Caleb approves)"
    if e != "0": return e
    f = os.environ.get("PHOENIX_HALT_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "PHX_HALT.json"))
    try:
        d = json.load(open(f))
        if d: return "; ".join(str(v.get("reason", v)) if isinstance(v, dict) else str(v) for v in d.values())
    except FileNotFoundError: pass
    except Exception as e: return "halt file unreadable: %r" % e
    return None
def trading_enabled(): return os.environ.get("PHOENIX_TRADING_ENABLED") == "1"
def gate():
    h = halt()
    return {"live_enabled": trading_enabled() and not h, "flag": trading_enabled(), "halt": h,
            "signer": False, "note": "live Phoenix submit not implemented in this build (dry-run previews only)"}
def markets():
    out = []
    for m in _get("/v1/view/exchange/markets", 60):
        if m.get("marketStatus") != "active": continue
        lt = m.get("leverageTiers") or [{}]
        out.append({"symbol": m["symbol"], "name": (m.get("metadata") or {}).get("name"), "tickSize": m["tickSize"],
                    "baseLotsDecimals": m["baseLotsDecimals"], "makerFee": m.get("makerFee"), "takerFee": m.get("takerFee"),
                    "maxLeverage": lt[0].get("maxLeverage"), "isolatedOnly": m.get("isolatedOnly")})
    return sorted(out, key=lambda x: x["symbol"])
def _mk(sym):
    for m in _get("/v1/view/exchange/markets", 60):
        if m["symbol"] == sym: return m
    raise ValueError("unknown Phoenix market %s" % sym)
def book(sym):
    b = _get("/v1/view/orderbook/%s" % sym, 2)
    return {"bid": b["bids"][0][0] if b.get("bids") else None, "ask": b["asks"][0][0] if b.get("asks") else None}
def account():
    s = _get("/v1/trader/state/%s?traderPdaIndex=0" % TRADER, 5)["snapshot"]
    subs = []; pos = []; orders = []
    for a in s.get("subaccounts", []):
        subs.append({"sub": a["subaccountIndex"], "collateral_usd": int(a.get("collateral", "0")) / QUOTE,
                     "spot": [{"symbol": c["symbol"], "balance": int(c["balance"]) / 10 ** c["decimals"]} for c in a.get("spotCollaterals", [])]})
        for p in a.get("positions", []) or []:
            if int(p.get("basePositionLots", 0)) == 0: continue
            sym = p.get("symbol"); d = _mk(sym)["baseLotsDecimals"] if sym else 0
            pos.append({"sub": a["subaccountIndex"], "symbol": sym, "size": int(p["basePositionLots"]) / 10 ** d,
                        "sl": bool(p.get("stopLossTriggers") or p.get("conditionalStopLossTriggers")), "raw": {k: v for k, v in p.items() if not isinstance(v, (list, dict))}})
        for o in (a.get("orders") or a.get("limitOrders") or []):
            orders.append(dict(o, sub=a["subaccountIndex"]))
    return {"trader": TRADER, "state": (s.get("capabilities") or {}).get("state"), "subaccounts": subs, "positions": pos, "orders": orders}
def preview(sym, side, size_usd, px, sl, tp=None):
    """Dry-run post-only order. Never sends. Validates maker-only (no cross) and required SL."""
    side = str(side).lower(); size_usd = float(size_usd)
    if side not in ("long", "short"): raise ValueError("side must be long or short")
    if sl in (None, ""): raise ValueError("stop-loss is required for Phoenix orders")
    m = _mk(sym); bk = book(sym); sl = float(sl)
    tick = m["tickSize"] * 10 ** m["baseLotsDecimals"] / QUOTE if m["baseLotsDecimals"] >= 0 else m["tickSize"] / (10 ** -m["baseLotsDecimals"] * QUOTE)
    if px in (None, ""): px = bk["bid"] if side == "long" else bk["ask"]
    px = float(px); n = px / tick; n = round(n) if abs(n - round(n)) < 1e-6 else (math.floor if side == "long" else math.ceil)(n); px = round(n * tick, 10)
    warn = []
    if side == "long" and bk["ask"] is not None and px >= bk["ask"]: raise ValueError("post-only bid %.6g would cross ask %.6g" % (px, bk["ask"]))
    if side == "short" and bk["bid"] is not None and px <= bk["bid"]: raise ValueError("post-only ask %.6g would cross bid %.6g" % (px, bk["bid"]))
    if side == "long" and sl >= px or side == "short" and sl <= px: raise ValueError("stop-loss must be beyond entry")
    if size_usd < 1: raise ValueError("size too small")
    lot = 10 ** -m["baseLotsDecimals"]; qty = math.floor(size_usd / px / lot) * lot
    if qty <= 0: raise ValueError("size below one lot")
    risk = qty * abs(px - sl); r = None
    if tp not in (None, ""):
        tp = float(tp); r = abs(tp - px) / abs(px - sl)
        if r < 1: warn.append("TP under 1R")
    g = gate()
    if not g["live_enabled"]: warn.append("LIVE SUBMIT DISABLED: " + (("halt: " + g["halt"]) if g["halt"] else "PHOENIX_TRADING_ENABLED not set"))
    return {"ok": True, "dry_run": True, "venue": "phoenix", "symbol": sym, "side": side, "type": "post-only limit",
            "price": px, "tick": tick, "qty": round(qty, 10), "notional": round(qty * px, 4), "sl": sl, "sl_exit": "reduce-only stop",
            "tp": tp, "r_multiple": r, "risk_usd": round(risk, 4), "maker_fee_usd": round(qty * px * (m.get("makerFee") or 0), 4),
            "book": bk, "gate": g, "warn": warn}
def submit(body):
    """Approval-gated live submit. Always refused in this build."""
    g = gate()
    if body.get("approved") is not True: raise ValueError("explicit approval required (approved: true)")
    if g["halt"]: raise PermissionError("Phoenix halted: %s" % g["halt"])
    if not g["flag"]: raise PermissionError("Phoenix live trading disabled (PHOENIX_TRADING_ENABLED not set)")
    raise PermissionError("Phoenix live signer not deployed in this build; preview only")
def cancel(body):
    g = gate()
    if g["halt"] or not g["flag"]: raise PermissionError("Phoenix live actions disabled")
    raise PermissionError("Phoenix live signer not deployed in this build")

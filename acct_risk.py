"""Per-account risk state for the Bulk maker panel (20 Oct 2026).

Each panel account (main, challenge) has its OWN equity log and HALT file, so a halt on one never blocks the other:
  day P&L <= -7% -> halt until next SGT midnight; week P&L <= -20% -> until next Monday 00:00 SGT;
  equity 20% below its high-water mark -> halt until the file is removed by hand.
A halt blocks new OPENS only (closes, cancels and stops still work); it never flattens.
Same rules and maths as /workspace/bulk-maker-close/risk_guard.py, which is NOT imported or changed (box strategies keep
using their own HALT / risk_equity.json on main).
Note: on Render the disk is ephemeral, so baselines restart from current equity after a deploy/restart.
"""
import json, os, time, threading
SGT = 8 * 3600
DAY_HALT_PCT, WEEK_HALT_PCT, HWM_DD_PCT = 7.0, 20.0, 20.0
STOP_TRIES = 3
FLATTEN_MAKER_SEC = 90.0

def _ts(t=None): return time.strftime("%Y-%m-%d %H:%M:%S SGT", time.gmtime((t or time.time()) + SGT))
def _load(p, d):
    try: return json.load(open(p))
    except Exception: return d
def _save(p, obj):
    tmp = p + ".tmp"; json.dump(obj, open(tmp, "w"), indent=1); os.replace(tmp, p)
def day_key(t): return time.strftime("%Y-%m-%d", time.gmtime(t + SGT))
def week_key(t):
    lt = time.gmtime(t + SGT); monday = t + SGT - (lt.tm_wday * 86400 + lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
    return time.strftime("%Y-%m-%d", time.gmtime(monday))
def next_day_start(t):
    lt = time.gmtime(t + SGT); return t + (86400 - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec))
def next_week_start(t):
    lt = time.gmtime(t + SGT); return next_day_start(t) + (6 - lt.tm_wday) * 86400

class AcctRisk:
    """mode 'fixed': sizing/cap off a fixed dollar base (main, $1,218). mode 'equity': per-trade risk = risk_pct of LIVE equity."""
    def __init__(self, key, dirpath, mode, base_usd=None, risk_pct=None, cap_pct=5.0, enforce_cap=False, day_week_halts=True, hwm_halt=True):
        self.hwm_halt = hwm_halt   # 23 Oct 2026: False on challenge (Caleb approved)
        self.day_week_halts = day_week_halts   # 23 Oct 2026: False on challenge (Caleb via Tom)
        self.key = key; self.mode = mode; self.base_usd = base_usd; self.risk_pct = risk_pct
        self.cap_pct = cap_pct; self.enforce_cap = enforce_cap
        self.eq_file = os.path.join(dirpath, "risk_equity_%s.json" % key)
        self.halt_file = os.path.join(dirpath, "HALT_%s" % key)
        self.alert_log = os.path.join(dirpath, "ALERTS_%s.log" % key)
        self.lock = threading.Lock(); self.alerts = []

    def alert(self, msg, log_fn=None):
        line = "%s ALERT [%s] %s" % (_ts(), self.key, msg)
        self.alerts.append({"t": int(time.time()), "text": "[%s] %s" % (self.key, msg)}); del self.alerts[:-30]
        try:
            with open(self.alert_log, "a") as fh: fh.write(line + "\n")
        except Exception: pass
        print(line, flush=True)
        if log_fn: log_fn("ALERT " + msg)

    def read_halt(self, t=None):
        t = t or time.time()
        if not os.path.exists(self.halt_file): return False, None
        info = _load(self.halt_file, None)
        if not isinstance(info, dict): info = {"reason": "manual HALT file", "manual": True}
        u = info.get("until")
        if not self.day_week_halts and info.get("kind") in ("day", "week") or (not self.hwm_halt and info.get("kind") == "hwm"):
            try: os.remove(self.halt_file)
            except Exception: pass
            return False, None
        if u and t >= u:
            try: os.remove(self.halt_file)
            except Exception: pass
            return False, None
        return True, info

    def write_halt(self, reason, until=None, kind="manual"):
        h, cur = self.read_halt()
        if h and (cur.get("until") is None or (until is not None and cur.get("until", 0) >= until)): return cur
        info = {"account": self.key, "reason": reason, "kind": kind, "since": time.time(), "since_sgt": _ts(), "until": until,
                "until_sgt": _ts(until) if until else "until cleared by hand"}
        _save(self.halt_file, info); self.alert("HALT set (%s): %s -> %s" % (kind, reason, info["until_sgt"])); return info

    def evaluate(self, eq, t=None):
        t = t or time.time()
        with self.lock:
            st = _load(self.eq_file, {})
            dk, wk = day_key(t), week_key(t)
            if st.get("day", {}).get("key") != dk: st["day"] = {"key": dk, "start": eq, "t": t}
            if st.get("week", {}).get("key") != wk: st["week"] = {"key": wk, "start": eq, "t": t}
            st["hwm"] = max(float(st.get("hwm") or 0.0), eq); st["last"] = {"eq": eq, "t": t}
            d0, w0, hwm = st["day"]["start"], st["week"]["start"], st["hwm"]
            day = (eq / d0 - 1) * 100 if d0 > 0 else 0.0
            week = (eq / w0 - 1) * 100 if w0 > 0 else 0.0
            dd = (eq / hwm - 1) * 100 if hwm > 0 else 0.0
            try: _save(self.eq_file, st)
            except Exception: pass
            if eq > 0:
                if self.day_week_halts and day <= -DAY_HALT_PCT: self.write_halt("day P&L %.2f%% <= -7%%" % day, next_day_start(t), "day")
                if self.day_week_halts and week <= -WEEK_HALT_PCT: self.write_halt("week P&L %.2f%% <= -20%%" % week, next_week_start(t), "week")
                if self.hwm_halt and dd <= -HWM_DD_PCT: self.write_halt("drawdown %.2f%% from HWM $%.2f" % (dd, hwm), None, "hwm")
            h, info = self.read_halt(t)
        return {"halted": h, "halt": info, "dayPct": round(day, 2), "weekPct": round(week, 2), "ddPct": round(dd, 2),
                "hwm": round(hwm, 2), "dayStart": round(d0, 2), "weekStart": round(w0, 2)}

    def rebuild(self, eq, upnl, closed, activity, t=None):
        """Halt state rebuilt from Bulk history (survives Render restarts; no disk needed, which is not free on Render).
        Uses TRADING P&L only (closed positions: realizedPnl + fees + funding, plus current uPnL), so deposits, withdrawals
        and internal transfers never move day/week P&L. Day/week use the WORST point of the period's realized curve, so a
        halt stays on even if equity recovers. HWM is measured from the latest deposit/withdrawal/transfer (auto-rebase)."""
        t = t or time.time(); ns = lambda x: int(x) / 1e9
        rows = sorted(((ns(r.get("closeTime", 0)), float(r.get("realizedPnl") or 0) + float(r.get("fees") or 0) + float(r.get("funding") or 0))
                       for r in closed or [] if r.get("closeTime")), key=lambda x: x[0])
        flows = [ns(a.get("timestamp", 0)) for a in activity or [] if a.get("status", "completed") == "completed"
                 and str(a.get("activityType", "")) in ("deposit", "withdraw", "withdrawal", "transferInternal", "transferExternal")]
        real_now = eq - upnl
        def period(b):
            after = [p for ts, p in rows if ts >= b]; start = real_now - sum(after)
            cur = start; worst = start; best = start
            for p in after: cur += p; worst = min(worst, cur); best = max(best, cur)
            return start, min(worst, eq), max(best, eq)
        def at(ts):
            lt = time.gmtime(ts + SGT); return ts - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
        d_b = at(t); w_b = d_b - time.gmtime(t + SGT).tm_wday * 86400
        h_b = max(flows) if flows else 0.0
        d0, dmin, _ = period(d_b); w0, wmin, _ = period(w_b); _, _, hwm = period(h_b)
        pct = lambda a, b: (a / b - 1) * 100 if b > 0 else 0.0
        trig = []
        if eq > 0:
            if self.day_week_halts and pct(dmin, d0) <= -DAY_HALT_PCT: trig.append(("day", "day P&L hit %.2f%% (start $%.2f, low $%.2f)" % (pct(dmin, d0), d0, dmin), next_day_start(t)))
            if self.day_week_halts and pct(wmin, w0) <= -WEEK_HALT_PCT: trig.append(("week", "week P&L hit %.2f%% (start $%.2f, low $%.2f)" % (pct(wmin, w0), w0, wmin), next_week_start(t)))
            if self.hwm_halt and pct(eq, hwm) <= -HWM_DD_PCT: trig.append(("hwm", "drawdown %.2f%% from HWM $%.2f" % (pct(eq, hwm), hwm), None))
        for kind, why, until in trig: self.write_halt(why, until, kind)
        h, info = self.read_halt(t)
        if trig and not h: h, info = True, {"reason": "; ".join(x[1] for x in trig), "kind": "rebuilt"}
        return {"halted": h, "halt": info, "source": "bulk-history", "dayPct": round(pct(eq, d0), 2), "dayWorstPct": round(pct(dmin, d0), 2),
                "weekPct": round(pct(eq, w0), 2), "weekWorstPct": round(pct(wmin, w0), 2), "ddPct": round(pct(eq, hwm), 2),
                "hwm": round(hwm, 2), "hwmSince": _ts(h_b) if h_b else "all history", "dayStart": round(d0, 2), "weekStart": round(w0, 2),
                "closedUsed": len(rows), "flowsSeen": len(flows)}

    def cap_usd(self, eq):
        base = self.base_usd if self.mode == "fixed" else eq
        return base * self.cap_pct / 100.0

    def trade_risk_usd(self, eq, pct=None):
        """Max $ loss at SL per trade (None = not enforced, main keeps its % of margin sizing).
        22 Oct 2026 (Caleb via Tom): equity-mode accounts risk the % Caleb picks per trade (pct), no cap."""
        if self.mode == "equity" and (pct or self.risk_pct): return eq * float(pct or self.risk_pct) / 100.0
        return None

    def describe(self, eq):
        return {"mode": self.mode, "riskPct": self.risk_pct, "baseUsd": self.base_usd if self.mode == "fixed" else round(eq, 2),
                "perTradeUsd": None if self.trade_risk_usd(eq) is None else round(self.trade_risk_usd(eq), 2),
                "capPct": self.cap_pct, "capUsd": round(self.cap_usd(eq), 2), "capEnforced": self.enforce_cap,
                "limits": {"day": -DAY_HALT_PCT, "week": -WEEK_HALT_PCT, "hwmDD": -HWM_DD_PCT}}

def place_stop_with_retry(place_fn, verify_fn=None, tries=STOP_TRIES, delay=2.0, log_fn=print):
    last = (False, None)
    for i in range(tries):
        try: last = place_fn()
        except Exception as e: log_fn("stop attempt %d/%d error %r" % (i + 1, tries, e)); last = (False, None)
        ok = bool(last[0])
        if ok and verify_fn is not None:
            time.sleep(1.0)
            try: ok = bool(verify_fn())
            except Exception as e: log_fn("stop verify error %r" % (e,)); ok = False
        if ok: return True, last[1], i + 1
        log_fn("stop attempt %d/%d not confirmed" % (i + 1, tries))
        if i + 1 < tries: time.sleep(delay)
    return False, last[1], tries

def flatten_unprotected(tr, C, account, sym, risk, maker_sec=FLATTEN_MAKER_SEC, log_fn=print, sleep=time.sleep, clock=time.time):
    """Reduce-only flatten of `account`'s position on `sym`: maker ALO re-pegged for maker_sec, then reduce-only IOC collared 1%."""
    risk.alert("%s FLATTEN unprotected position (stop failed %dx): maker ALO first, IOC after %.0fs" % (sym, STOP_TRIES, maker_sec), log_fn)
    m = C.market_info(sym); tick = float(m["tickSize"]); lot = float(m.get("sizeIncrement") or m.get("lotSize") or 1e-8)
    t_end = clock() + maker_sec; oid = None; px_now = None
    def pos():
        s, row = C.position(account, sym); return s, bool((row or {}).get("iso", False))
    try:
        while True:
            size, iso = pos()
            if size == 0: log_fn("%s flat after maker flatten" % sym); return True
            is_long = size > 0; rem = round(C.floor_to(abs(size), lot), 8)
            if clock() >= t_end: break
            bid, ask = C.touch(sym)
            if bid is None: sleep(2); continue
            want = C.rnd(C.ceil_to(ask, tick), tick) if is_long else C.rnd(C.floor_to(bid, tick), tick)
            if oid is None or want != px_now:
                if oid: tr.cancel(sym, oid); oid = None
                r = tr.place(sym, not is_long, want, rem, iso)   # asserts ALO + reduce_only
                k = C.status_kinds(r); px_now = want
                if any(x in ("resting", "working") for x in k): oid = r.get("oid")
                log_fn("%s flatten ALO RO %s %g @ %s -> %s" % (sym, "SELL" if is_long else "BUY", rem, want, k or "dry"))
                if not tr.live: return True
            sleep(3)
    finally:
        if oid and tr.live:
            try: tr.cancel(sym, oid)
            except Exception: pass
    size, iso = pos()
    if size == 0: return True
    is_long = size > 0; rem = round(C.floor_to(abs(size), lot), 8)
    if rem <= 0: return True
    bid, ask = C.touch(sym)
    px = C.rnd(C.floor_to(bid * 0.99, tick), tick) if is_long else C.rnd(C.ceil_to(ask * 1.01, tick), tick)
    r = tr.place_reduce_ioc(sym, not is_long, px, rem, iso)
    risk.alert("%s FLATTEN reduce-only IOC %s %g limit %s -> %s" % (sym, "SELL" if is_long else "BUY", rem, px, C.status_kinds(r)), log_fn)
    size, _ = pos()
    if size != 0: risk.alert("%s STILL OPEN %s after flatten - MANUAL ACTION NEEDED" % (sym, size), log_fn)
    return size == 0

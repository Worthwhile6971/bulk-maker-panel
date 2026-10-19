"""Offline panel tests (19 Oct 2026 safety package). Network blocked; no job thread is started; no order can be sent."""
import os, sys, tempfile, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, HERE)
os.environ.setdefault("BULK_ACCOUNT", "TESTACCOUNT")
import requests
def _blocked(*a, **k): raise RuntimeError("NETWORK BLOCKED IN TESTS")
requests.Session.request = _blocked
import panel as P, close as C
SENT = []
C.post = lambda path, body: SENT.append(path) or _blocked()
P.DRY = True   # dry Trader: cannot sign or send
P.NET_FILE = os.path.join(tempfile.mkdtemp(), "net.json")
P.PUB.update({"main": "TESTACCOUNT", "challenge": "TESTCHALLENGE"}); P.PUB_T[0] = 9e18
for _r in P.RISK.values(): _d = tempfile.mkdtemp(); _r.eq_file = _d + "/eq.json"; _r.halt_file = _d + "/HALT"; _r.alert_log = _d + "/a.log"
P.preview = lambda key, sym, side, pct, lev, sl=None: {"ok": True, "pct": 10, "qty": 1.0, "leverage": 5, "warn": []}
LAUNCHED = []; P._launch = lambda j: LAUNCHED.append(j) or j
fa = lambda *pos: {"positions": [{"symbol": s, "size": z} for s, z in pos]}
class T(unittest.TestCase):
    def test_open_requires_sl_or_confirm(self):
        LAUNCHED.clear()
        with self.assertRaises(ValueError): P.start_open({"symbol": "SOL-USD", "side": "short", "percent": 10, "acct": "main"})
        with self.assertRaises(ValueError): P.start_open({"symbol": "SOL-USD", "side": "short", "percent": 10, "acct": "main", "confirm_no_sl": "yes"})  # must be literal true
        j = P.start_open({"symbol": "SOL-USD", "side": "short", "percent": 10, "acct": "main", "sl": 107}); self.assertEqual(j.sl, 107.0)
        j = P.start_open({"symbol": "SOL-USD", "side": "short", "percent": 10, "acct": "main", "confirm_no_sl": True}); self.assertIsNone(j.sl)
        self.assertEqual(len(LAUNCHED), 2)
    def test_net_watch(self):
        P.NET["sizes"] = None; P.NET_ALERTS.clear(); P.JOBS.clear()
        P.net_watch(fa(("SOL-USD", -11.0))); self.assertEqual(P.NET_ALERTS, [])          # baseline
        P.net_watch(fa(("SOL-USD", -11.0))); self.assertEqual(P.NET_ALERTS, [])
        P.net_watch(fa(("SOL-USD", -6.0))); self.assertEqual(len(P.NET_ALERTS), 1)       # unexpected change
        class J: sym = "ETH-USD"; state = "working"
        P.JOBS["x"] = J()
        P.net_watch(fa(("SOL-USD", -6.0), ("ETH-USD", 0.5))); self.assertEqual(len(P.NET_ALERTS), 1)   # panel job on ETH -> expected
        self.assertEqual(P.dismiss_alerts()["dismissed"], 1)
    def test_protective_asserts(self):
        with self.assertRaises(AssertionError): C.assert_protective({"type": "stop", "is_buy": False, "size": 1}, False, 1)
    def test_account_required_and_isolated(self):
        for bad in (None, "", "Main", "foo"):
            with self.assertRaises(ValueError): P.acct_pub(bad)
            with self.assertRaises(ValueError): P.start_open({"symbol": "SOL-USD", "side": "short", "percent": 10, "sl": 107, "acct": bad})
        self.assertNotEqual(P.acct_pub("main"), P.acct_pub("challenge"))
        j = P.start_open({"symbol": "SOL-USD", "side": "short", "percent": 10, "sl": 107, "acct": "challenge"})
        self.assertEqual((j.acct, j.pub, j.trader.account), ("challenge", "TESTCHALLENGE", "TESTCHALLENGE"))
        j = P.start_close("main", "SOL-USD", 50); self.assertEqual(j.pub, "TESTACCOUNT")
    def test_challenge_requires_sl(self):
        with self.assertRaises(ValueError): P.start_open({"symbol": "SOL-USD", "side": "short", "percent": 10, "confirm_no_sl": True, "acct": "challenge"})
    def test_halt_per_account(self):
        r = P.RISK["challenge"]; r.evaluate(100.0); st = r.evaluate(92.0)   # -8% day
        self.assertTrue(st["halted"]); self.assertFalse(P.RISK["main"].read_halt()[0])
        with self.assertRaises(ValueError): P.start_open({"symbol": "SOL-USD", "side": "short", "percent": 10, "sl": 107, "acct": "challenge"})
        P.start_open({"symbol": "SOL-USD", "side": "short", "percent": 10, "sl": 107, "acct": "main"})
        import os; os.remove(r.halt_file)
    def test_risk_sizing(self):
        self.assertEqual(P.RISK["challenge"].trade_risk_usd(100.0), 1.0); self.assertIsNone(P.RISK["main"].trade_risk_usd(2000))
        self.assertAlmostEqual(P.RISK["main"].cap_usd(5000), 60.9)
    def test_zz_nothing_sent(self): self.assertEqual(SENT, [])
if __name__ == "__main__": unittest.main(verbosity=2)

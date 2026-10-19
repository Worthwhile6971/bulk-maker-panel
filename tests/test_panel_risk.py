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
P.preview = lambda sym, side, pct, lev: {"ok": True, "pct": 10, "qty": 1.0, "leverage": 5, "warn": []}
LAUNCHED = []; P._launch = lambda j: LAUNCHED.append(j) or j
fa = lambda *pos: {"positions": [{"symbol": s, "size": z} for s, z in pos]}
class T(unittest.TestCase):
    def test_open_requires_sl_or_confirm(self):
        with self.assertRaises(ValueError): P.start_open({"symbol": "SOL-USD", "side": "short", "percent": 10})
        with self.assertRaises(ValueError): P.start_open({"symbol": "SOL-USD", "side": "short", "percent": 10, "confirm_no_sl": "yes"})  # must be literal true
        j = P.start_open({"symbol": "SOL-USD", "side": "short", "percent": 10, "sl": 107}); self.assertEqual(j.sl, 107.0)
        j = P.start_open({"symbol": "SOL-USD", "side": "short", "percent": 10, "confirm_no_sl": True}); self.assertIsNone(j.sl)
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
    def test_zz_nothing_sent(self): self.assertEqual(SENT, [])
if __name__ == "__main__": unittest.main(verbosity=2)

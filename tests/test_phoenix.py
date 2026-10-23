"""Offline Phoenix venue tests: no network, no signing."""
import os, sys, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, HERE)
import phoenix as X
MK = [{"symbol": "SOL", "marketStatus": "active", "tickSize": 100, "baseLotsDecimals": 2, "makerFee": 0.00005, "takerFee": 0.00035, "leverageTiers": [{"maxLeverage": 20}]}]
def fake(path, ttl=5):
    if path.endswith("markets"): return MK
    if "orderbook" in path: return {"bids": [[100.0, 1]], "asks": [[100.1, 1]]}
    raise AssertionError(path)
X._get = fake
class T(unittest.TestCase):
    def setUp(self): os.environ.pop("PHOENIX_TRADING_ENABLED", None); os.environ.pop("PHOENIX_HALT", None)
    def test_default_disabled_and_halted(self):
        g = X.gate(); self.assertFalse(g["live_enabled"]); self.assertTrue(g["halt"])
    def test_flag_alone_with_halt_still_off(self):
        os.environ["PHOENIX_TRADING_ENABLED"] = "1"; self.assertFalse(X.gate()["live_enabled"])
    def test_sl_required(self):
        with self.assertRaises(ValueError): X.preview("SOL", "long", 50, 99.9, None)
    def test_post_only_no_cross(self):
        with self.assertRaises(ValueError): X.preview("SOL", "long", 50, 100.1, 95)
        with self.assertRaises(ValueError): X.preview("SOL", "short", 50, 100.0, 105)
    def test_sl_side(self):
        with self.assertRaises(ValueError): X.preview("SOL", "long", 50, 99.9, 101)
    def test_preview_dry(self):
        p = X.preview("SOL", "long", 50, None, 95, 110)
        self.assertTrue(p["dry_run"]); self.assertEqual(p["price"], 100.0); self.assertGreater(p["qty"], 0); self.assertTrue(p["warn"])
    def test_submit_always_refused(self):
        os.environ["PHOENIX_TRADING_ENABLED"] = "1"; os.environ["PHOENIX_HALT"] = "0"
        with self.assertRaises(ValueError): X.submit({"approved": False})
        X.signer_available = lambda: False
        with self.assertRaises(PermissionError): X.submit({"approved": True, "sl": 1})   # no signer -> refused
        os.environ.pop("PHOENIX_HALT")
        with self.assertRaises(PermissionError): X.submit({"approved": True, "sl": 1})   # halt default on
        with self.assertRaises(PermissionError): X.cancel({"approved": True})
    def test_owner_key_never_passed_to_signer(self):
        self.assertNotIn("PHOENIX_TRADER_PRIVATE_KEY", X.SAFE_ENV)
    def test_no_signing_code(self):
        src = open(X.__file__).read()
        for bad in ("sendTransaction", "PRIVATE_KEY", "secret_key", "Keypair"): self.assertNotIn(bad, src)
if __name__ == "__main__": unittest.main()

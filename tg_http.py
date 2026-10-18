"""Authenticated, read-only HTTP endpoints mounted on the panel's existing ThreadingHTTPServer handler.

Routes (all need `Authorization: Bearer <TG_READ_TOKEN>`; the token is never accepted in the URL):
  GET  /tg/status                      reader health (no message content)
  GET  /tg/summary[?brief=1][&flagged=1]   latest hourly unread summary (brief=1 drops snippets, flagged=1 keeps only priority/alert items)
  GET  /tg/vanguard?since_id=N&limit=K new Vanguard posts with id > N (raw text + parsed fields)
  POST /tg/stop                        stops ONLY the Telegram reader thread (cannot restart without a redeploy)
No secrets (api hash, session string, token) are ever returned. Everything outside /tg/ is passed untouched
to the panel's original handler.
"""
import collections
import hashlib
import hmac
import json
import threading
import time
from urllib.parse import urlparse, parse_qs


class Limiter:
    """Sliding-window limits: global requests/min, plus failed-auth lockout per client key."""

    def __init__(self, max_per_min=60, max_fails=10, fail_window=900):
        self.lock = threading.Lock()
        self.reqs = collections.deque()
        self.fails = {}
        self.max_per_min, self.max_fails, self.fail_window = max_per_min, max_fails, fail_window

    def allow(self, key, now=None):
        now = now or time.time()
        with self.lock:
            while self.reqs and now - self.reqs[0] > 60:
                self.reqs.popleft()
            fl = [t for t in self.fails.get(key, []) if now - t < self.fail_window]
            if fl:
                self.fails[key] = fl
            else:
                self.fails.pop(key, None)
            if len(fl) >= self.max_fails or len(self.reqs) >= self.max_per_min:
                return False
            self.reqs.append(now)
            if len(self.fails) > 1000:    # bound memory
                self.fails.clear()
            return True

    def fail(self, key, now=None):
        with self.lock:
            self.fails.setdefault(key, []).append(now or time.time())


def token_ok(header_value, token):
    """Constant-time bearer check (compares fixed-length digests so length does not leak)."""
    if not token or len(token) < 32:
        return False
    hv = (header_value or "")
    presented = hv[7:].strip() if hv[:7].lower() == "bearer " else ""
    a = hashlib.sha256(presented.encode()).digest()
    b = hashlib.sha256(token.encode()).digest()
    return hmac.compare_digest(a, b) and bool(presented)


def _client_key(h):
    """Rate-limit key: CF-Connecting-IP when present (Render sits behind Cloudflare, which sets it; the panel itself
    trusts it first), else the rightmost X-Forwarded-For hop (added by the closest proxy), else the socket peer.
    Caveat: if the origin were ever reachable without Cloudflare the header is spoofable; the 60 req/min global limit
    and the 32+ char token still hold, only the per-client lockout could be dodged."""
    cf = (h.headers.get("CF-Connecting-IP") or "").strip()
    if cf and len(cf) <= 64:
        return cf
    xff = h.headers.get("X-Forwarded-For") or ""
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    return parts[-1] if parts else h.client_address[0]


def _send(h, code, obj):
    b = json.dumps(obj, ensure_ascii=False).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Cache-Control", "no-store")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Content-Length", str(len(b)))
    h.end_headers()
    h.wfile.write(b)


def handle(h, method, reader, cfg, limiter):
    key = _client_key(h)
    if not cfg.read_token or len(cfg.read_token) < 32:
        return _send(h, 404, {"error": "not found"})
    if not limiter.allow(key):
        return _send(h, 429, {"error": "rate limited"})
    if not token_ok(h.headers.get("Authorization"), cfg.read_token):
        limiter.fail(key)
        return _send(h, 401, {"error": "unauthorized"})
    u = urlparse(h.path)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    p = u.path.rstrip("/")
    if method == "GET" and p == "/tg/status":
        return _send(h, 200, reader.get_status())
    if method == "GET" and p == "/tg/summary":
        s = reader.get_summary()
        if s is None:
            return _send(h, 503, {"error": "no summary yet", "status": reader.get_status().get("state")})
        s = dict(s)
        items = s["items"]
        if q.get("flagged") == "1":
            items = [i for i in items if i.get("priority") or i.get("alert_hits")]
        if q.get("brief") == "1":
            items = [{k: v for k, v in i.items() if k != "snippets"} for i in items]
        s["items"] = items
        s["status"] = reader.get_status().get("state")
        return _send(h, 200, s)
    if method == "GET" and p == "/tg/vanguard":
        try:
            since = int(q.get("since_id", "0") or 0)
            limit = int(q.get("limit", "50") or 50)
        except ValueError:
            return _send(h, 400, {"error": "since_id and limit must be integers"})
        posts, truncated, last_id = reader.store.read_since(since, limit)
        return _send(h, 200, {"posts": posts, "count": len(posts), "last_id": last_id, "truncated": truncated,
                              "persistent": reader.store.persistent})
    if method == "POST" and p == "/tg/stop":
        reader.stop("stopped:by-endpoint")
        return _send(h, 200, {"ok": True, "state": "stopped:by-endpoint"})
    return _send(h, 404, {"error": "not found"})


def wrap_handler(handler_cls, reader, cfg):
    """Patch do_GET / do_POST so /tg/* is served here and everything else reaches the original code unchanged."""
    limiter = Limiter()
    reader.limiter = limiter
    orig_get, orig_post = handler_cls.do_GET, handler_cls.do_POST

    def _is_tg(path):
        return path == "/tg" or path.startswith("/tg/") or path.startswith("/tg?")

    def do_GET(self):
        if _is_tg(self.path):
            try:
                return handle(self, "GET", reader, cfg, limiter)
            except Exception:   # never let a /tg bug surface as anything but a 500 on /tg
                try:
                    _send(self, 500, {"error": "internal"})
                except Exception:
                    pass
                return
        return orig_get(self)

    def do_POST(self):
        if _is_tg(self.path):
            try:
                return handle(self, "POST", reader, cfg, limiter)
            except Exception:
                try:
                    _send(self, 500, {"error": "internal"})
                except Exception:
                    pass
                return
        return orig_post(self)

    handler_cls.do_GET, handler_cls.do_POST = do_GET, do_POST

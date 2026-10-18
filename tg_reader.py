"""Read-only Telegram poller that runs as an isolated daemon thread beside the Bulk panel.

Isolation contract (see PLAN.md):
  * Nothing here runs unless TG_ENABLED is truthy; with it off, install() is a no-op.
  * telethon is imported lazily INSIDE the worker thread, so a missing/broken dependency cannot break
    the panel's startup.
  * The worker thread catches everything, restarts itself with exponential backoff, gives up on fatal
    auth errors (never hammer Telegram) and shuts itself down if the CONTAINER memory (cgroup, not Python RSS) passes TG_MEM_CAP_MB.
  * Waits at least 180 s after boot before connecting (overlapping deploys + one shared session string = AuthKeyDuplicated).
  * Skips the dialog scan while the panel reports a running job (optional busy_fn passed by panel_hook.patch).
  * STRICTLY read-only: ReadOnlyClient is a whitelist proxy (connect, disconnect, is_user_authorized,
    iter_dialogs, iter_messages, get_entity, get_input_entity, get_me, is_connected). A second guard
    sits at the MTProto request level (ReadOnlyTelegramClient.__call__) and only lets an exact allowlist of read requests (GetDialogs, GetHistory, GetUsers, ...)
    requests through. Nothing marks read, sends, joins, leaves, edits or deletes.
  * Logs go to a private logger ("tg_reader") and NEVER contain message text, names, or secrets.
Python 3.9+ compatible.
"""
import asyncio
import collections
import gc
import hmac
import json
import logging
import os
import random
import re
import sys
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone

try:
    from tg_signal import parse_signal
except ImportError:  # pragma: no cover - when imported as a package
    from .tg_signal import parse_signal

log = logging.getLogger("tg_reader")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("[tg_reader] %(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

SGT = timezone(timedelta(hours=8))
TELEGRAM_SERVICE_ID = 777000   # official "Telegram" account (login codes) -> never included
ALERT_KEYWORDS_CI = ["sanctum", "phoenix", "eugene", "proposal", "swsol", "phoenixsol", "deadline", "urgent"]
ALERT_KEYWORDS_CS = ["INF"]    # whole word, upper-case only (avoids "inf" noise)

# The ONLY client methods the reader may touch. Anything else raises WriteBlocked.
ALLOWED_CLIENT_METHODS = frozenset({"connect", "disconnect", "is_user_authorized", "iter_dialogs",
                                    "iter_messages", "get_entity", "get_input_entity", "get_me", "is_connected"})
# Exact MTProto request class names allowed through the low-level guard (everything else, including
# Get* requests that act, e.g. GetBotCallbackAnswer / GetMessagesViews, is refused). If a needed read is
# missing, the reader reports "MTProto request X blocked" in /tg/status last_error; add it here deliberately.
ALLOWED_REQUEST_NAMES = frozenset({"GetDialogs", "GetPeerDialogs", "GetPinnedDialogs", "GetDialogFilters",
                                   "GetHistory", "GetMessages", "GetUsers", "GetChats", "GetChannels",
                                   "GetFullChannel", "GetFullChat", "GetFullUser", "GetConfig", "GetCdnConfig",
                                   "ResolveUsername"})
FATAL_ERRORS = frozenset({"AuthKeyDuplicatedError", "AuthKeyUnregisteredError", "SessionRevokedError",
                          "SessionExpiredError", "UserDeactivatedError", "UserDeactivatedBanError",
                          "PhoneNumberBannedError", "FatalAuth"})


# Deploys on Render can overlap the old and the new instance for up to ~90 s. Two instances sharing ONE
# session string make Telegram raise AuthKeyDuplicatedError and kill the session for good, so the reader never
# connects sooner than this many seconds after process start, whatever TG_START_DELAY says.
MIN_START_DELAY = 180
DEFAULT_MEM_CAP_MB = 400       # of the 512 MB container; measured on the container cgroup, not on Python RSS
BUSY_RETRY_SECONDS = 60        # while a panel job runs, re-check this often (no Telegram traffic meanwhile)
CGROUP_V2_USAGE = "/sys/fs/cgroup/memory.current"
CGROUP_V2_STAT = "/sys/fs/cgroup/memory.stat"
CGROUP_V1_USAGE = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
CGROUP_V1_STAT = "/sys/fs/cgroup/memory/memory.stat"


class _Skip(Exception):
    pass


class WriteBlocked(RuntimeError):
    pass


class FatalAuth(RuntimeError):
    pass


class ReadOnlyClient:
    """Whitelist proxy: any attribute not in ALLOWED_CLIENT_METHODS (send_*, read acks, join, delete...) raises."""

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name):
        if name not in ALLOWED_CLIENT_METHODS:
            raise WriteBlocked("client.%s is not allowed (read-only reader)" % name)
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name, value):
        raise WriteBlocked("read-only proxy cannot be modified")

    def __call__(self, *a, **k):
        raise WriteBlocked("raw MTProto requests are not allowed through the read-only proxy")


def request_allowed(request):
    reqs = request if isinstance(request, (list, tuple)) else [request]
    for r in reqs:
        n = type(r).__name__
        if n.endswith("Request"):
            n = n[:-7]
        if n not in ALLOWED_REQUEST_NAMES:
            return False
    return True


def _config_int(env, key, default, lo=None, hi=None):
    try:
        v = int(str(env.get(key, "")).strip() or default)
    except ValueError:
        v = default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _truthy(s):
    return str(s or "").strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.enabled = _truthy(env.get("TG_ENABLED"))              # kill switch: default OFF
        self.api_id = (env.get("TG_API_ID") or "").strip()
        self.api_hash = (env.get("TG_API_HASH") or "").strip()
        self.string_session = (env.get("TG_STRING_SESSION") or "").strip()
        self.read_token = (env.get("TG_READ_TOKEN") or "").strip()
        self.watch_channel = (env.get("TG_WATCH_CHANNEL") or "Vanguard Crypto Club").strip()
        self.data_dir = (env.get("TG_DATA_DIR") or "").strip()
        self.urgent_json = env.get("TG_URGENT_JSON") or ""
        # floor of MIN_START_DELAY (180 s): an overlapping old deploy instance must be gone before we connect
        self.start_delay = _config_int(env, "TG_START_DELAY", MIN_START_DELAY, MIN_START_DELAY, 3600)
        self.summary_interval = _config_int(env, "TG_SUMMARY_INTERVAL", 3600, 300, 86400)
        self.vanguard_interval = _config_int(env, "TG_VANGUARD_INTERVAL", 120, 30, 3600)
        self.max_dialogs = _config_int(env, "TG_MAX_DIALOGS", 100, 20, 500)
        self.max_snippets = _config_int(env, "TG_MAX_SNIPPETS", 3, 0, 5)
        self.snippet_len = _config_int(env, "TG_SNIPPET_LEN", 160, 40, 300)
        self.max_snippet_chats = _config_int(env, "TG_MAX_SNIPPET_CHATS", 20, 0, 150)
        self.max_items = _config_int(env, "TG_MAX_ITEMS", 100, 10, 200)
        self.chat_delay = float(_config_int(env, "TG_CHAT_DELAY_MS", 500, 100, 5000)) / 1000.0
        self.ring_size = _config_int(env, "TG_RING_SIZE", 200, 20, 1000)
        self.backfill = _config_int(env, "TG_VANGUARD_BACKFILL", 20, 0, 100)
        self.jsonl_max_bytes = _config_int(env, "TG_JSONL_MAX_BYTES", 5_000_000, 100_000, 50_000_000)
        self.mem_cap_mb = _config_int(env, "TG_MEM_CAP_MB", DEFAULT_MEM_CAP_MB, 100, 2000)
        self.max_post_chars = 8000

    def missing(self):
        m = []
        if not self.api_id.isdigit():
            m.append("TG_API_ID")
        if not self.api_hash:
            m.append("TG_API_HASH")
        if not self.string_session:
            m.append("TG_STRING_SESSION")
        return m


# ---------------------------------------------------------------- text helpers
def norm_title(s):
    """Lower-case, turn emoji/symbols/punctuation into spaces, collapse whitespace. 'Vanguard 🦅 Crypto Club!' -> 'vanguard crypto club'"""
    s = unicodedata.normalize("NFKC", s or "")
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat[0] in ("L", "N"):
            out.append(ch.casefold())
        elif cat[0] in ("Z", "P", "S") or ch in "\n\t":
            out.append(" ")      # spaces, punctuation and symbols/emoji act as word separators
        # everything else (M* variation selectors, Cf like ZWJ) is dropped
    return " ".join("".join(out).split())


def matches_watch(title, watch):
    w = norm_title(watch)
    return bool(w) and w in norm_title(title)


def snippet(text, n=160):
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def sender_name(msg, fallback="?"):
    s = getattr(msg, "sender", None)
    if s is None:
        return fallback
    if getattr(s, "title", None):
        return s.title
    name = " ".join(p for p in (getattr(s, "first_name", None), getattr(s, "last_name", None)) if p)
    return name or getattr(s, "username", None) or fallback


def classify(d):
    """'private' | 'group' | 'watch_candidate' (broadcast channel) | None (excluded)."""
    e = d.entity
    if d.is_user:
        if getattr(e, "bot", False) or getattr(e, "deleted", False):
            return None
        if getattr(e, "is_self", False) or getattr(e, "id", None) == TELEGRAM_SERVICE_ID:
            return None
        return "private"
    if getattr(e, "broadcast", False):
        return "watch_candidate"
    if d.is_group:
        return "group"
    return None


def is_muted(d, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        mu = d.dialog.notify_settings.mute_until
    except AttributeError:
        return False
    if not mu:
        return False
    if isinstance(mu, datetime):
        if mu.tzinfo is None:
            mu = mu.replace(tzinfo=timezone.utc)
        return mu > now
    try:
        return int(mu) > int(now.timestamp())
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------- urgent / alerts
def load_urgent(raw):
    try:
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        log.warning("TG_URGENT_JSON is not valid JSON; ignoring")
        data = {}
    out = {}
    for k in ("chat_names", "usernames", "keywords"):
        v = data.get(k, [])
        out[k] = [str(x).lower() for x in v][:100] if isinstance(v, list) else []
    return out


def _kw_hits(text, kws, flags=re.I):
    return [kw for kw in kws if re.search(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", text, flags)]


def is_priority(item, urgent):
    name = item["name"].lower()
    if any(n and n in name for n in urgent["chat_names"]):
        return True
    uname = (item.get("username") or "").lower()
    if uname and uname in [u.lstrip("@") for u in urgent["usernames"]]:
        return True
    text = " ".join(s["text"] for s in item["snippets"])
    return bool(_kw_hits(text, urgent["keywords"]))


def alert_hits(item):
    """Default heuristic: DMs, or group chats with an unread @mention, whose name/snippets contain alert words."""
    if item["type"] == "group" and item["mentions"] == 0:
        return []
    text = " ".join(s["text"] for s in item["snippets"]) + " " + item["name"]
    return _kw_hits(text, ALERT_KEYWORDS_CI) + _kw_hits(text, ALERT_KEYWORDS_CS, 0)


# ---------------------------------------------------------------- summary collection
async def collect_summary(client, cfg, sleep=asyncio.sleep, should_abort=None):
    """Iterate dialogs read-only. Returns (items, stats, watch_entity). Stops gracefully on FloodWait.
    should_abort: optional callable returning a reason string ("busy", "memory") to stop the scan early
    (checked before every dialog); the reason is returned in stats["aborted"] and the partial result must be discarded."""
    from telethon.errors import FloodWaitError
    items, watch_entity = [], None
    stats = {"scanned": 0, "flood_wait": None, "truncated": False, "snippet_chats": 0, "watch_channel_found": False,
             "aborted": None}
    try:
        async for d in client.iter_dialogs(limit=cfg.max_dialogs):
            if should_abort is not None:
                why = should_abort()
                if why:
                    stats["aborted"] = why
                    break
            stats["scanned"] += 1
            kind = classify(d)
            if kind is None:
                continue
            if kind == "watch_candidate":
                if matches_watch(d.name or "", cfg.watch_channel) and watch_entity is None:
                    watch_entity = d.entity
                    stats["watch_channel_found"] = True
                    if d.unread_count:
                        items.append({"name": d.name or "(unnamed)", "type": "watch_channel", "unread": d.unread_count,
                                      "mentions": 0, "muted": is_muted(d), "archived": bool(getattr(d, "archived", False)),
                                      "username": None, "snippets": []})
                continue
            if not d.unread_count:
                continue
            snippets = []
            if cfg.max_snippets and stats["snippet_chats"] < cfg.max_snippet_chats:
                stats["snippet_chats"] += 1
                try:
                    read_max = getattr(d.dialog, "read_inbox_max_id", 0) or 0
                    async for m in client.iter_messages(d.entity, limit=max(cfg.max_snippets * 3, 5), min_id=read_max):
                        if getattr(m, "out", False):
                            continue
                        txt = getattr(m, "message", "") or ("[media]" if getattr(m, "media", None) else "")
                        snippets.append({"from": snippet(sender_name(m, d.name), 60), "text": snippet(txt, cfg.snippet_len)})
                        if len(snippets) >= cfg.max_snippets:
                            break
                    snippets.reverse()
                except FloodWaitError:
                    raise
                except Exception as ex:   # one bad chat must not kill the run; never log content
                    snippets = [{"from": "?", "text": "[could not read messages: %s]" % type(ex).__name__}]
                await sleep(cfg.chat_delay)
            e = d.entity
            items.append({"name": snippet(d.name or "(unnamed)", 80), "type": kind, "unread": d.unread_count,
                          "mentions": getattr(d, "unread_mentions_count", 0) or 0, "muted": is_muted(d),
                          "archived": bool(getattr(d, "archived", False)),
                          "username": getattr(e, "username", None), "snippets": snippets})
    except FloodWaitError as fw:
        stats["flood_wait"] = fw.seconds
    stats["truncated"] = stats["scanned"] >= cfg.max_dialogs and not stats["aborted"]
    return items, stats, watch_entity


def build_summary(items, stats, urgent, cfg, now=None):
    now = now or datetime.now(timezone.utc)
    for it in items:
        if it["type"] == "watch_channel":
            it["priority"], it["alert_hits"] = False, []
        else:
            it["priority"], it["alert_hits"] = is_priority(it, urgent), alert_hits(it)
    items.sort(key=lambda i: (not i["priority"], not i["alert_hits"], -i["mentions"], -i["unread"]))
    return {"generated_at": now.isoformat(timespec="seconds"),
            "generated_at_sgt": now.astimezone(SGT).strftime("%Y-%m-%d %H:%M SGT"),
            "totals": {"chats_with_unread": len(items), "unread_messages": sum(i["unread"] for i in items),
                       "priority": sum(i["priority"] for i in items),
                       "alert_candidates": sum(bool(i["alert_hits"]) for i in items)},
            "stats": stats, "items": items[: cfg.max_items], "items_truncated": len(items) > cfg.max_items}


# ---------------------------------------------------------------- Vanguard store
class VanguardStore:
    """Append-only JSONL (if a writable data dir exists) + bounded in-memory ring. Dedupe by message id."""

    def __init__(self, data_dir, ring_size=200, max_bytes=5_000_000):
        self.lock = threading.Lock()
        self.ring = collections.deque(maxlen=ring_size)
        self.seen = set()
        self.last_id = 0
        self.max_bytes = max_bytes
        self.dir = None
        self.persistent = False
        if data_dir:
            try:
                os.makedirs(data_dir, mode=0o700, exist_ok=True)
                probe = os.path.join(data_dir, ".probe")
                with open(probe, "w") as f:
                    f.write("x")
                os.remove(probe)
                self.dir, self.persistent = data_dir, True
            except OSError as ex:
                log.warning("TG_DATA_DIR not writable (%s); running with in-memory ring only", type(ex).__name__)
        self.jsonl = os.path.join(self.dir, "vanguard.jsonl") if self.dir else None
        self.state = os.path.join(self.dir, "state.json") if self.dir else None
        self._load()

    def _load(self):
        if not self.persistent:
            return
        try:
            with open(self.state) as f:
                self.last_id = int(json.load(f).get("vanguard_last_id", 0))
        except (OSError, ValueError, AttributeError):
            pass
        try:
            for line in self._tail_lines(self.ring.maxlen):
                try:
                    p = json.loads(line)
                    self.ring.append(p)
                    self.seen.add(p["id"])
                    self.last_id = max(self.last_id, p["id"])
                except (ValueError, KeyError, TypeError):
                    continue
        except OSError:
            pass

    def _tail_lines(self, n):
        dq = collections.deque(maxlen=n)
        with open(self.jsonl, "rb") as f:
            for line in f:
                dq.append(line.decode("utf-8", "replace"))
        return list(dq)

    def _write_private(self, path, text, append=False):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)

    def add(self, post):
        """Returns True if newly stored, False if duplicate/old."""
        mid = int(post["id"])
        with self.lock:
            if mid in self.seen or mid <= self.last_id:
                return False
            if self.persistent:
                try:
                    if os.path.exists(self.jsonl) and os.path.getsize(self.jsonl) > self.max_bytes:
                        os.replace(self.jsonl, self.jsonl + ".1")
                    self._write_private(self.jsonl, json.dumps(post, ensure_ascii=False) + "\n", append=True)
                except OSError as ex:
                    log.warning("could not append vanguard.jsonl (%s); ring only", type(ex).__name__)
            if len(self.ring) == self.ring.maxlen:
                self.seen.discard(self.ring[0]["id"])
            self.ring.append(post)
            self.seen.add(mid)
            self.last_id = max(self.last_id, mid)
            if self.persistent:
                try:
                    tmp = self.state + ".tmp"
                    self._write_private(tmp, json.dumps({"vanguard_last_id": self.last_id}))
                    os.replace(tmp, self.state)
                except OSError:
                    pass
            return True

    def set_baseline(self, mid):
        with self.lock:
            self.last_id = max(self.last_id, int(mid))
            if self.persistent:
                try:
                    tmp = self.state + ".tmp"
                    self._write_private(tmp, json.dumps({"vanguard_last_id": self.last_id}))
                    os.replace(tmp, self.state)
                except OSError:
                    pass

    def read_since(self, since_id=0, limit=50):
        limit = max(1, min(int(limit), 200))
        out, truncated = [], False
        with self.lock:
            ring_min = self.ring[0]["id"] if self.ring else None
            use_file = self.persistent and os.path.exists(self.jsonl) and (ring_min is None or since_id < ring_min)
            if use_file:
                try:
                    with open(self.jsonl, "rb") as f:
                        for line in f:
                            try:
                                p = json.loads(line)
                            except ValueError:
                                continue
                            if p.get("id", 0) > since_id:
                                if len(out) >= limit:
                                    truncated = True
                                    break
                                out.append(p)
                    return out, truncated, self.last_id
                except OSError:
                    out = []
            for p in self.ring:
                if p["id"] > since_id:
                    if len(out) >= limit:
                        truncated = True
                        break
                    out.append(p)
            # the ring (ephemeral mode) may no longer hold everything after since_id
            gap = (ring_min is not None and since_id < ring_min - 1 and not self.persistent)
            return out, (truncated or gap), self.last_id


def make_post(msg, backfill=False, max_chars=8000):
    raw = getattr(msg, "message", "") or ""
    d = getattr(msg, "date", None)
    if isinstance(d, datetime) and d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    ed = getattr(msg, "edit_date", None)
    media = getattr(msg, "media", None)
    post = {"id": int(msg.id),
            "date_utc": d.astimezone(timezone.utc).isoformat(timespec="seconds") if d else None,
            "date_sgt": d.astimezone(SGT).strftime("%Y-%m-%d %H:%M:%S SGT") if d else None,
            "fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "edit_date_utc": ed.astimezone(timezone.utc).isoformat(timespec="seconds") if isinstance(ed, datetime) else None,
            "text": raw[:max_chars], "text_truncated": len(raw) > max_chars,
            "has_media": bool(media), "media_type": type(media).__name__ if media else None,
            "parsed": parse_signal(raw), "backfill": bool(backfill)}
    return post


async def poll_vanguard(client, entity, store, cfg):
    """Fetch posts newer than store.last_id (oldest first). Returns number of new posts stored."""
    new = 0
    if store.last_id:
        msgs = []
        async for m in client.iter_messages(entity, min_id=store.last_id, limit=100, reverse=True):
            msgs.append(m)
        backfill = False
    else:   # first ever run (or ephemeral restart): take the latest N, flagged as backfill
        msgs = []
        async for m in client.iter_messages(entity, limit=max(1, cfg.backfill)):
            msgs.append(m)
        msgs.reverse()
        backfill = True
        if not cfg.backfill:       # baseline only: remember the newest id, store nothing
            if msgs:
                store.set_baseline(int(msgs[-1].id))
            return 0
    for m in msgs:
        if not getattr(m, "message", None) and not getattr(m, "media", None):
            continue   # service messages (pins, joins...)
        if store.add(make_post(m, backfill=backfill, max_chars=cfg.max_post_chars)):
            new += 1
    return new


# ---------------------------------------------------------------- reader (thread + supervisor)
def rss_mb():
    """Resident memory of THIS Python process only (misses the panel's Node child). Last-resort fallback."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return None


def _read_int(path):
    with open(path) as f:
        return int(f.read().strip())


def _stat_value(path, key):
    try:
        with open(path) as f:
            for line in f:
                k, _, v = line.partition(" ")
                if k == key:
                    return int(v.strip())
    except (OSError, ValueError):
        pass
    return 0


def cgroup_mem_mb():
    """Container memory in MB as (mb, source). cgroup v2 memory.current, then cgroup v1 memory.usage_in_bytes.
    Reclaimable page cache (inactive_file, as `docker stats` does) is subtracted when memory.stat is readable, so
    log/file cache cannot trigger a false shutdown. Returns (None, None) when no cgroup file is readable."""
    for usage, stat, key, src in ((CGROUP_V2_USAGE, CGROUP_V2_STAT, "inactive_file", "cgroup_v2"),
                                  (CGROUP_V1_USAGE, CGROUP_V1_STAT, "total_inactive_file", "cgroup_v1")):
        try:
            b = _read_int(usage)
        except (OSError, ValueError):
            continue
        b = max(0, b - _stat_value(stat, key))
        return b / 1048576.0, src
    return None, None


_RSS_WARNED = [False]


def memory_mb():
    """(mb, source): cgroup v2 -> cgroup v1 -> process RSS (with a one-time logged warning) -> (None, 'unavailable')."""
    mb, src = cgroup_mem_mb()
    if mb is not None:
        return mb, src
    r = rss_mb()
    if r is not None:
        if not _RSS_WARNED[0]:
            _RSS_WARNED[0] = True
            log.warning("no readable cgroup memory file; falling back to process RSS (misses child processes such as the panel's Node)")
        return r, "rss"
    return None, "unavailable"


class Reader:
    def __init__(self, cfg, client_factory=None, sleep_unit=1.0, busy_fn=None):
        self.cfg = cfg
        self.busy_fn = busy_fn          # optional: returns True while a panel job is running (see panel_hook.patch)
        self._busy_err_logged = 0.0
        self.urgent = load_urgent(cfg.urgent_json)
        self.store = VanguardStore(cfg.data_dir, cfg.ring_size, cfg.jsonl_max_bytes)
        self.client_factory = client_factory or self._default_client
        self.sleep_unit = sleep_unit
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.summary = None
        self.thread = None
        self.watch_entity = None
        self.status = {"state": "init", "started_at": None, "last_summary_at": None, "last_vanguard_poll_at": None,
                       "restarts": 0, "last_error": None, "flood_wait_until": None, "vanguard_new_total": 0,
                       "watch_channel_found": False, "summary_skipped_busy": 0, "last_skip_busy_at": None,
                       "mem_mb": None, "mem_source": None, "mem_cap_mb": cfg.mem_cap_mb}
        self.limiter = None
        self.backoff_cap = 900.0
        self.backoff_base = 10.0

    # --- status helpers
    def set_status(self, **kw):
        with self.lock:
            self.status.update(kw)

    def get_status(self):
        with self.lock:
            s = dict(self.status)
        s.update({"persistent": self.store.persistent, "vanguard_last_id": self.store.last_id,
                  "rss_mb": round(rss_mb() or 0, 1) or None, "enabled": True,
                  "start_delay_s": self.cfg.start_delay, "max_dialogs": self.cfg.max_dialogs,
                  "thread_alive": bool(self.thread and self.thread.is_alive())})
        return s

    def get_summary(self):
        with self.lock:
            return self.summary

    # --- lifecycle
    def start(self):
        self.thread = threading.Thread(target=self._thread_main, name="tg-reader", daemon=True)
        self.thread.start()
        return self.thread

    def stop(self, reason="stopped"):
        self.stop_event.set()
        self.set_status(state=reason)

    def is_busy(self):
        """True while the panel reports a running job. Never raises: with no busy_fn, or if it fails, the answer is
        "not busy" (the failure is logged, at most once per 10 minutes, class name only)."""
        fn = self.busy_fn
        if fn is None:
            return False
        try:
            return bool(fn())
        except BaseException as ex:   # noqa: BLE001 - a broken hook must never break the reader or the panel
            now = time.monotonic()
            if now - self._busy_err_logged > 600 or self._busy_err_logged == 0.0:
                self._busy_err_logged = now
                log.warning("busy_fn failed (%s); treating the panel as NOT busy", type(ex).__name__)
            return False

    def check_memory(self):
        """True if the container is over the cap. Updates status. Cap exceeded => caller shuts the reader down."""
        mb, src = memory_mb()
        with self.lock:
            self.status["mem_mb"] = round(mb, 1) if mb is not None else None
            self.status["mem_source"] = src
        return mb is not None and mb > self.cfg.mem_cap_mb, mb, src

    def _abort_reason(self):
        if self.check_memory()[0]:
            return "memory"
        if self.is_busy():
            return "busy"
        return None

    def _default_client(self):
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        class ReadOnlyTelegramClient(TelegramClient):
            async def __call__(self, request, *a, **k):
                if not request_allowed(request):
                    raise WriteBlocked("MTProto request %s blocked (read-only)" % type(request).__name__)
                return await super().__call__(request, *a, **k)

        c = ReadOnlyTelegramClient(StringSession(self.cfg.string_session), int(self.cfg.api_id), self.cfg.api_hash,
                                   receive_updates=False, connection_retries=2, retry_delay=5, request_retries=2,
                                   auto_reconnect=True, flood_sleep_threshold=30,
                                   device_model="Desktop", app_version="1.0")
        return c

    def _thread_main(self):
        """Outermost guard: nothing may escape this function."""
        try:
            self.set_status(state="starting", started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            if self.stop_event.wait(self.cfg.start_delay):
                return
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            attempt = 0
            while not self.stop_event.is_set():
                t0 = time.monotonic()
                try:
                    loop.run_until_complete(self._session())
                    break
                except BaseException as ex:   # noqa: BLE001 - deliberate: isolate trading code from ANY failure
                    name = type(ex).__name__
                    self.set_status(last_error=(str(ex)[:120] if isinstance(ex, WriteBlocked) else name), state="error")
                    if name in FATAL_ERRORS:
                        log.error("fatal auth error %s; reader stopped (needs a new session string)", name)
                        self.set_status(state="dead:" + name)
                        return
                    if time.monotonic() - t0 > 600:
                        attempt = 0           # it ran for a while: reset the backoff
                    attempt += 1
                    delay = min(self.backoff_cap, self.backoff_base * (2 ** (attempt - 1)))
                    delay = delay * (0.8 + 0.4 * random.random())
                    with self.lock:
                        self.status["restarts"] += 1
                    log.warning("session ended with %s; restart #%d in %.0fs", name, attempt, delay)
                    if self.stop_event.wait(delay * self.sleep_unit):
                        break
            try:
                loop.close()
            except Exception:
                pass
        except BaseException as ex:   # noqa: BLE001
            try:
                log.error("reader thread crashed: %s", type(ex).__name__)
                self.set_status(state="crashed", last_error=type(ex).__name__)
            except Exception:
                pass

    async def _session(self):
        from telethon.errors import FloodWaitError
        raw = self.client_factory()
        client = ReadOnlyClient(raw)
        cfg = self.cfg
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise FatalAuth("session string is not authorized")
            self.set_status(state="running", last_error=None)
            next_summary, next_van = 0.0, 0.0
            while not self.stop_event.is_set():
                now = time.monotonic()
                over, mem, src = self.check_memory()
                if over:
                    log.error("container memory %.0f MB (%s) above cap %d MB; shutting reader down to protect the service",
                              mem, src, cfg.mem_cap_mb)
                    self.stop("stopped:memory")
                    return
                try:
                    if now >= next_summary and self.is_busy():
                        # a panel job (maker orders re-posted every few seconds) is running: no dialog scan, no Telegram traffic
                        with self.lock:
                            self.status["summary_skipped_busy"] += 1
                            self.status["last_skip_busy_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        next_summary = time.monotonic() + BUSY_RETRY_SECONDS
                    elif now >= next_summary:
                        items, stats, ent = await collect_summary(client, cfg, should_abort=self._abort_reason)
                        if stats.get("aborted") == "memory":
                            log.error("container memory above cap during scan; shutting reader down to protect the service")
                            self.stop("stopped:memory")
                            return
                        if stats.get("aborted") == "busy":     # a job started mid-scan: drop the partial result, retry later
                            with self.lock:
                                self.status["summary_skipped_busy"] += 1
                                self.status["last_skip_busy_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                            next_summary = time.monotonic() + BUSY_RETRY_SECONDS
                            raise _Skip()
                        if ent is not None:
                            self.watch_entity = ent
                        rep = build_summary(items, stats, self.urgent, cfg)
                        with self.lock:
                            self.summary = rep
                            self.status.update(last_summary_at=rep["generated_at"], watch_channel_found=self.watch_entity is not None)
                        wait = cfg.summary_interval
                        if stats.get("flood_wait"):
                            wait = min(cfg.summary_interval, max(300, stats["flood_wait"] + 5))
                        next_summary = time.monotonic() + wait
                        gc.collect()
                    if time.monotonic() >= next_van and self.watch_entity is not None:
                        n = await poll_vanguard(client, self.watch_entity, self.store, cfg)
                        with self.lock:
                            self.status["vanguard_new_total"] += n
                            self.status["last_vanguard_poll_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        next_van = time.monotonic() + cfg.vanguard_interval
                except _Skip:
                    pass
                except FloodWaitError as fw:
                    pause = min(3600, fw.seconds + 5)
                    self.set_status(flood_wait_until=(datetime.now(timezone.utc) + timedelta(seconds=pause)).isoformat(timespec="seconds"))
                    next_summary = max(next_summary, time.monotonic() + pause)
                    next_van = max(next_van, time.monotonic() + pause)
                await asyncio.sleep(2.0 * self.sleep_unit)
        finally:
            try:
                await client.disconnect()
            except BaseException:   # noqa: BLE001
                pass


# ---------------------------------------------------------------- hook into the panel's HTTP handler
_READER = None


def get_reader():
    return _READER


def install(handler_cls, env=None, busy_fn=None):
    """The single hook. Safe to call with TG disabled (returns None and changes nothing).
    busy_fn: optional zero-arg callable, True while a panel job is running; the reader skips its dialog scan then."""
    global _READER
    cfg = Config(env)
    if not cfg.enabled:
        return None
    miss = cfg.missing()
    if miss:
        log.warning("TG_ENABLED is on but %s missing; reader not started", ",".join(miss))
        return None
    try:
        from tg_http import wrap_handler
    except ImportError:  # pragma: no cover
        from .tg_http import wrap_handler
    reader = Reader(cfg, busy_fn=busy_fn)
    _READER = reader
    wrap_handler(handler_cls, reader, cfg)
    reader.start()
    log.info("reader installed (delay %ds, mem cap %d MB, busy_fn=%s, summary every %ds, watch=%s, persistent=%s)",
             cfg.start_delay, cfg.mem_cap_mb, "set" if busy_fn else "none", cfg.summary_interval, "set" if cfg.watch_channel else "none", reader.store.persistent)
    return reader

"""Persistent state: one JSON file (state.json), atomic writes, inter-process file lock.

Same pattern as the sticker bot: every read-modify-write happens inside
`transaction()`, which takes an exclusive flock, loads the file, yields the
dict, and atomically replaces the file (tmp + fsync + os.replace).
"""
import os, json, time, fcntl, threading, copy, logging
from contextlib import contextmanager

log = logging.getLogger("store")
BASE = os.path.dirname(os.path.abspath(__file__))
# One state file per platform (user ids, tickets and the admin binding are platform specific):
#   telegram → state.json   bale → state_bale.json   rubika → state_rubika.json
_PLATFORM = (os.environ.get("SHOP_PLATFORM") or "telegram").strip().lower()
PATH = os.path.join(BASE, "state.json" if _PLATFORM == "telegram" else f"state_{_PLATFORM}.json")
# The shop-API connection (URL, key, paths, field mapping) is shared by all platforms: configure it once
# from any admin panel.  Separate secret file, mode 600, own lock.
API_PATH = os.path.join(BASE, "api_config.json")

_tl = threading.RLock()
_cur = None


def set_path(p):
    """Tests: point state + shared API config at another location."""
    global PATH, API_PATH
    PATH = p
    API_PATH = os.path.join(os.path.dirname(p), "api_config.json")


def _blank():
    return {
        "_v": 1,
        "admin_id": None,          # numeric Telegram id; auto-bound when @example_owner first writes
        "users": {},               # "<uid>": {...profile...}
        "tickets": {},             # "<id>": {...}
        "next_ticket": 1,
        "admin_map": {},           # admin-chat message_id -> ticket id (for native "reply" routing)
        "texts": {},               # admin-editable: faq_fa, faq_en, about_fa, about_en
        "pending_broadcast": None,
        "broadcasts": 0,
        "notified": {},            # order_number -> last status we notified about
        "admins": [],              # extra admins (numeric ids); owner = admin_id
        "orders_log": [],          # orders users looked up via the bot: {n, uid, ts, s, sr}
        "order_subs": {},          # order_number -> [uid, ...] users who verified/own that order
        "idmap": {"c2i": {}, "i2c": {}, "next": 1, "info": {}},   # Rubika: string chat ids ↔ stable ints
        "poll": {},                # Rubika: persisted getUpdates offset_id
        "claim": {"hash": None, "fails": {}},   # non-Telegram platforms: one-time admin claim code (hash only)
        # legacy: the API connection used to live here; now in api_config.json (see api_config()).
        "api": {"enabled": True, "base_url": "", "api_key": "", "auth_style": "bearer",
                "auth_name": "", "paths": {}, "fields": {}},
    }


def _normalize(st):
    for k, v in _blank().items():
        st.setdefault(k, v)
    if not isinstance(st.get("api"), dict):
        st["api"] = _blank()["api"]
    for k, v in _blank()["api"].items():
        st["api"].setdefault(k, v)
    return st


def _load():
    try:
        with open(PATH) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return _blank()
    except Exception as e:
        bak = f"{PATH}.corrupt-{int(time.time())}"
        try:
            os.replace(PATH, bak)
        except OSError:
            pass
        log.error("state file unreadable (%s); moved to %s", type(e).__name__, bak)
        return _blank()
    if not isinstance(raw, dict):
        return _blank()
    return _normalize(raw)


def _save(st):
    tmp = PATH + ".tmp"
    # created with mode 600 from the start (state holds the API key)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, PATH)


@contextmanager
def transaction(write=True):
    """Exclusive read-modify-write. Nested use reuses the outer state."""
    global _cur
    with _tl:
        if _cur is not None:
            yield _cur
            return
        fd = os.fdopen(os.open(PATH + ".lock", os.O_RDWR | os.O_CREAT, 0o600), "a+")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            st = _load()
            _cur = st
            try:
                yield st
                if write:
                    _save(st)
            finally:
                _cur = None
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                fd.close()


def snapshot():
    with transaction(write=False) as st:
        return copy.deepcopy(st)


def get_user(uid):
    with transaction(write=False) as st:
        return copy.deepcopy(st["users"].get(str(uid), {}))


def update_user(uid, **kw):
    """Set keys (value None deletes the key). Returns the updated user dict."""
    with transaction() as st:
        u = st["users"].setdefault(str(uid), {})
        for k, v in kw.items():
            if v is None:
                u.pop(k, None)
            else:
                u[k] = v
        return copy.deepcopy(u)


def admin_id():
    with transaction(write=False) as st:
        return st.get("admin_id")


def get_text(key):
    with transaction(write=False) as st:
        return st["texts"].get(key)


def set_text(key, value):
    """value None/'' -> reset to the built-in default."""
    with transaction() as st:
        if value:
            st["texts"][key] = value
        else:
            st["texts"].pop(key, None)


_API_BLANK = {"enabled": True, "base_url": "", "api_key": "", "auth_style": "bearer",
              "auth_name": "", "paths": {}, "fields": {}}


@contextmanager
def _api_txn(write=True):
    """Exclusive read-modify-write of the shared api_config.json (own flock, atomic, mode 600)."""
    with _tl:
        fd = os.fdopen(os.open(API_PATH + ".lock", os.O_RDWR | os.O_CREAT, 0o600), "a+")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                with open(API_PATH) as f:
                    a = json.load(f)
                if not isinstance(a, dict):
                    a = {}
            except FileNotFoundError:
                a = None
            except Exception:
                a = {}
            if a is None:       # first use: migrate whatever this platform's state.json already had
                try:
                    with open(PATH) as f:
                        legacy = json.load(f).get("api")
                except Exception:
                    legacy = None
                a = legacy if isinstance(legacy, dict) else {}
                write = True
            for k, v in _API_BLANK.items():
                a.setdefault(k, copy.deepcopy(v))
            yield a
            if write:
                tmp = API_PATH + ".tmp"
                wfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(wfd, "w") as f:
                    json.dump(a, f, ensure_ascii=False, indent=1)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, API_PATH)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                fd.close()


def api_config():
    with _api_txn(write=False) as a:
        return copy.deepcopy(a)


def update_api(**kw):
    """Set keys of the api section (value None deletes -> default). Returns a copy."""
    with _api_txn() as a:
        for k, v in kw.items():
            if v is None:
                a.pop(k, None)
            else:
                a[k] = v
        for k, v in _API_BLANK.items():
            a.setdefault(k, copy.deepcopy(v))
        return copy.deepcopy(a)


def set_api_map(section, key, value):
    """section: 'paths' | 'fields'. value None/'' -> reset that entry to default."""
    with _api_txn() as a:
        m = a.setdefault(section, {})
        if value:
            m[key] = value
        else:
            m.pop(key, None)

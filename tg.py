"""Messaging facade used by all business logic. Delegates to the active platform Transport
(transport.py): Telegram (default), Bale, Rubika – selected by env SHOP_PLATFORM.

Business code calls tg.send / tg.show / tg.copy_message … and never touches a platform API directly."""
import os, sys, logging

import transport
from transport import ApiError, add_secret, redact, pairs      # re-exported

PLATFORM = (os.environ.get("SHOP_PLATFORM") or "telegram").strip().lower()
if PLATFORM not in transport.TRANSPORTS:
    print(f"unknown SHOP_PLATFORM={PLATFORM!r}", file=sys.stderr)
    sys.exit(1)
T = transport.TRANSPORTS[PLATFORM]()
TOKEN = T.token
TOKEN_ENV = T.token_env
LABEL = T.label
log = logging.getLogger("tg")


class RedactFormatter(logging.Formatter):
    """Replaces every bot token / API key anywhere in the final log line (message AND tracebacks)."""
    def format(self, record):
        return redact(super().format(record))


def setup_logging(logfile=None):
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    h = logging.StreamHandler(open(logfile, "a", buffering=1) if logfile else sys.stderr)
    h.setFormatter(RedactFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(h)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def safe(e):
    return redact(e)


# ---------- markup helpers (plain dicts; each transport encodes/translates them) ----------
def kb(rows):
    return {"inline_keyboard": rows}


def btn(text, data=None, url=None):
    b = {"text": text}
    if url:
        b["url"] = url
    else:
        if len(data.encode()) > 64:
            log.warning("callback_data too long (%d bytes): %r", len(data.encode()), data[:20])
            data = data.encode()[:64].decode(errors="ignore")
        b["callback_data"] = data
    return b


def contact_keyboard(share_label, menu_label):
    return T.contact_keyboard(share_label, menu_label)


# ---------- thin delegates ----------
def call(method, data=None, files=None, timeout=60):
    return T.call(method, data, files, timeout)


def get_me():
    return T.get_me()


def prepare_polling():
    return T.prepare_polling()


def get_updates(state):
    return T.get_updates(state)


def send(chat_id, text, markup=None, html=True, raise_errors=False):
    return T.send(chat_id, text, markup, html, raise_errors)


def show(chat_id, msg_id, text, markup=None):
    return T.show(chat_id, msg_id, text, markup)


def send_photo(chat_id, photo_url, caption, markup=None):
    return T.send_photo(chat_id, photo_url, caption, markup)


def copy_message(chat_id, from_chat_id, message_id, markup=None, raise_errors=False, msg=None):
    """Copy a message (without a forward header). `msg` = original message dict, used by platforms
    that have no copyMessage (Rubika) to re-send text."""
    return T.copy_message(chat_id, from_chat_id, message_id, markup, raise_errors, msg)


def delete_message(chat_id, message_id):
    return T.delete_message(chat_id, message_id)


def clear_markup(chat_id, message_id):
    return T.clear_markup(chat_id, message_id)


def answer_cb(cb_id, text=None):
    return T.answer_cb(cb_id, text)


def remove_keyboard(chat_id, text="🌸"):
    return T.remove_keyboard(chat_id, text)


def is_own_contact(contact, uid):
    return T.is_own_contact(contact, uid)


def is_dead_chat(e):
    return T.is_dead_chat(e)

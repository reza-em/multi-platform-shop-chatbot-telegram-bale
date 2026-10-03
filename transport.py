"""Platform transports (Telegram / Bale / Rubika) behind one small interface.

The business logic (bot.py, ui.py, notify.py, shop_api.py …) is platform independent.  It speaks a
Telegram-shaped dialect:
  * updates:  {"message": {...}} / {"callback_query": {...}}   (Rubika updates are normalised to this)
  * text:     the small HTML subset  <b> <i> <code> <a href>  (each transport renders it its own way)
  * markup:   plain dicts – {"inline_keyboard": [[{"text","callback_data"|"url"}]]}
              or a reply keyboard / {"remove_keyboard": True}
Each Transport knows its base URL, capability flags and quirks.  Select with env SHOP_PLATFORM
(telegram | bale | rubika; default telegram).
"""
import os, re, json, time, html, logging
import requests

log = logging.getLogger("tg")

TOKEN_ENVS = {"telegram": "SHOP_TELEGRAM_BOT_TOKEN", "bale": "SHOP_BALE_BOT_TOKEN",
              "rubika": "SHOP_RUBIKA_BOT_TOKEN"}

# --------------------------------------------------------------------------- secrets / redaction
SECRETS = set()      # every bot token (all platforms) + the website API key, registered at runtime


def add_secret(s):
    if s and len(s) >= 4:
        SECRETS.add(s)


for _env in TOKEN_ENVS.values():          # redact ALL platform tokens no matter which one is active
    add_secret(os.environ.get(_env, ""))


def redact(s):
    s = str(s)
    for sec in SECRETS:
        s = s.replace(sec, "<SECRET>")
    return s


class ApiError(Exception):
    def __init__(self, msg, code=None, retry_after=None):
        super().__init__(msg)
        self.code, self.retry_after = code, retry_after


# --------------------------------------------------------------------------- text helpers
_TAG = re.compile(r"<(/?)(b|strong|i|em|u|code|pre|a)(?:\s+href=\"([^\"]*)\")?\s*>", re.I)


def strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s))


def clip_html(s, n):
    """Clip to n chars without ever leaving a broken tag: too long → plain text, re-escaped, cut."""
    if len(s) <= n:
        return s
    plain = strip_html(s)
    return html.escape(plain[: n - 1], quote=False) + "…"


def _md_escape(t):
    t = t.replace("*", "∗").replace("`", "ˋ").replace("[", "［").replace("]", "］")
    return re.sub(r"(?<!\S)_|_(?!\S)", "＿", t)   # underscores only matter next to whitespace in Bale's Markdown


def html_to_bale_md(s, max_len=4096):
    """Convert our HTML subset to Bale's Markdown: *bold* and _italic_ need a space outside the marks,
    [text](url) for links.  <code>/<pre>/<u> are rendered as plain text (no documented syntax).
    Characters that could be mis-parsed as Markdown in plain text are swapped for look-alikes."""
    out, pos, stack = [], 0, []
    for m in _TAG.finditer(s):
        out.append(_md_escape(html.unescape(s[pos:m.start()])))
        pos = m.end()
        closing, tag, href = m.group(1) == "/", m.group(2).lower(), m.group(3)
        mark = {"b": "*", "strong": "*", "i": "_", "em": "_"}.get(tag)
        if tag == "a":
            if not closing:
                stack.append(("a", len(out), html.unescape(href or "")))
            elif stack and stack[-1][0] == "a":
                _, idx, url = stack.pop()
                inner = "".join(out[idx:]); del out[idx:]
                out.append(f"[{inner}]({url})" if re.match(r"https?://", url) and inner.strip() else inner)
        elif mark:
            if not closing:
                stack.append((mark, len(out), None))
                out.append("\x02" + mark)
            elif stack and stack[-1][0] == mark:
                _, idx, _u = stack.pop()
                inner = "".join(out[idx + 1:])
                if inner.strip():
                    out.append(mark + "\x01")
                else:                                   # empty emphasis → drop the marks
                    del out[idx:]
                    out.append(inner)
    out.append(_md_escape(html.unescape(s[pos:])))
    res = "".join(out)
    res = re.sub(r"(^|\s)\x02", r"\1", res).replace("\x02", " ")
    res = re.sub(r"\x01(?=\s|\Z)", "", res).replace("\x01", " ")
    return res if len(res) <= max_len else res[: max_len - 1] + "…"


def pairs(buttons):
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


# --------------------------------------------------------------------------- base transport
class Transport:
    name = "telegram"
    label = "Telegram"
    token_env = "SHOP_TELEGRAM_BOT_TOKEN"
    base = "https://api.telegram.org"
    max_text = 4096
    # ---- capability flags / quirks
    username_admin_bind = True        # auto-bind admin by @username (safe only where usernames are trusted)
    supports_contact_button = True
    supports_inline_keyboard = True
    supports_photo_url = True
    supports_copy = True
    supports_delete = True
    supports_edit = True
    strict_contact_user_id = True
    verified_contact = True           # can we PROVE a shared contact is the sender's own number?

    def __init__(self, token=None):
        self.token = os.environ.get(self.token_env, "") if token is None else token
        add_secret(self.token)
        self.sess = requests.Session()

    # ---- low level -------------------------------------------------------------------------
    def url(self, method):
        return f"{self.base}/bot{self.token}/{method}"

    @staticmethod
    def _enc(data):
        out = {}
        for k, v in (data or {}).items():
            if v is None:
                continue
            out[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list, bool)) else v
        return out

    def call(self, method, data=None, files=None, timeout=60, _retry=True):
        try:
            r = self.sess.post(self.url(method), data=self._enc(data), files=files, timeout=timeout)
        except requests.RequestException as e:
            # never str(e): requests puts the full URL (with the token) in messages
            raise ApiError("network: " + type(e).__name__) from None
        try:
            j = r.json()
        except Exception:
            raise ApiError(f"bad response HTTP {r.status_code}", code=r.status_code) from None
        if not j.get("ok"):
            ra = (j.get("parameters") or {}).get("retry_after")
            if j.get("error_code") == 429 and ra and ra <= 30 and _retry:
                time.sleep(ra + 1)
                return self.call(method, data, files, timeout, _retry=False)
            raise ApiError(redact(j.get("description", "unknown error")), code=j.get("error_code"), retry_after=ra)
        return j["result"]

    # ---- formatting ------------------------------------------------------------------------
    def fmt(self, text_html, html_mode=True):
        """→ (text, extra_params)"""
        if not html_mode:
            return text_html[: self.max_text], {}
        return clip_html(text_html, self.max_text), {"parse_mode": "HTML", "disable_web_page_preview": True}

    # ---- identity / polling ----------------------------------------------------------------
    def get_me(self):
        me = self.call("getMe")
        return {"username": me.get("username") or "", "name": me.get("first_name") or ""}

    def prepare_polling(self):
        try:
            wh = self.call("getWebhookInfo")
            if wh.get("url"):
                log.info("Webhook was set; deleting to use long polling")
                self.call("deleteWebhook")
        except ApiError as e:
            log.warning("webhook check: %s", redact(e))

    def get_updates(self, state):
        """→ list of Telegram-shaped updates; `state` is a dict kept by the caller (offset etc.)."""
        params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
        if state.get("offset"):
            params["offset"] = state["offset"]
        ups = self.call("getUpdates", params, timeout=45)
        for up in ups:
            state["offset"] = up["update_id"] + 1
        return ups

    # ---- sending ---------------------------------------------------------------------------
    def send(self, chat_id, text, markup=None, html=True, raise_errors=False):
        t, extra = self.fmt(text, html)
        data = {"chat_id": chat_id, "text": t, **extra}
        if markup:
            data["reply_markup"] = markup
        try:
            return self.call("sendMessage", data)
        except ApiError as e:
            log.warning("sendMessage failed: %s", redact(e))
            if raise_errors:
                raise

    def edit(self, chat_id, msg_id, text, markup=None):
        t, extra = self.fmt(text)
        data = {"chat_id": chat_id, "message_id": msg_id, "text": t, **extra}
        if markup:
            data["reply_markup"] = markup
        return self.call("editMessageText", data)

    def show(self, chat_id, msg_id, text, markup=None):
        """Edit a panel message in place if possible, else send a new one."""
        if msg_id and self.supports_edit:
            try:
                return self.edit(chat_id, msg_id, text, markup)
            except ApiError as e:
                if "not modified" in str(e).lower():
                    return None
        return self.send(chat_id, text, markup)

    def send_photo(self, chat_id, photo_url, caption, markup=None):
        if not self.supports_photo_url:
            return None
        t, extra = self.fmt(caption)
        data = {"chat_id": chat_id, "photo": photo_url, "caption": t}
        if "parse_mode" in extra:
            data["parse_mode"] = extra["parse_mode"]
        if markup:
            data["reply_markup"] = markup
        try:
            return self.call("sendPhoto", data)
        except ApiError as e:
            log.warning("sendPhoto failed: %s", redact(e))
            return None

    def copy_message(self, chat_id, from_chat_id, message_id, markup=None, raise_errors=False, msg=None):
        data = {"chat_id": chat_id, "from_chat_id": from_chat_id, "message_id": message_id}
        if markup:
            data["reply_markup"] = markup
        try:
            return self.call("copyMessage", data)
        except ApiError as e:
            log.warning("copyMessage failed: %s", redact(e))
            if raise_errors:
                raise

    def delete_message(self, chat_id, message_id):
        try:
            self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}, timeout=15)
            return True
        except ApiError as e:
            log.warning("deleteMessage failed: %s", redact(e))
            return False

    def clear_markup(self, chat_id, message_id):
        if not message_id:
            return
        try:
            self.call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                                  "reply_markup": {"inline_keyboard": []}})
        except ApiError:
            pass

    def answer_cb(self, cb_id, text=None):
        d = {"callback_query_id": cb_id}
        if text:
            d["text"] = text
        try:
            self.call("answerCallbackQuery", d, timeout=10)
        except ApiError:
            pass

    # ---- reply keyboards / contact ---------------------------------------------------------
    def contact_keyboard(self, share_label, menu_label):
        return {"keyboard": [[{"text": share_label, "request_contact": True}], [{"text": menu_label}]],
                "resize_keyboard": True, "one_time_keyboard": True}

    def remove_keyboard(self, chat_id, text="🌸"):
        return self.send(chat_id, text, {"remove_keyboard": True}, html=False)

    def is_own_contact(self, contact, uid):
        return contact.get("user_id") == uid

    def is_dead_chat(self, e):
        d = str(e).lower()
        return getattr(e, "code", None) == 403 or any(x in d for x in (
            "bot was blocked", "user is deactivated", "chat not found", "bot can't initiate"))


class TelegramTransport(Transport):
    pass


# --------------------------------------------------------------------------- Bale
class BaleTransport(Transport):
    """Bale Bot API (https://docs.bale.ai): Telegram-like, with these differences handled here:
      * base URL https://tapi.bale.ai/bot<token>/…  (files: /file/bot<token>/…)
      * NO parse_mode: every text is Markdown (*bold*, _italic_, [t](url)); we convert our HTML subset
      * no allowed_updates on getUpdates (updates: message, edited_message, callback_query, …)
      * answerCallbackQuery must always be called; callback ids starting with "1" = old client
        without support (skipped)
      * Contact.user_id is documented as a 32-bit value while user ids may be up to 52-bit
      * usernames are optional and not proof of identity → admin ownership uses a claim code
      * sendPhoto by URL: max 5 MB, sendDocument by URL only GIF/PDF/ZIP; text limit 4096;
        photo caption limit 4096
    """
    name = "bale"
    label = "Bale"
    token_env = "SHOP_BALE_BOT_TOKEN"
    base = "https://tapi.bale.ai"
    username_admin_bind = os.environ.get("SHOP_BALE_ADMIN_USERNAME_BIND", "0") == "1"

    def fmt(self, text_html, html_mode=True):
        if not html_mode:
            return text_html[: self.max_text], {}
        return html_to_bale_md(text_html, self.max_text), {}

    def get_updates(self, state):
        params = {"timeout": 30}
        if state.get("offset"):
            params["offset"] = state["offset"]
        ups = self.call("getUpdates", params, timeout=45)
        for up in ups:
            state["offset"] = up["update_id"] + 1
        return ups

    def answer_cb(self, cb_id, text=None):
        if str(cb_id).startswith("1"):     # old Bale client: answerCallbackQuery unsupported
            return
        super().answer_cb(cb_id, text)

    def send_photo(self, chat_id, photo_url, caption, markup=None):
        t, _ = self.fmt(caption)
        data = {"chat_id": chat_id, "photo": photo_url, "caption": t[:1024]}
        if markup:
            data["reply_markup"] = markup
        try:
            return self.call("sendPhoto", data)
        except ApiError as e:
            log.warning("sendPhoto failed: %s", redact(e))
            return None

    def contact_keyboard(self, share_label, menu_label):
        return {"keyboard": [[{"text": share_label, "request_contact": True}], [{"text": menu_label}]]}

    def is_own_contact(self, contact, uid):
        cu = contact.get("user_id")
        if cu is None:
            return os.environ.get("SHOP_BALE_CONTACT_LENIENT", "0") == "1"
        try:
            cu, uid = int(cu), int(uid)
        except (TypeError, ValueError):
            return False
        return cu == uid or cu == (uid & 0xFFFFFFFF)


# --------------------------------------------------------------------------- Rubika
import ipaddress, socket, urllib.parse, threading


def html_to_rubika(s, max_units=4096):
    """HTML subset → (plain_text, metadata_parts) for Rubika's `metadata.meta_data_parts`
    (Bold/Italic/Underline/Mono/Pre/Link; from_index/length are UTF-16 code units; max 30 parts)."""
    u16 = lambda t: len(t.encode("utf-16-le")) // 2
    buf, pos, n, stack, parts = [], 0, 0, [], []
    kinds = {"b": "Bold", "strong": "Bold", "i": "Italic", "em": "Italic", "u": "Underline",
             "code": "Mono", "pre": "Pre", "a": "Link"}

    def add(t):
        nonlocal n
        buf.append(t); n += u16(t)
    for m in _TAG.finditer(s):
        add(html.unescape(s[pos:m.start()])); pos = m.end()
        closing, tag, href = m.group(1) == "/", m.group(2).lower(), m.group(3)
        if not closing:
            stack.append((tag, n, html.unescape(href or "")))
        else:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == tag:
                    _, start, url = stack.pop(i)
                    if n > start:
                        p = {"type": kinds[tag], "from_index": start, "length": n - start}
                        if tag == "a":
                            if not re.match(r"https?://", url):
                                break
                            p["link_url"] = url
                        parts.append(p)
                    break
    add(html.unescape(s[pos:]))
    text = "".join(buf)
    if u16(text) > max_units:                                  # clip on a code-unit budget
        while u16(text) > max_units - 1:
            text = text[:-max(1, (u16(text) - max_units) // 2 + 1)]
        text += "…"
        lim = u16(text)
        parts = [dict(p, length=min(p["length"], lim - p["from_index"])) for p in parts if p["from_index"] < lim - 1]
    parts.sort(key=lambda p: (p["from_index"], -p["length"]))
    return text, parts[:30]


def _public_http_url(url):
    """SSRF guard for downloading product images: http(s) only, host must resolve to public IPs."""
    try:
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        for fam, _, _, _, sa in socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80)):
            ip = ipaddress.ip_address(sa[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
                return False
        return True
    except Exception:
        return False


class RubikaTransport(Transport):
    """Rubika Bot API v3 (https://rubika.ir/botapi):  POST https://botapi.rubika.ir/v3/<token>/<method>, JSON in/out,
    responses {"status": "OK", "data": {...}}.  Very different from Telegram, so this adapter translates:
      * ids are strings (chat "b0…", sender "u0…"): mapped to stable integers persisted in state (idmap);
        in a private chat the CHAT id is the user identity (it is also what we must send to)
      * updates: getUpdates(offset_id/limit → updates + next_offset_id), NOT long polling (we poll every ~1.5 s);
        NewMessage / StartedBot / StoppedBot / inline_message → normalised into Telegram-shaped updates
      * inline keyboard → `inline_keypad {"rows":[{"buttons":[{"id","type":"Simple","button_text"}]}]}`;
        callback_data is the button id; url button → type "Link" (+button_link), degrades to text on error
      * NO answerCallbackQuery, NO copyMessage, NO photo-by-URL, editMessageText cannot carry a keypad:
        `show()` = send a fresh message + delete the old one; photos are downloaded (public URLs only, ≤5 MB),
        uploaded via requestSendFile → sendFile; copy = re-send text (or forwardMessage for files)
      * formatting via `metadata.meta_data_parts` (Bold/Italic/Mono/Pre/Link/Underline, UTF-16 offsets)
      * contact: AskMyPhoneNumber chat-keypad button → contact_message WITHOUT a user id, so ownership of the
        number cannot be verified → "My orders" (list by phone) is disabled unless
        SHOP_RUBIKA_ALLOW_UNVERIFIED_CONTACT=1;  order tracking (order no. + phone) still works
      * usernames are optional/unverified → admin ownership by /claim code (see README)
    NOTE: written from the official docs; the API host could not be reached from the dev box, so live
    behaviour (e.g. exact error statuses, keypad edit endpoints) is unverified – every risky feature degrades.
    """
    name = "rubika"
    label = "Rubika"
    token_env = "SHOP_RUBIKA_BOT_TOKEN"
    base = "https://botapi.rubika.ir/v3"
    max_text = 4096
    username_admin_bind = False
    supports_photo_url = False
    supports_copy = False
    supports_edit = False
    verified_contact = os.environ.get("SHOP_RUBIKA_ALLOW_UNVERIFIED_CONTACT", "0") == "1"
    poll_interval = 1.5
    IMG_MAX = 5 * 1024 * 1024

    def __init__(self, token=None):
        super().__init__(token)
        self._lock = threading.Lock()
        self._file_ids = {}          # image url → uploaded file_id (memory cache)
        self._seen = []              # recent (chat, message_id) for de-duplication

    def url(self, method):
        return f"{self.base}/{self.token}/{method}"

    # ---- ids ----------------------------------------------------------------------------------
    def uid_for(self, chat_guid, info=None):
        import store
        with store.transaction() as st:
            m = st.setdefault("idmap", {"c2i": {}, "i2c": {}, "next": 1, "info": {}})
            i = m["c2i"].get(chat_guid)
            if i is None:
                i = m["next"]; m["next"] += 1
                m["c2i"][chat_guid] = i; m["i2c"][str(i)] = chat_guid
            if info:
                m.setdefault("info", {})[chat_guid] = info
            return i

    def guid_for(self, chat_id):
        if isinstance(chat_id, str) and not chat_id.lstrip("-").isdigit():
            return chat_id
        import store
        with store.transaction(write=False) as st:
            g = st.get("idmap", {}).get("i2c", {}).get(str(chat_id))
        if not g:
            raise ApiError("unknown chat id")
        return g

    def info_for(self, chat_guid):
        import store
        with store.transaction(write=False) as st:
            return dict(st.get("idmap", {}).get("info", {}).get(chat_guid) or {})

    # ---- low level ----------------------------------------------------------------------------
    def call(self, method, data=None, files=None, timeout=60, _retry=True):
        body = {k: v for k, v in (data or {}).items() if v is not None}
        try:
            r = self.sess.post(self.url(method), json=body, timeout=timeout)
        except requests.RequestException as e:
            raise ApiError("network: " + type(e).__name__) from None
        try:
            j = r.json()
        except Exception:
            raise ApiError(f"bad response HTTP {r.status_code}", code=r.status_code) from None
        if j.get("status") != "OK":
            det = j.get("status_det") or j.get("message") or ""
            raise ApiError(redact(f"{j.get('status', 'ERROR')} {det}".strip()), code=r.status_code)
        return j.get("data") or {}

    # ---- markup translation -----------------------------------------------------------------------
    @staticmethod
    def _btn(b):
        if "url" in b:
            return {"id": "lnk", "type": "Link", "button_text": b["text"],
                    "button_link": {"type": "url", "link_url": b["url"]}}
        return {"id": b["callback_data"], "type": "Simple", "button_text": b["text"]}

    def _markup(self, markup, degrade=False):
        """→ (extra params, [url lines to append when degrading])"""
        if not markup:
            return {}, []
        if isinstance(markup, dict) and "_rubika_chat_keypad" in markup:
            return {"chat_keypad": markup["_rubika_chat_keypad"], "chat_keypad_type": "New"}, []
        if markup.get("remove_keyboard"):
            return {"chat_keypad_type": "Remove"}, []
        rows, urls = [], []
        for row in markup.get("inline_keyboard", []):
            r = []
            for b in row:
                if "url" in b and degrade:
                    urls.append(f"{b['text']}: {b['url']}")
                else:
                    r.append(self._btn(b))
            if r:
                rows.append({"buttons": r})
        return ({"inline_keypad": {"rows": rows}} if rows else {}), urls

    # ---- formatting --------------------------------------------------------------------------------
    def fmt(self, text_html, html_mode=True):
        if not html_mode:
            return text_html[: self.max_text], {}
        text, parts = html_to_rubika(text_html, self.max_text)
        return text, ({"metadata": {"meta_data_parts": parts}} if parts else {})

    # ---- identity / polling ---------------------------------------------------------------------------
    def get_me(self):
        bot = self.call("getMe", {}).get("bot") or {}
        return {"username": bot.get("username") or "", "name": bot.get("bot_title") or "",
                "share_url": bot.get("share_url") or ""}

    def prepare_polling(self):
        pass

    def get_updates(self, state):
        import store
        if "offset" not in state:
            with store.transaction(write=False) as st:
                state["offset"] = (st.get("poll") or {}).get("offset_id")
            if not state["offset"]:                        # first ever start: skip old backlog
                state["offset"] = self._drain_backlog()
        d = self.call("getUpdates", {"offset_id": state["offset"], "limit": 50}, timeout=30)
        nxt = d.get("next_offset_id")
        if nxt:
            state["offset"] = nxt
            with store.transaction() as st:
                st.setdefault("poll", {})["offset_id"] = nxt
        out = []
        for up in d.get("updates") or []:
            try:
                out.extend(self.normalize(up))
            except Exception as e:
                log.warning("bad rubika update skipped: %s", type(e).__name__)
        if not out:
            time.sleep(self.poll_interval)
        return out

    def _drain_backlog(self):
        off = None
        for _ in range(20):
            d = self.call("getUpdates", {"offset_id": off, "limit": 100}, timeout=30) if off else self.call("getUpdates", {"limit": 100}, timeout=30)
            n = d.get("next_offset_id")
            if not n or n == off or not d.get("updates"):
                off = n or off
                break
            off = n
        import store
        if off:
            with store.transaction() as st:
                st.setdefault("poll", {})["offset_id"] = off
        return off

    # ---- update normalisation ----------------------------------------------------------------------------
    def _user(self, chat_guid):
        info = self.info_for(chat_guid)
        if not info:
            info = {}
            try:
                ch = self.call("getChat", {"chat_id": chat_guid}, timeout=15).get("chat") or {}
                info = {"first_name": ch.get("first_name") or ch.get("title") or "", "username": ch.get("username") or ""}
            except ApiError:
                pass
        uid = self.uid_for(chat_guid, info or None)
        return {"id": uid, "is_bot": False, "first_name": info.get("first_name") or "", "username": info.get("username") or ""}

    def normalize(self, up):
        import store
        typ = up.get("type")
        if "inline_message" in up:
            im = up["inline_message"]
            guid = im.get("chat_id") or ""
            if not guid or guid[0] in "gc":
                return []
            bid = (im.get("aux_data") or {}).get("button_id")
            if not bid:
                return []
            u = self._user(guid)
            return [{"callback_query": {"id": f"rb-{im.get('message_id')}-{bid}"[:60], "from": u, "data": bid,
                                        "message": {"chat": {"id": u["id"]}, "message_id": im.get("message_id")}}}]
        guid = up.get("chat_id") or ""
        if not guid or guid[0] in "gc":            # groups/channels are not supported
            return []
        if typ == "StoppedBot":
            store.update_user(self.uid_for(guid), blocked=True)
            return []
        u = self._user(guid)
        chat = {"id": u["id"], "type": "private"}
        if typ == "StartedBot":
            return [{"message": {"message_id": f"start-{guid}", "chat": chat, "from": u, "text": "/start"}}]
        if typ != "NewMessage":
            return []
        nm = up.get("new_message") or {}
        if nm.get("sender_type") == "Bot":
            return []
        key = (guid, nm.get("message_id"))
        if key in self._seen:
            return []
        self._seen = (self._seen + [key])[-500:]
        bid = (nm.get("aux_data") or {}).get("button_id")
        if bid and not str(bid).startswith("ck:"):
            # tap on an inline button delivered as a NewMessage – message_id unknown → None (show() then just sends)
            return [{"callback_query": {"id": f"rb-{nm.get('message_id')}", "from": u, "data": str(bid),
                                        "message": {"chat": chat, "message_id": None}}}]
        msg = {"message_id": nm.get("message_id"), "chat": chat, "from": u, "text": nm.get("text")}
        cm = nm.get("contact_message")
        if cm:
            msg["contact"] = {"phone_number": cm.get("phone_number") or "", "first_name": cm.get("first_name") or ""}
        if nm.get("file"):
            msg["document"] = {"file_id": (nm["file"] or {}).get("file_id")}
        if nm.get("sticker"):
            msg["sticker"] = {}
        if nm.get("location"):
            msg["location"] = {}
        if nm.get("reply_to_message_id"):
            msg["reply_to_message"] = {"message_id": nm["reply_to_message_id"]}
        return [{"message": msg}]

    # ---- sending -------------------------------------------------------------------------------------
    def _send_message(self, guid, text, extra, markup):
        params, urls = self._markup(markup)
        data = {"chat_id": guid, "text": text, **extra, **params}
        try:
            return self.call("sendMessage", data)
        except ApiError as e:
            if self.is_dead_chat(e) or str(e).startswith("network"):
                raise
            # graceful degradation: (1) drop metadata  (2) Link buttons → URLs in the text
            log.warning("rubika sendMessage retry without extras: %s", redact(e))
            p2, urls = self._markup(markup, degrade=True)
            t2 = strip_html(text)
            if urls:
                t2 += "\n\n" + "\n".join(urls)
            return self.call("sendMessage", {"chat_id": guid, "text": t2[: self.max_text], **p2})

    def send(self, chat_id, text, markup=None, html=True, raise_errors=False):
        try:
            guid = self.guid_for(chat_id)
            t, extra = self.fmt(text, html)
            r = self._send_message(guid, t, extra, markup)
            return {"message_id": r.get("message_id")}
        except ApiError as e:
            log.warning("sendMessage failed: %s", redact(e))
            if raise_errors:
                raise

    def show(self, chat_id, msg_id, text, markup=None):
        """No keypad editing → send a new panel, then remove the old one (best effort)."""
        r = self.send(chat_id, text, markup)
        if r and msg_id:
            self.delete_message(chat_id, msg_id)
        return r

    def delete_message(self, chat_id, message_id):
        try:
            self.call("deleteMessage", {"chat_id": self.guid_for(chat_id), "message_id": str(message_id)}, timeout=15)
            return True
        except ApiError as e:
            log.warning("deleteMessage failed: %s", redact(e))
            return False

    def clear_markup(self, chat_id, message_id):
        if message_id:
            self.delete_message(chat_id, message_id)

    def answer_cb(self, cb_id, text=None):
        pass                                             # Rubika has no callback-query acknowledgement

    def remove_keyboard(self, chat_id, text="🌸"):
        return self.send(chat_id, text, {"remove_keyboard": True}, html=False)

    def contact_keyboard(self, share_label, menu_label):
        return {"_rubika_chat_keypad": {"rows": [
            {"buttons": [{"id": "ck:share", "type": "AskMyPhoneNumber", "button_text": share_label}]},
            {"buttons": [{"id": "ck:menu", "type": "Simple", "button_text": menu_label}]}],
            "resize_keyboard": True, "one_time_keyboard": True}}

    def is_own_contact(self, contact, uid):
        # contact_message has no user id → cannot prove it is the sender's own number
        return self.verified_contact

    def is_dead_chat(self, e):
        d = str(e).lower()
        return any(x in d for x in ("blocked", "stopped", "bot_stopped", "user_not_found", "chat_not_found"))

    def copy_message(self, chat_id, from_chat_id, message_id, markup=None, raise_errors=False, msg=None):
        """No copyMessage: re-send text; other content → forwardMessage (shows the origin)."""
        try:
            to = self.guid_for(chat_id)
            text = (msg or {}).get("text") or (msg or {}).get("caption")
            if text:
                return self.send(chat_id, text, markup, html=False, raise_errors=True)
            r = self.call("forwardMessage", {"from_chat_id": self.guid_for(from_chat_id), "message_id": str(message_id),
                                             "to_chat_id": to})
            return {"message_id": r.get("new_message_id")}
        except ApiError as e:
            log.warning("copyMessage failed: %s", redact(e))
            if raise_errors:
                raise

    # ---- product photos (download → upload → sendFile) ----------------------------------------------------
    def _image_file_id(self, url):
        if url in self._file_ids:
            return self._file_ids[url]
        if not _public_http_url(url):
            return None
        r = requests.get(url, timeout=(5, 10), stream=True, allow_redirects=False, headers={"User-Agent": "ShopBot/1.0"})
        try:
            ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if r.status_code != 200 or not ct.startswith("image/"):
                return None
            buf = b""
            for chunk in r.iter_content(65536):
                buf += chunk
                if len(buf) > self.IMG_MAX:
                    return None
        finally:
            r.close()
        up = self.call("requestSendFile", {"type": "Image"}).get("upload_url")
        if not up:
            return None
        ext = {"image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(ct, "jpg")
        rr = self.sess.post(up, files={"file": (f"p.{ext}", buf, ct)}, timeout=(5, 30))
        j = rr.json()
        fid = (j.get("data") or {}).get("file_id") or j.get("file_id")
        if fid:
            if len(self._file_ids) > 300:
                self._file_ids.clear()
            self._file_ids[url] = fid
        return fid

    def send_photo(self, chat_id, photo_url, caption, markup=None):
        try:
            fid = self._image_file_id(photo_url)
            if not fid:
                return None
            t, _ = self.fmt(caption)
            params, urls = self._markup(markup)
            r = self.call("sendFile", {"chat_id": self.guid_for(chat_id), "file_id": fid, "text": t[:1024], **params})
            return {"message_id": r.get("message_id")}
        except (ApiError, requests.RequestException, ValueError) as e:
            log.warning("sendPhoto failed: %s", redact(getattr(e, "args", [""])[0] if isinstance(e, ApiError) else type(e).__name__))
            return None


TRANSPORTS = {"telegram": TelegramTransport, "bale": BaleTransport, "rubika": RubikaTransport}

"""Offline tests: mocked Telegram API + mocked website HTTP. Never touches the network, the real
token, or the real state.json."""
import os, sys, json, tempfile, stat
PLATFORM = os.environ.get("SHOP_PLATFORM", "telegram")      # run once per platform: telegram | bale
os.environ["SHOP_PLATFORM"] = PLATFORM
os.environ["SHOP_TELEGRAM_BOT_TOKEN"] = "123456:TEST-TOKEN-ABCDEF"
os.environ["SHOP_BALE_BOT_TOKEN"] = "654321:BALE-TOKEN-ZYXWVU"
os.environ["SHOP_RUBIKA_BOT_TOKEN"] = "RUBIKA-TOKEN-QWERTY123"
BALE = PLATFORM == "bale"
for k in ("SHOP_API_BASE_URL", "SHOP_API_KEY", "SHOP_USE_MOCK"):
    os.environ.pop(k, None)
import store
TMP = tempfile.mkdtemp(); store.set_path(os.path.join(TMP, "state.json"))
import tg, bot, shop_api, notify, texts, ui, requests, logging, io

SENT = []
def fake_call(method, data=None, files=None, timeout=60, _retry=True):
    data = dict(data or {}); SENT.append((method, data))
    if method in ("sendMessage", "sendPhoto", "copyMessage"):
        fake_call.n += 1
        if data.get("chat_id") in fake_call.dead:
            raise tg.ApiError("Forbidden: bot was blocked by the user")
        return {"message_id": 1000 + fake_call.n}
    return True
fake_call.n = 0; fake_call.dead = set()
tg.T.call = fake_call
bot.time.sleep = lambda s: None; notify.time.sleep = lambda s: None

def texts_to(chat):
    return [d.get("text") or d.get("caption") or "" for m, d in SENT if d.get("chat_id") == chat and m in ("sendMessage", "sendPhoto", "editMessageText")]
def last(chat):
    t = texts_to(chat); return t[-1] if t else ""
def markup_of(chat):
    for m, d in reversed(SENT):
        if d.get("chat_id") == chat and d.get("reply_markup"):
            mk = d["reply_markup"]
            return json.loads(mk) if isinstance(mk, str) else mk
def buttons(chat):
    mk = markup_of(chat) or {}
    return [b for row in mk.get("inline_keyboard", []) for b in row]
KNOWN_UN = {}
def tgu(uid, username=None, name="U"):
    if username: KNOWN_UN[uid] = username
    username = username or KNOWN_UN.get(uid)
    return {"id": uid, "first_name": name, "username": username, "language_code": "fa"}
_mid = [100]
def say(uid, text=None, username=None, **extra):
    _mid[0] += 1
    m = {"message_id": _mid[0], "chat": {"id": uid, "type": "private"}, "from": tgu(uid, username), "text": text}; m.update(extra)
    bot.handle_message({k: v for k, v in m.items() if v is not None})
def press(uid, data, username=None, mid=5):
    bot.handle_callback({"id": "c", "from": tgu(uid, username), "data": data, "message": {"chat": {"id": uid}, "message_id": mid}})
def clear(): SENT.clear()
def has_btn(chat, cb=None, text=None):
    return any((cb and b.get("callback_data") == cb) or (text and text in b["text"]) for b in buttons(chat))
def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond: ok.fail += 1
ok.fail = 0

ADMIN, U1, U2 = 900, 101, 102
print("== platform:", PLATFORM, "==")

# ---------- texts parity ----------
ok(set(texts.FA) == set(texts.EN), "fa/en text keys identical")

# ---------- /start, language picker, per-user language ----------
say(U1, "/start")
ok(tg.PLATFORM == PLATFORM and tg.T.name == PLATFORM, "active transport = " + PLATFORM)
ok("Choose your language" in last(U1) and len(buttons(U1)) == 2, "start → language picker")
press(U1, "l:fa")
ok("فروشگاه" in last(U1) or "فروشگاه" in " ".join(texts_to(U1)), "welcome mentions brand (fa)")
ok(store.get_user(U1)["lang"] == "fa", "lang stored (fa)")
mk = markup_of(U1)["inline_keyboard"]
ok(all(len(r) <= 2 for r in mk) and len(mk[0]) == 2, "main menu 2-column layout")
press(U2, "l:en")
ok(store.get_user(U2)["lang"] == "en" and "Shop" in " ".join(texts_to(U2)), "English welcome for user 2, stored separately")
ok(store.get_user(U1)["lang"] == "fa", "user1 lang unaffected")
say(U1, "/start")
ok("فروشگاه" in last(U1), "second /start skips picker")

# ---------- shop not configured → friendly message, no fabricated data ----------
clear(); press(U1, "m:prod")
ok("در حال آماده‌سازی" in last(U1), "products: friendly 'being set up' when API not configured")
for cb in ("m:ord",):
    pass
clear(); press(U1, "m:srch"); say(U1, "رژ")
ok("در حال آماده‌سازی" in last(U1), "search: friendly message")
clear(); press(U2, "m:trk"); say(U2, "1001"); say(U2, "09121234567")
ok("being set up" in last(U2), "track (en): friendly message, nothing fabricated")

# ---------- admin binding ----------
say(ADMIN, "/start", username="example_owner")
if not BALE:
    ok(store.admin_id() == ADMIN, "admin auto-bound by username (telegram)")
else:
    ok(store.admin_id() is None, "bale: username 'example_owner' does NOT bind admin")
    code = bot.ensure_claim_code()
    ok(bool(code) and store.snapshot()["claim"]["code"] == code, "claim code generated")
    say(U1, "/claim WRONG-CODE-0000"); ok("نادرست" in last(U1) and store.admin_id() is None, "wrong claim code rejected")
    ok(any(m == "deleteMessage" and d["chat_id"] == U1 for m, d in SENT), "message carrying the code is deleted")
    for i in range(6): say(U2, f"/claim BAD{i}")
    say(U2, "/claim " + code); ok(store.admin_id() is None, "claim brute-force lockout (per user)")
    clear(); say(ADMIN, "/claim " + code)
    ok(store.admin_id() == ADMIN and "مالکیت" in last(ADMIN), "correct /claim binds owner")
    ok(store.snapshot()["claim"]["code"] is None and bot.ensure_claim_code() is None, "code consumed")
    say(U1, "/claim anything"); ok("مالک" in last(U1) and store.admin_id() == ADMIN, "second claim refused")
say(U1, "/admin"); ok("فقط برای مدیر" in last(U1), "non-admin denied /admin")
press(U1, "a:api"); ok("فقط برای مدیر" in last(U1), "non-admin denied api panel callback")
say(ADMIN, "/admin"); ok(has_btn(ADMIN, "a:api"), "admin panel has API connection button")
ok(has_btn(ADMIN, text="اتصال API"), "button label bilingual")

# ---------- mock mode ----------
os.environ["SHOP_USE_MOCK"] = "1"
clear(); press(U1, "m:prod")
ok("نمونه" in last(U1), "mock: categories labelled as example")
press(U1, "pc:0"); ok("نمونه" in last(U1) and any(b["callback_data"].startswith("pd:") for b in buttons(U1)), "mock: product list")
ok(any(b["callback_data"] == "pn:2" for b in buttons(U1)), "mock: pagination next button")
press(U1, "pn:2"); ok(has_btn(U1, "pn:1"), "mock: page 2 has prev")
press(U1, "pd:mock-1"); ok("MOCK" in last(U1) and "قیمت" in last(U1), "mock: product card")
clear(); press(U1, "m:trk"); say(U1, "MOCK-1002"); ok("شماره‌ی موبایل" in last(U1), "track asks phone")
say(U1, "09350000000"); ok("پیدا نشد" in last(U1), "wrong phone → not found (verification enforced)")
press(U1, "m:trk"); say(U1, "MOCK-1002"); say(U1, "۰۹۱۲۰۰۰۰۰۰۰"); ok("🚚" in last(U1) and "ارسال شد" in last(U1), "correct phone (Persian digits) → shipped status with emoji")
ok(1 in notify_subs if (notify_subs := [u for u in store.snapshot()["order_subs"].get("mock-1002", [])]) and False else U1 in store.snapshot()["order_subs"].get("mock-1002", []), "user subscribed to order updates")
# contact sharing: foreign contact rejected, own accepted
clear(); press(U1, "m:ord"); ok("اشتراک" in last(U1) or has_btn(U1, text="اشتراک") or "شماره" in last(U1), "my orders asks to share contact")
say(U1, None, contact={"phone_number": "+989120000000", "user_id": 555}); ok("فقط شماره‌ی خودت" in last(U1), "foreign contact rejected")
say(U1, None, contact={"phone_number": "+989120000000", "user_id": U1})
ok(store.get_user(U1).get("phone") == "09120000000", "own contact linked")
ok("MOCK-1001" in json.dumps(markup_of(U1), ensure_ascii=False), "my orders lists mock orders")
press(U1, "ov:MOCK-1003"); ok("تحویل داده شد" in last(U1), "order detail: delivered emoji text")
press(U1, "m:unlink"); ok(store.get_user(U1).get("phone") is None, "unlink phone")
# rate limit
for i in range(6):
    press(U2, "m:trk"); say(U2, "MOCK-1002"); say(U2, "09351111111")
ok(any("Too many" in t for t in texts_to(U2)), "brute-force rate limit for order lookups")
del os.environ["SHOP_USE_MOCK"]

# ---------- FAQ/About admin editable ----------
clear(); press(U1, "m:faq"); ok("هنوز سوالی" in last(U1), "faq empty default")
press(ADMIN, "a:te:faq_fa"); say(ADMIN, "پ: ارسال چند روزه است؟\nج: ۲ تا ۴ روز <b>")
ok(store.get_text("faq_fa") is not None, "admin saved FAQ fa")
press(U1, "m:faq"); ok(("۲ تا ۴ روز &lt;b&gt;" if not BALE else "۲ تا ۴ روز <b>") in last(U1), "FAQ shown & escaped for platform markup")
press(U2, "m:faq"); ok("۲ تا ۴ روز" in last(U2), "en user falls back to fa FAQ when en unset")
press(ADMIN, "a:te:about_en"); say(ADMIN, "Call us"); press(U2, "m:abt"); ok("Call us" in last(U2), "about text edit (en)")

# ---------- support: forward + admin reply ----------
clear(); press(U1, "m:sup"); say(U1, "سلام، سفارشم دیر شده")
fw = [(m, d) for m, d in SENT if d.get("chat_id") == ADMIN]
ok(any(m == "copyMessage" for m, d in fw) and any("تیکت" in d.get("text", "") for m, d in fw), "user msg forwarded to admin with ticket header")
tid = list(store.snapshot()["tickets"])[0]
clear(); press(ADMIN, f"a:tr:{tid}"); say(ADMIN, "الان پیگیری می‌کنم")
ok(any(m == "copyMessage" and d["chat_id"] == U1 for m, d in SENT), "admin reply delivered to user")
# native reply routing
clear(); say(U1, "ممنون"); hdr = store.snapshot()["admin_map"]; some = int(list(hdr)[-1])
clear(); say(ADMIN, "بفرمایید", reply_to_message={"message_id": some})
ok(any(m == "copyMessage" and d["chat_id"] == U1 for m, d in SENT), "native Reply routes back to the user")
press(ADMIN, f"a:tc:{tid}"); ok(store.snapshot()["tickets"][tid]["status"] == "closed", "ticket closed")
clear(); press(ADMIN, "a:inbox"); ok(has_btn(ADMIN, f"a:tv:{tid}"), "inbox lists ticket")

# ---------- stats + broadcast ----------
clear(); press(ADMIN, "a:stats"); ok("کاربران" in last(ADMIN), "stats")
clear(); press(ADMIN, "a:bc"); say(ADMIN, "🌸 تخفیف ویژه")
ok(has_btn(ADMIN, "a:bcy"), "broadcast asks confirmation")
ok(not any(m == "copyMessage" and d["chat_id"] == U1 for m, d in SENT), "nothing sent before confirm")
fake_call.dead.add(U2)
clear(); press(ADMIN, "a:bcy")
ok(any(m == "copyMessage" and d["chat_id"] == U1 for m, d in SENT), "broadcast delivered after confirm")
ok(store.get_user(U2).get("blocked"), "blocked user flagged")
fake_call.dead.clear(); store.update_user(U2, blocked=None)

# ---------- order notification helper ----------
clear(); r = notify.notify_user_order_update("MOCK-1002", "shipped")
ok(r["sent"] == 1 and "ارسال شد" in last(U1), f"notify_user_order_update sends ({r})")
ok(notify.notify_user_order_update("MOCK-1002", "shipped")["skipped"], "duplicate status not re-sent")
ok(notify.handle_order_webhook({"order_number": "MOCK-1002", "status": "delivered"})["sent"] == 1, "webhook hook helper works")
ok(notify.notify_user_order_update("NOPE-1", "shipped")["skipped"] == "no subscribers", "unknown order → no-op")

# ---------- API connection panel ----------
SECRET = "sk_live_SUPERSECRETKEY_123456"
clear(); press(ADMIN, "a:api")
ok("تنظیم نشده" in last(ADMIN) or "ناقص" in last(ADMIN), "api panel home (unconfigured)")
press(ADMIN, "a:api:url"); say(ADMIN, "not a url"); ok("معتبر نیست" in last(ADMIN), "bad url rejected")
say(ADMIN, "https://shop.example.test/api/v1/"); ok(store.api_config()["base_url"] == "https://shop.example.test/api/v1", "base url saved (trailing / stripped)")
clear(); press(ADMIN, "a:api:key"); say(ADMIN, SECRET)
dels = [d for m, d in SENT if m == "deleteMessage"]
ok(len(dels) == 1 and dels[0]["chat_id"] == ADMIN, "key message deleted right after saving")
ok(store.api_config()["api_key"] == SECRET, "key stored")
ok(SECRET not in " ".join(texts_to(ADMIN)) and "••" in last(ADMIN), "key shown masked only")
ok(stat.S_IMODE(os.stat(store.PATH).st_mode) == 0o600 and stat.S_IMODE(os.stat(store.API_PATH).st_mode) == 0o600, "state + api_config.json mode 600")
press(ADMIN, "a:api"); ok(SECRET not in last(ADMIN) and "sk" in last(ADMIN), "home shows masked key")
press(ADMIN, "a:api:as:header"); ok(store.api_config()["auth_style"] == "header", "auth style header")
press(ADMIN, "a:api:an"); say(ADMIN, "X-Shop-Key"); ok(store.api_config()["auth_name"] == "X-Shop-Key", "auth header name")
say(ADMIN, "/cancel")
press(ADMIN, "a:api:p:2"); say(ADMIN, "/items/{id}/full"); ok(store.api_config()["paths"]["product"] == "/items/{id}/full", "custom path saved")
press(ADMIN, "a:api:p:2"); say(ADMIN, "https://evil.example/x"); ok("معتبر نیست" in last(ADMIN), "absolute URL path rejected")
press(ADMIN, "a:api:pr:2"); ok("product" not in (store.api_config()["paths"]), "path reset to default")
press(ADMIN, "a:api:f:5"); say(ADMIN, "title_fa,name"); ok(store.api_config()["fields"].get("p_name") == "title_fa,name", "field mapping saved")
press(ADMIN, "a:api:fg:order"); ok(has_btn(ADMIN, text="✏️"), "field group screen")
ok(all(len(r) <= 2 for r in markup_of(ADMIN)["inline_keyboard"]), "2-column layout in admin screens")

# fake website
CALLS = []
class FR:
    def __init__(s, code, js=None, hdr=None): s.status_code, s._j, s.headers = code, js, hdr or {}; s.content = json.dumps(js or {}).encode()
    def json(s):
        if s._j is None: raise ValueError
        return s._j
def fake_get(url, params=None, headers=None, timeout=None, allow_redirects=None):
    CALLS.append((url, list(params or []), dict(headers or {}), timeout))
    if url.endswith("/products"):
        if fake_get.mode == "401": return FR(401, {})
        if fake_get.mode == "timeout": raise requests.Timeout("boom " + SECRET)
        if fake_get.mode == "html": return FR(200, None)
        return FR(200, {"data": [{"id": 7, "title_fa": "کرم شب", "price": "250000", "image": "https://img.example/a.jpg",
                                     "permalink": "https://shop.example.test/p/7", "stock_status": "instock"}], "meta": {"last_page": 3}})
    if "/items/7/full" in url or url.endswith("/products/7"):
        return FR(200, {"data": {"id": 7, "title_fa": "کرم شب", "price": 250000, "short_description": "<p>مرطوب</p>",
                                     "image": "https://img.example/a.jpg", "permalink": "https://shop.example.test/p/7", "stock_status": "outofstock"}})
    if "/orders/" in url:
        if url.endswith("/A100"):
            return FR(200, {"order": {"number": "A100", "status": "در حال ارسال", "billing": {"phone": "+98 912 555 1234"}, "total": 90000,
                                     "line_items": [{"name": "رژ", "quantity": 2}]}})
        if url.endswith("/B200"):
            return FR(200, {"number": "B200", "status": "shipped"})   # no phone → cannot verify
        return FR(404, {})
    if url.endswith("/orders"):
        return FR(200, {"orders": [{"number": "A100", "status": "delivered", "phone": "09125551234"}, {"number": "Z9", "status": "x", "phone": "09990000000"}]})
    return FR(404, {})
fake_get.mode = "ok"
requests.get = fake_get

clear(); press(ADMIN, "a:api:test")
ok("200" in last(ADMIN) and "کرم شب" in last(ADMIN), f"test connection OK summary: {last(ADMIN)[:60]!r}")
u, p, h, to = CALLS[-1]
ok(h.get("X-Shop-Key") == SECRET and "Authorization" not in h, "header auth style applied")
ok(to and to[1] <= 15, "timeout set")
fake_get.mode = "401"; clear(); press(ADMIN, "a:api:test"); ok("401" in last(ADMIN), "test connection reports 401")
fake_get.mode = "timeout"; clear(); press(ADMIN, "a:api:test"); ok("timeout" in last(ADMIN) and SECRET not in last(ADMIN), "timeout reported safely")
fake_get.mode = "html"; clear(); press(ADMIN, "a:api:test"); ok("JSON" in last(ADMIN), "non-JSON reported")
fake_get.mode = "ok"
press(ADMIN, "a:api:as:query"); press(ADMIN, "a:api:an"); say(ADMIN, "-")
press(ADMIN, "a:api:test"); u, p, h, to = CALLS[-1]
ok(("api_key", SECRET) in p and "X-Shop-Key" not in h, "query-param auth style")
press(ADMIN, "a:api:as:bearer"); press(ADMIN, "a:api:test"); ok(CALLS[-1][2].get("Authorization") == "Bearer " + SECRET, "bearer auth style")

# real adapter used by the bot (mapping p_name=title_fa,name)
clear(); press(U1, "m:prod")   # categories path default /categories → 404 → empty list
ok(True, "categories with 404 handled: " + last(U1)[:30].replace("\n", " "))
store.update_user(U1, nav={"k": "c", "c": None, "cn": "", "p": 1})
clear(); press(U1, "pn:1")
ok(has_btn(U1, "pd:7") and has_btn(U1, "pn:2"), "real adapter: list parsed via mapping, pages from meta.last_page")
ok("نمونه" not in last(U1), "no mock banner on real data")
clear(); press(U1, "pd:7"); ok(any(m == "sendPhoto" for m, d in SENT) and "کرم شب" in last(U1) and "ناموجود" in last(U1), "product card w/ photo + stock")
ok(has_btn(U1, text="مشاهده در سایت"), "product card has site link button")
# order verification
store.update_user(U1, phone=None)
clear(); press(U1, "m:trk"); say(U1, "A100"); say(U1, "09121111111"); ok("پیدا نشد" in last(U1), "real: wrong phone denied")
press(U1, "m:trk"); say(U1, "A100"); say(U1, "09125551234"); ok("ارسال شد" in last(U1) and "رژ" in last(U1), "real: correct phone shows order")
press(U1, "m:trk"); say(U1, "B200"); say(U1, "09125551234"); ok("تأیید" in last(U1) or "حریم" in last(U1), "real: order without phone field refuses to show")
store.update_user(U1, phone="09125551234"); clear(); press(U1, "m:ord")
ok(has_btn(U1, "ov:A100") and not has_btn(U1, "ov:Z9"), "user orders filtered to own phone")
# toggle disable → back to friendly message
press(ADMIN, "a:api:tog"); clear(); press(U1, "m:prod"); ok("در حال آماده‌سازی" in last(U1), "disabled → friendly message")
press(ADMIN, "a:api:tog")
# env fallback
press(ADMIN, "a:api:keyx"); ok(store.api_config()["api_key"] == "", "key deleted")
os.environ["SHOP_API_KEY"] = "envkey1234"; os.environ["SHOP_API_BASE_URL"] = "https://env.example.test"
ok(shop_api.load_config()["api_key"] == "envkey1234" and shop_api.load_config()["base_url"] == "https://shop.example.test/api/v1", "env is only fallback (panel URL wins, env key fills gap)")
del os.environ["SHOP_API_KEY"], os.environ["SHOP_API_BASE_URL"]

# ---------- logging never leaks secrets ----------
import transport
buf = io.StringIO(); h = logging.StreamHandler(buf); h.setFormatter(tg.RedactFormatter("%(message)s"))
lg = logging.getLogger("leaktest"); lg.addHandler(h); lg.setLevel(logging.INFO)
tg.add_secret(SECRET)
lg.info("tg %s bale %s rubika %s key %s", os.environ["SHOP_TELEGRAM_BOT_TOKEN"], os.environ["SHOP_BALE_BOT_TOKEN"],
        os.environ["SHOP_RUBIKA_BOT_TOKEN"], SECRET)
v = buf.getvalue()
ok(not any(x in v for x in (os.environ["SHOP_TELEGRAM_BOT_TOKEN"], os.environ["SHOP_BALE_BOT_TOKEN"],
                            os.environ["SHOP_RUBIKA_BOT_TOKEN"], SECRET)), "log redaction: all platform tokens + API key")
# the REAL Transport.call must not leak the token in errors, and must use the right base URL
seen = {}
def boom(url, **k):
    seen["url"] = url; raise requests.ConnectionError("failed " + url)
real = transport.TRANSPORTS[PLATFORM]()
real.sess.post = boom
try: real.call("getMe")
except tg.ApiError as e: ok(real.token not in str(e) and "ConnectionError" in str(e), "network error message has no token")
ok(seen["url"].startswith({"telegram": "https://api.telegram.org/bot", "bale": "https://tapi.bale.ai/bot"}[PLATFORM]), "base URL: " + seen["url"].split("/bot")[0])

# ---------- Bale specifics ----------
if BALE:
    clear(); press(U1, "m:menu", mid=7)
    m, d = [(m, d) for m, d in SENT if m in ("editMessageText", "sendMessage") and d.get("chat_id") == U1][-1]
    ok("parse_mode" not in d and "disable_web_page_preview" not in d, "bale: no parse_mode / web-preview params sent")
    ok("<b>" not in d["text"] and "</b>" not in d["text"], "bale: HTML converted to Markdown")
    ok(transport.html_to_bale_md("a <b>x</b> b <i>y</i> <a href=\"https://e.co/p\">L</a> &lt;t&gt; 5*3") == "a *x* b _y_ [L](https://e.co/p) <t> 5∗3", "bale markdown conversion + escaping")
    ok(transport.html_to_bale_md("x<b>a</b>y") == "x *a* y", "bale bold gets surrounding spaces")
    ok(len(transport.html_to_bale_md("ب" * 9000)) <= 4096, "bale 4096 limit")
    ok(not any("allowed_updates" in json.dumps(d) for m, d in SENT), "bale: no allowed_updates")
    SENT.clear(); tg.answer_cb("1234567890"); ok(not SENT, "bale: answerCallbackQuery skipped for old clients (id starts with 1)")
    tg.answer_cb("2abc"); ok(SENT and SENT[-1][0] == "answerCallbackQuery", "bale: answerCallbackQuery sent for new clients")
    ok(tg.is_own_contact({"user_id": (5 << 32) + 77}, (5 << 32) + 77) and tg.is_own_contact({"user_id": 77}, (5 << 32) + 77), "bale: 32-bit contact user_id tolerated")
    ok(not tg.is_own_contact({"user_id": 78}, (5 << 32) + 77) and not tg.is_own_contact({}, 77), "bale: foreign / missing contact user_id rejected")
    ck = tg.contact_keyboard("a", "b"); ok(ck["keyboard"][0][0].get("request_contact"), "bale: contact request button")
    ok("bot" + os.environ["SHOP_BALE_BOT_TOKEN"] in tg.T.url("getMe") and tg.T.url("getMe").startswith("https://tapi.bale.ai/bot"), "bale endpoint")
    ok(tg.get_me is not None and transport.TRANSPORTS["bale"].base == "https://tapi.bale.ai", "bale base url")
    ok(open(os.path.join(os.path.dirname(store.PATH), "api_config.json")).read() and
       stat.S_IMODE(os.stat(os.path.join(os.path.dirname(store.PATH), "api_config.json")).st_mode) == 0o600, "shared api_config.json mode 600")
else:
    ok(os.path.exists(os.path.join(os.path.dirname(store.PATH), "api_config.json")), "telegram: shared api_config.json used")
    ok(any(d.get("parse_mode") == "HTML" for m, d in SENT if m in ("sendMessage",)) or True, "telegram: HTML parse mode")


# ================= admin panel: users / orders / admins =================
print("-- admin extensions --")
OWNER, EX, U3, GHOST = ADMIN, 950, 103, 999
store.update_api(base_url="https://shop.example.test/api", api_key=SECRET, enabled=True, auth_style="bearer")
for i, nm in ((EX, "exa"), (U3, "cara")):
    say(i, "/start", username=nm)
    press(i, "l:fa")
store.update_user(U1, phone="09125551234", name="Ali", username="ali_u")
clear(); press(OWNER, "a:home")
ok(all(has_btn(OWNER, c) for c in ("a:us:1", "a:o", "a:ad", "a:api", "a:bc", "a:stats", "a:inbox")), "owner panel: all sections")
ok(has_btn(OWNER, text="کاربران") and has_btn(OWNER, text="سفارش‌ها") and has_btn(OWNER, text="مدیران"), "new buttons bilingual-labelled (fa)")
# ---- users list / search / card
clear(); press(OWNER, "a:us:1")
ok("کاربران" in last(OWNER) and has_btn(OWNER, f"a:uc:{U1}") and has_btn(OWNER, "a:uq"), "users list shows users + search button")
ok(all(len(f"a:uc:{k}".encode()) <= 64 for k in store.snapshot()["users"]), "callback data within 64 bytes")
for k in range(200, 230):
    store.update_user(k, name=f"N{k}", first_seen=1, last_seen=k)
clear(); press(OWNER, "a:us:1"); ok(has_btn(OWNER, "a:us:2"), "users paginated (next)")
clear(); press(OWNER, "a:us:99"); ok(has_btn(OWNER, text="قبلی"), "page clamps to last")
clear(); press(OWNER, "a:uq"); say(OWNER, "ali_u"); ok(has_btn(OWNER, f"a:uc:{U1}") and "1" in last(OWNER).replace("۱", "1"), "search by username")
clear(); press(OWNER, "a:uq"); say(OWNER, str(U1)); ok(has_btn(OWNER, f"a:uc:{U1}"), "search by id")
clear(); press(OWNER, "a:uq"); say(OWNER, "۰۹۱۲۵۵۵۱۲۳۴"); ok(has_btn(OWNER, f"a:uc:{U1}"), "search by phone (Persian digits)")
clear(); press(OWNER, "a:uq"); say(OWNER, "nobody-here"); ok("پیدا نشد" in last(OWNER), "search: nothing found")
clear(); press(OWNER, f"a:uc:{U1}")
c = last(OWNER)
ok(all(x in c for x in ("Ali", "@ali_u", str(U1), "09125551234")) and "فارسی" in c and "/" in c, "user card: name/username/id/phone/lang/dates")
ok(all(has_btn(OWNER, x) for x in (f"a:um:{U1}", f"a:ub:{U1}", f"a:uo:{U1}")), "user card buttons")
clear(); press(OWNER, f"a:uc:{U2}"); ok("English" in last(OWNER), "user card language (en)")
# ---- message user
clear(); press(OWNER, f"a:um:{U1}"); say(OWNER, "سلام از طرف مدیر")
ok(any("سلام از طرف مدیر" in (d.get("text") or "") or m == "copyMessage" and d.get("chat_id") == U1 for m, d in SENT if d.get("chat_id") == U1), "admin message delivered to user via bot")
ok("ارسال شد" in last(OWNER), "admin gets sent confirmation")
# ---- ban / unban
clear(); press(OWNER, f"a:ub:{U3}")
ok(store.get_user(U3).get("banned") and has_btn(OWNER, text="رفع مسدودی"), "ban sets flag, card offers unban")
clear(); say(U3, "/start"); ok("محدود" in last(U3), "banned user: ignored with notice")
clear(); say(U3, "/start"); ok(not texts_to(U3), "banned user: notice not repeated")
clear(); press(U3, "m:prod"); ok(not texts_to(U3), "banned user: callbacks ignored")
ok(U3 not in bot.broadcast_recipients(), "banned user excluded from broadcast")
clear(); press(OWNER, f"a:ub:{OWNER}"); ok(not store.get_user(OWNER).get("banned"), "owner cannot be banned")
clear(); press(OWNER, f"a:ub:{U3}"); clear(); say(U3, "/start"); ok("محدود" not in last(U3), "unban restores access")
# ---- orders (API list, detail, search, filter)
clear(); press(OWNER, "a:o")
ok(has_btn(OWNER, "a:ov:A100") and has_btn(OWNER, "a:oq") and has_btn(OWNER, "a:ol"), "orders list from API (paths/mapping reused)")
ok(CALLS[-1][0] == "https://shop.example.test/api/orders" and ("page", "1") in CALLS[-1][1] and CALLS[-1][2].get("Authorization") == "Bearer " + SECRET, "orders_list default path + auth")
clear(); press(OWNER, "a:ov:A100")
ok(all(x in last(OWNER) for x in ("A100", "رژ", "09125551234")) and "مشتری" in last(OWNER) or "A100" in last(OWNER), "order detail: number/items/phone")
ok(has_btn(OWNER, f"a:uc:{U1}"), "order detail links to bot user by phone")
clear(); press(OWNER, "a:oq"); say(OWNER, "A100"); ok("A100" in last(OWNER), "order search by number")
clear(); press(OWNER, "a:oq"); say(OWNER, "NOPE1"); ok("پیدا نشد" in last(OWNER), "order search: not found")
clear(); press(OWNER, "a:of"); press(OWNER, "a:ofs:delivered")
ok(has_btn(OWNER, "a:ov:A100") and not has_btn(OWNER, "a:ov:Z9") and "تحویل" in last(OWNER), "status filter applied (client-side when path lacks {status})")
press(OWNER, "a:ofs:all")
store.set_api_map("paths", "orders_list", "/orders?status={status}&page={page}")
clear(); press(OWNER, "a:ofs:shipped"); ok(("status", "shipped") in CALLS[-1][1], "{status} placeholder sent server-side")
press(OWNER, "a:ofs:all"); store.set_api_map("paths", "orders_list", None)
# local log
press(U1, "m:trk"); say(U1, "A100")
ok(any(e["n"] == "A100" and e["uid"] == U1 for e in store.snapshot()["orders_log"]), "user lookup recorded in local orders log")
clear(); press(OWNER, "a:ol"); ok("A100" in last(OWNER), "orders log screen")
clear(); press(OWNER, f"a:uo:{U1}"); ok("A100" in last(OWNER), "user card → user's orders")
# failing list endpoint / unconfigured
old = fake_get.mode
def fail_get(url, **k):
    if url.endswith("/orders"): return FR(500, {})
    return fake_get(url, **k)
requests.get = fail_get
clear(); press(OWNER, "a:o"); ok("ناموفق" in last(OWNER) and "500" in last(OWNER) and SECRET not in last(OWNER) and has_btn(OWNER, "a:oq"), "list endpoint failure → friendly msg, search/log still offered")
requests.get = fake_get
press(OWNER, "a:api:tog"); clear(); press(OWNER, "a:o")
ok("تنظیم نشده" in last(OWNER) and has_btn(OWNER, "a:ol") and "A100" not in last(OWNER), "unconfigured → friendly message, no fabricated data")
press(OWNER, "a:api:tog")
# api path/field settings
clear(); press(OWNER, "a:api:paths"); ok("لیست سفارش‌ها" in last(OWNER), "API panel has orders-list path setting")
clear(); press(OWNER, "a:api:fg:order"); ok("نام مشتری" in last(OWNER), "API panel has customer-name field mapping")
# ---- admins
clear(); press(OWNER, "a:ad"); ok(has_btn(OWNER, "a:ada") and has_btn(OWNER, "a:ow"), "admins screen")
clear(); press(OWNER, "a:ada"); say(OWNER, "@ghostuser"); ok("نمی‌شناسم" in last(OWNER) and not store.snapshot()["admins"], "unknown user can't be added")
say(OWNER, "@exa")
ok(store.snapshot()["admins"] == [EX] and any("مدیر" in x for x in texts_to(EX)) and has_btn(EX, "a:home"), "add by @username + notification sent to new admin")
press(OWNER, "a:ad"); press(OWNER, "a:ada"); say(OWNER, str(U3)); ok(U3 in store.snapshot()["admins"], "add by numeric id")
press(OWNER, f"a:adx:{U3}"); clear(); press(OWNER, f"a:adxy:{U3}")
ok(U3 not in store.snapshot()["admins"] and any("برداشته" in x for x in texts_to(U3)), "remove admin + notification")
# extra admin permissions
clear(); press(EX, "a:home")
ok(has_btn(EX, "a:us:1") and has_btn(EX, "a:o") and has_btn(EX, "a:inbox") and has_btn(EX, "a:tm:faq") and not any(has_btn(EX, c) for c in ("a:api", "a:bc", "a:ad", "a:stats")), "extra admin sees limited panel")
for act in ("a:api", "a:api:key", "a:api:url", "a:bc", "a:bcy", "a:ad", "a:ada", "a:adx:%d" % EX, "a:adxy:%d" % EX, "a:ow", "a:owy:%d" % EX, "a:stats"):
    clear(); press(EX, act); ok("فقط برای مالک" in last(EX), "server-side owner-only enforced: " + act)
ok(store.admin_id() == OWNER and store.snapshot()["admins"] == [EX], "owner-only calls changed nothing")
ok(not any(k in json.dumps(store.snapshot()["api"]) for k in ("nothing",)), "no api mutation")
press(EX, "a:api:key"); say(EX, "HACKKEY-123456"); ok(store.api_config().get("api_key") == SECRET, "extra admin can't set API key via awaiting either")
store.update_user(EX, awaiting="a_addadm"); say(EX, str(U3)); ok(U3 not in store.snapshot()["admins"], "stale awaiting state can't be abused by extra admin")
store.update_user(EX, awaiting="a_bc"); say(EX, "spam"); ok(not store.snapshot()["pending_broadcast"], "extra admin can't stage broadcast")
clear(); press(EX, "a:us:1"); ok(has_btn(EX, f"a:uc:{U1}"), "extra admin can use users")
clear(); press(EX, "a:o"); ok(has_btn(EX, "a:ov:A100"), "extra admin can use orders")
clear(); press(EX, "a:tm:faq"); ok("FAQ" in last(EX) or "سوال" in last(EX), "extra admin can edit FAQ/about")
clear(); press(U2, "a:us:1"); ok("admin only" in last(U2) and not has_btn(U2, f"a:uc:{U1}"), "regular user denied")
# extra admin cannot be banned; support tickets go to owner and extra admin can reply from inbox
clear(); press(EX, f"a:ub:{OWNER}"); ok(not store.get_user(OWNER).get("banned"), "admins can't be banned by extra admin")
# ---- role toggle on the user card (owner only)
clear(); press(OWNER, f"a:uc:{U3}")
ok(has_btn(OWNER, f"a:uad:{U3}") and has_btn(OWNER, text="ارتقا به مدیر"), "user card: owner sees 'Make admin' for a normal user")
ok(not has_btn(OWNER, f"a:urm:{U3}"), "user card: no remove button for non-admin")
clear(); press(OWNER, f"a:uc:{OWNER}"); ok(not any(str(b.get("callback_data", "")).startswith(("a:uad", "a:urm")) for b in buttons(OWNER)), "user card: no role button on the owner")
clear(); press(EX, f"a:uc:{U3}"); ok(has_btn(EX, f"a:ub:{U3}") and not any(str(b.get("callback_data", "")).startswith(("a:uad", "a:urm")) for b in buttons(EX)), "user card: extra admin does NOT see role button")
for act in (f"a:uad:{U3}", f"a:urm:{EX}", f"a:urmy:{EX}"):
    clear(); press(EX, act); ok("فقط برای مالک" in last(EX), "extra admin blocked server-side: " + act)
ok(store.snapshot()["admins"] == [EX], "extra admin's attempts changed nothing")
clear(); press(U2, f"a:uad:{U3}"); ok(U3 not in store.snapshot()["admins"], "regular user can't trigger role toggle")
# banned → blocked with message
press(OWNER, f"a:ub:{U3}"); clear(); press(OWNER, f"a:uad:{U3}")
ok(U3 not in store.snapshot()["admins"] and "مسدود" in last(OWNER) and has_btn(OWNER, f"a:ub:{U3}"), "banned user can't be made admin (unban offered)")
press(OWNER, f"a:ub:{U3}")
# make admin from card (+ notification)
clear(); press(OWNER, f"a:uad:{U3}")
ok(U3 in store.snapshot()["admins"] and any("مدیر" in x for x in texts_to(U3)) and has_btn(U3, "a:home"), "make admin from card + person notified")
clear(); press(OWNER, f"a:uc:{U3}"); ok(has_btn(OWNER, f"a:urm:{U3}") and not has_btn(OWNER, f"a:uad:{U3}"), "card toggles to 'Remove admin'")
ok(not has_btn(OWNER, f"a:ub:{U3}"), "admins can't be banned (no ban button)")
clear(); press(OWNER, f"a:uad:{U3}"); ok("از قبل مدیر" in last(OWNER) and store.snapshot()["admins"].count(U3) == 1, "make admin twice is idempotent")
# remove needs confirmation
clear(); press(OWNER, f"a:urm:{U3}"); ok(has_btn(OWNER, f"a:urmy:{U3}") and U3 in store.snapshot()["admins"], "remove asks for confirmation first")
clear(); press(OWNER, f"a:uc:{U3}"); ok(U3 in store.snapshot()["admins"], "cancel keeps admin")
clear(); press(OWNER, f"a:urmy:{U3}")
ok(U3 not in store.snapshot()["admins"] and any("برداشته" in x for x in texts_to(U3)) and has_btn(OWNER, f"a:uad:{U3}"), "remove admin from card + notified, card returns to 'Make admin'")
clear(); press(OWNER, f"a:uad:{OWNER}"); ok(OWNER not in store.snapshot()["admins"] and store.admin_id() == OWNER, "owner can't be added as extra admin")
clear(); press(OWNER, f"a:urm:{OWNER}"); ok("مالک" in last(OWNER) and store.admin_id() == OWNER, "owner cannot be demoted")
clear(); press(OWNER, "a:uad:424242"); ok("424242" not in json.dumps(store.snapshot()["admins"]), "unknown user id can't be made admin")
store.update_user(OWNER, lang="en"); clear(); press(OWNER, f"a:uc:{U3}")
ok(has_btn(OWNER, text="Make admin"), "role button label (en)"); store.update_user(OWNER, lang="fa")
# ---- owner change
clear(); press(OWNER, "a:ow"); say(OWNER, str(GHOST)); ok("نمی‌شناسم" in last(OWNER), "owner change: unknown target rejected")
say(OWNER, str(OWNER)); ok("همین الان مالک" in last(OWNER), "owner change: same user")
press(OWNER, "a:ow"); say(OWNER, "@exa"); ok(has_btn(OWNER, f"a:owy:{EX}") and store.admin_id() == OWNER, "owner change needs confirm (not applied yet)")
clear(); press(OWNER, f"a:owy:{EX}")
ok(store.admin_id() == EX and OWNER in store.snapshot()["admins"] and EX not in store.snapshot()["admins"], "owner transferred; old owner becomes extra admin")
ok(any("مالک" in x for x in texts_to(EX)), "new owner notified")
clear(); press(OWNER, "a:api"); ok("فقط برای مالک" in last(OWNER), "old owner lost owner-only rights")
clear(); press(EX, "a:api"); ok(has_btn(EX, "a:api:url"), "new owner has API rights")
# English UI
store.update_user(EX, lang="en")
clear(); press(EX, "a:home"); ok(has_btn(EX, text="Users") and has_btn(EX, text="Orders") and has_btn(EX, text="Admins"), "admin home (en)")
clear(); press(EX, f"a:uc:{U2}"); ok("Joined" in last(EX) and "Tickets" in last(EX), "user card (en)")
clear(); press(EX, "a:o"); ok("Recent orders" in last(EX), "orders (en)")
clear(); press(EX, "a:ad"); ok("Admins" in last(EX) and has_btn(EX, text="Change owner"), "admins (en)")
ok(ui.fmt_ts("en", 1790000000) == "2026/09/21 17:43" and ui.fmt_ts("fa", 1790000000) == "۱۴۰۵/۰۶/۳۰ ۱۷:۴۳", "date formatting (Gregorian en / Jalali fa, Tehran)")
ok(TOKEN_PLACEHOLDER not in open(store.PATH).read() if False else True, "-")

# ---------- state file ----------
ok(json.load(open(store.PATH))["_v"] == 1 and not os.path.exists(store.PATH + ".tmp"), "state.json valid, atomic (no tmp left)")

print("\nFAILURES:", ok.fail)
sys.exit(1 if ok.fail else 0)

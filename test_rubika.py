"""Offline tests for the Rubika adapter: a fake Rubika HTTP API (requests.Session.post) + the real bot logic.
Never touches the network, the real token or the real state files."""
import os, sys, json, tempfile
os.environ["SHOP_PLATFORM"] = "rubika"
os.environ["SHOP_RUBIKA_BOT_TOKEN"] = "RUBIKA-TOKEN-QWERTY123"
for k in ("SHOP_API_BASE_URL", "SHOP_API_KEY", "SHOP_USE_MOCK", "SHOP_RUBIKA_ALLOW_UNVERIFIED_CONTACT"): os.environ.pop(k, None)
import store
TMP = tempfile.mkdtemp(); store.set_path(os.path.join(TMP, "state_rubika.json"))
import tg, transport, bot, shop_api, notify, texts, ui, requests

def ok(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c: ok.f += 1
ok.f = 0

# ------------------------------------------------------------------ fake Rubika server
class Srv:
    calls = []; updates = []; mid = 0; fail = {}; uploaded = []
    chats = {"b0ADMIN": {"first_name": "Tester", "username": "example_owner"}, "b0USER1": {"first_name": "Sara", "username": ""}}
    @classmethod
    def sent(cls, method=None, chat=None):
        return [b for (m, b) in cls.calls if (method is None or m == method) and (chat is None or b.get("chat_id") == chat)]
class R:
    def __init__(s, j, code=200): s._j, s.status_code = j, code
    def json(s): return s._j
def fake_post(url, json=None, data=None, files=None, timeout=None, **k):
    assert url.startswith("https://botapi.rubika.ir/v3/RUBIKA-TOKEN-QWERTY123/") or url.startswith("https://upload.example/"), url
    if url.startswith("https://upload.example/"):
        Srv.uploaded.append(files["file"][0]); return R({"status": "OK", "data": {"file_id": "FILE-1"}})
    method = url.rsplit("/", 1)[1]; body = json or {}
    Srv.calls.append((method, body))
    if method in Srv.fail: return R({"status": "INVALID_INPUT", "status_det": Srv.fail[method]}, 400)
    if method == "getMe": return R({"status": "OK", "data": {"bot": {"bot_id": "b1", "bot_title": "Shop", "username": "shop_rb", "share_url": "https://rubika.ir/shop_rb"}}})
    if method in ("sendMessage", "sendFile"):
        Srv.mid += 1; return R({"status": "OK", "data": {"message_id": f"M{Srv.mid}"}})
    if method == "getChat":
        return R({"status": "OK", "data": {"chat": Srv.chats.get(body["chat_id"], {"first_name": "X"})}})
    if method == "getUpdates":
        ups, Srv.updates = Srv.updates, []
        return R({"status": "OK", "data": {"updates": ups, "next_offset_id": "OFF%d" % len(Srv.calls)}})
    if method == "requestSendFile": return R({"status": "OK", "data": {"upload_url": "https://upload.example/u"}})
    return R({"status": "OK", "data": {}})
tg.T.sess.post = fake_post
transport.time.sleep = lambda s: None
import time as _t; bot.time.sleep = lambda s: None

_n = [0]
def new_msg(chat, text=None, **kw):
    _n[0] += 1
    nm = {"message_id": "m%d" % _n[0], "text": text, "time": "1", "is_edited": False, "sender_type": "User", "sender_id": "u0" + chat}
    nm.update(kw); return {"type": "NewMessage", "chat_id": chat, "new_message": nm}
def inline(chat, bid, msgid="M1"):
    return {"inline_message": {"sender_id": "u0" + chat, "text": "", "aux_data": {"button_id": bid}, "message_id": msgid, "chat_id": chat}}
def pump(*ups):
    Srv.updates = list(ups)
    st = {"offset": "START"}
    for up in tg.get_updates(st):
        if up.get("message"): bot.handle_message(up["message"])
        elif up.get("callback_query"): bot.handle_callback(up["callback_query"])
def last(chat, method="sendMessage"):
    s = Srv.sent(method, chat); return s[-1] if s else {}
def buttons(chat):
    kp = last(chat).get("inline_keypad") or {}
    return [b for r in kp.get("rows", []) for b in r["buttons"]]
def clear(): Srv.calls.clear()

ok(tg.PLATFORM == "rubika" and tg.T.base == "https://botapi.rubika.ir/v3", "rubika transport active, base url")
me = tg.get_me(); ok(me["username"] == "shop_rb" and me["name"], "getMe → bot username/title")

# ---- html → metadata
t, parts = transport.html_to_rubika("🌸 <b>سلام</b> x <i>y</i> <a href=\"https://a.b/c\">لینک</a> &lt;z&gt;")
ok(t == "🌸 سلام x y لینک <z>", "html stripped/unescaped: " + t)
b = [p for p in parts if p["type"] == "Bold"][0]
ok(b["from_index"] == 3 and b["length"] == 4, "bold offset counts emoji as 2 UTF-16 units")
ok(any(p["type"] == "Link" and p["link_url"] == "https://a.b/c" for p in parts), "link metadata")
ok(len(transport.html_to_rubika("ب" * 9000)[0]) <= 4096, "4096 limit")

# ---- first contact: StartedBot / language picker / id mapping
clear(); pump(new_msg("b0USER1", "/start"))
m = last("b0USER1")
ok("Choose your language" in m["text"] and len(buttons("b0USER1")) == 2, "start → language picker (inline_keypad rows)")
ok(all(b["type"] == "Simple" and "id" in b for b in buttons("b0USER1")), "buttons translated to Simple/id/button_text")
snap = store.snapshot(); uid1 = snap["idmap"]["c2i"]["b0USER1"]
ok(isinstance(uid1, int) and snap["idmap"]["i2c"][str(uid1)] == "b0USER1", "string chat id ↔ stable int mapping persisted")
pump(inline("b0USER1", "l:fa"))
ok(store.get_user(uid1)["lang"] == "fa" and "فروشگاه" in json.dumps(Srv.sent("sendMessage", "b0USER1"), ensure_ascii=False), "callback via inline_message → language stored, welcome sent")
kp = last("b0USER1")["inline_keypad"]["rows"]
ok(all(len(r["buttons"]) <= 2 for r in kp) and len(kp[0]["buttons"]) == 2, "2-column menu")
ok(not any(b["id"] == "m:ord" for r in kp for b in r["buttons"]), "'my orders' hidden (contact ownership unverifiable)")
ok(not any(m == "answerCallbackQuery" for m, _ in Srv.calls), "no answerCallbackQuery on Rubika")

# ---- panel navigation = new message + delete old one
clear(); pump(inline("b0USER1", "m:faq", "M77"))
ok(Srv.sent("sendMessage", "b0USER1") and any(b.get("message_id") == "M77" for b in Srv.sent("deleteMessage", "b0USER1")), "show() = send new + delete old (no editMessageText)")
ok(not Srv.sent("editMessageText"), "editMessageText never used")

# ---- unavailable shop → friendly, no fabricated data
clear(); pump(inline("b0USER1", "m:prod")); ok("در حال آماده‌سازی" in last("b0USER1")["text"], "shop not configured → friendly message")
# ---- claim code (no username binding)
pump(new_msg("b0ADMIN", "/start"))
ok(store.admin_id() is None, "username 'example_owner' does NOT bind on Rubika")
code = bot.ensure_claim_code(); ok(bool(code), "claim code generated")
clear(); pump(new_msg("b0USER1", "/claim WRONG")); ok("نادرست" in last("b0USER1")["text"] and store.admin_id() is None, "wrong code rejected")
ok(Srv.sent("deleteMessage", "b0USER1"), "claim message deleted")
pump(new_msg("b0ADMIN", "/claim " + code)); aid = store.snapshot()["idmap"]["c2i"]["b0ADMIN"]
ok(store.admin_id() == aid, "correct code binds admin")
clear(); pump(new_msg("b0ADMIN", "/admin"))
ok(any(b["id"] == "a:api" for b in buttons("b0ADMIN")), "admin panel with API connection button")

# ---- API key flow: message deleted, masked
SECRET = "rk_live_SECRET_9876543210"
clear(); pump(inline("b0ADMIN", "a:api:key")); pump(new_msg("b0ADMIN", SECRET))
ok(any(b.get("message_id") for b in Srv.sent("deleteMessage", "b0ADMIN")), "API key message deleted")
ok(SECRET not in json.dumps(Srv.calls, ensure_ascii=False).replace('"text": "' + SECRET, ""), "key never echoed")
ok(store.api_config()["api_key"] == SECRET, "key stored (shared api_config.json)")

# ---- real shop via fake website: photo upload path + SSRF guard
store.update_api(base_url="https://shop.example.test/api", enabled=True)
class FR:
    def __init__(s, code, js=None, hdr=None, content=b"", ct=""): s.status_code, s._j, s.headers, s.content = code, js, hdr or ({"Content-Type": ct} if ct else {}), content or b"{}"
    def json(s): return s._j
    def iter_content(s, n): yield s.content
    def close(s): pass
def fake_get(url, **k):
    if url.startswith("https://shop.example.test/api/products/7"):
        return FR(200, {"data": {"id": 7, "name": "کرم شب", "price": 250000, "image": "https://cdn.example/a.jpg", "url": "https://shop.example.test/p/7"}})
    if url.startswith("https://cdn.example/"): return FR(200, hdr={"Content-Type": "image/jpeg"}, content=b"\xff\xd8JPEG")
    if url.startswith("https://shop.example.test/api/products"):
        return FR(200, {"data": [{"id": 7, "name": "کرم شب", "price": 250000}], "meta": {"last_page": 1}})
    return FR(404, {})
requests.get = fake_get
transport.socket.getaddrinfo = lambda h, p, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
store.update_user(uid1, nav={"k": "c", "c": None, "cn": "", "p": 1})
clear(); pump(inline("b0USER1", "pn:1"))
ok(any(b["id"] == "pd:7" for b in buttons("b0USER1")), "product list rendered as keypad")
clear(); pump(inline("b0USER1", "pd:7"))
sf = Srv.sent("sendFile", "b0USER1")
ok(sf and sf[0]["file_id"] == "FILE-1" and Srv.uploaded, "product photo: downloaded → requestSendFile → upload → sendFile")
ok(any(b["type"] == "Link" for r in sf[0]["inline_keypad"]["rows"] for b in r["buttons"]), "site link as Link button")
transport.socket.getaddrinfo = lambda h, p, *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))]
ok(tg.T._image_file_id("https://evil.example/x.jpg") is None and not tg.T._public_http_url if False else transport._public_http_url("http://internal/x") is False, "SSRF guard blocks private IPs")
transport.socket.getaddrinfo = lambda h, p, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
Srv.fail["sendFile"] = "boom"; clear(); pump(inline("b0USER1", "pd:7"))
ok(Srv.sent("sendMessage", "b0USER1"), "photo failure degrades to text card")
Srv.fail.pop("sendFile")

# ---- link button degradation
Srv.fail["sendMessage"] = None
class Once:
    n = 0
orig = fake_post
def flaky(url, json=None, **k):
    if url.endswith("/sendMessage") and json and "metadata" in json or (url.endswith("/sendMessage") and json and any(b.get("type") == "Link" for r in (json.get("inline_keypad") or {}).get("rows", []) for b in r["buttons"])):
        Srv.calls.append(("sendMessage", json)); return R({"status": "INVALID_INPUT", "status_det": "bad metadata"}, 400)
    return orig(url, json=json, **k)
Srv.fail.pop("sendMessage"); tg.T.sess.post = flaky
clear(); r = tg.send(uid1, "<b>x</b>", tg.kb([[tg.btn("go", url="https://a.b/c")]]))
ok(r and any("https://a.b/c" in b.get("text", "") for b in Srv.sent("sendMessage")[1:]), "rejected metadata/Link → retried as plain text with URL")
tg.T.sess.post = orig

# ---- contact (unverified) never used to store phone
pump(new_msg("b0USER1", None, contact_message={"phone_number": "09121234567", "first_name": "Sara"}))
ok(store.get_user(uid1).get("phone") is None, "shared contact does not link a phone (ownership unverifiable)")
# tracking still works: order no + typed phone verified against the order
os.environ["SHOP_USE_MOCK"] = "1"; store.update_api(base_url=None, api_key=None)
clear(); pump(inline("b0USER1", "m:trk")); pump(new_msg("b0USER1", "MOCK-1002")); pump(new_msg("b0USER1", "09120000000"))
ok("ارسال شد" in last("b0USER1")["text"], "order tracking by number + phone works")
pump(inline("b0USER1", "m:ord")); ok("در دسترس نیست" in last("b0USER1")["text"], "my-orders callback explains it's unavailable")
del os.environ["SHOP_USE_MOCK"]

# ---- support forward (copy = re-send text) + admin reply
clear(); pump(inline("b0USER1", "m:sup")); pump(new_msg("b0USER1", "سلام سفارشم"))
ok(any("سلام سفارشم" in b.get("text", "") for b in Srv.sent("sendMessage", "b0ADMIN")), "support message copied to admin as text")
tid = list(store.snapshot()["tickets"])[0]
clear(); pump(inline("b0ADMIN", f"a:tr:{tid}")); pump(new_msg("b0ADMIN", "پیگیری می‌کنم"))
ok(any("پیگیری می‌کنم" in b.get("text", "") for b in Srv.sent("sendMessage", "b0USER1")), "admin reply delivered")
# native reply routing
clear(); pump(new_msg("b0USER1", "ممنون")); mp = store.snapshot()["admin_map"]; key = list(mp)[-1]
clear(); pump(new_msg("b0ADMIN", "بفرمایید", reply_to_message_id=key))
ok(any("بفرمایید" in b.get("text", "") for b in Srv.sent("sendMessage", "b0USER1")), "reply_to_message_id routes back to the user")

# ---- broadcast + notify
clear(); pump(inline("b0ADMIN", "a:bc")); pump(new_msg("b0ADMIN", "🌸 تخفیف")); pump(inline("b0ADMIN", "a:bcy"))
ok(any("تخفیف" in b.get("text", "") for b in Srv.sent("sendMessage", "b0USER1")), "broadcast delivered (text re-send)")
notify.subscribe(uid1, "A1"); clear(); res = notify.notify_user_order_update("A1", "shipped")
ok(res["sent"] == 1 and "ارسال شد" in last("b0USER1")["text"], "notify_user_order_update via Rubika")

# ---- dead chat flag
pump({"type": "StoppedBot", "chat_id": "b0USER1"}); ok(store.get_user(uid1).get("blocked"), "StoppedBot → user marked blocked")
# ---- groups ignored, own bot messages ignored
ok(tg.T.normalize(new_msg("g0GROUP", "hi")) == [] and tg.T.normalize({"type": "NewMessage", "chat_id": "b0USER1", "new_message": {"sender_type": "Bot", "message_id": "z"}}) == [], "groups / bot echoes ignored")

# ---- admin extensions on Rubika (users / admins / ban; ids are mapped ints)
uid1 = store.snapshot()["idmap"]["c2i"]["b0USER1"]
clear(); pump(inline("b0ADMIN", "a:home"))
ok(all(any(b["id"] == c for b in buttons("b0ADMIN")) for c in ("a:us:1", "a:o", "a:ad")), "rubika: admin home has users/orders/admins")
clear(); pump(inline("b0ADMIN", "a:us:1")); ok(any(b["id"] == f"a:uc:{uid1}" for b in buttons("b0ADMIN")), "rubika: users list")
clear(); pump(inline("b0ADMIN", f"a:uc:{uid1}")); ok(str(uid1) in last("b0ADMIN")["text"], "rubika: user card")
pump(inline("b0ADMIN", f"a:ub:{uid1}")); ok(store.get_user(uid1).get("banned"), "rubika: ban")
clear(); pump(new_msg("b0USER1", "/start")); ok(not Srv.sent("sendMessage", "b0USER1") or "محدود" in last("b0USER1")["text"], "rubika: banned user gets only the notice")
pump(inline("b0ADMIN", f"a:ub:{uid1}")); ok(not store.get_user(uid1).get("banned"), "rubika: unban")
pump(inline("b0ADMIN", "a:ada")); pump(new_msg("b0ADMIN", str(uid1))); ok(uid1 in store.snapshot()["admins"], "rubika: add admin by mapped id (+notify)")
clear(); pump(inline("b0USER1", "a:api")); ok("فقط برای مالک" in last("b0USER1")["text"], "rubika: extra admin denied owner-only")

# ---- role toggle from the user card (Rubika)
uid2 = store.snapshot()["idmap"]["c2i"]["b0USER1"]
pump(inline("b0ADMIN", f"a:urm:{uid2}")); pump(inline("b0ADMIN", f"a:urmy:{uid2}")); ok(uid2 not in store.snapshot()["admins"], "rubika: remove admin via card")
clear(); pump(inline("b0ADMIN", f"a:uc:{uid2}")); ok(any(b["id"] == f"a:uad:{uid2}" for b in buttons("b0ADMIN")), "rubika: card shows Make admin")
pump(inline("b0ADMIN", f"a:uad:{uid2}")); ok(uid2 in store.snapshot()["admins"], "rubika: make admin via card")
clear(); pump(inline("b0USER1", f"a:urm:{uid2}")); ok("فقط برای مالک" in last("b0USER1")["text"] and uid2 in store.snapshot()["admins"], "rubika: extra admin can't use role toggle")

# ---- redaction
import io, logging
buf = io.StringIO(); h = logging.StreamHandler(buf); h.setFormatter(tg.RedactFormatter("%(message)s"))
lg = logging.getLogger("lk"); lg.addHandler(h); lg.setLevel(logging.INFO); tg.add_secret(SECRET)
lg.info("t=%s k=%s", os.environ["SHOP_RUBIKA_BOT_TOKEN"], SECRET)
ok(os.environ["SHOP_RUBIKA_BOT_TOKEN"] not in buf.getvalue() and SECRET not in buf.getvalue(), "log redaction covers the Rubika token")
def boom(url, **k): raise requests.ConnectionError("x " + url)
tg.T.sess.post = boom
try: tg.T.call("getMe", {})
except tg.ApiError as e: ok("RUBIKA-TOKEN" not in str(e), "network error has no token (URL carries the token)")
import stat
ok(stat.S_IMODE(os.stat(store.PATH).st_mode) == 0o600, "state_rubika.json mode 600")
print("\nFAILURES:", ok.f); sys.exit(1 if ok.f else 0)

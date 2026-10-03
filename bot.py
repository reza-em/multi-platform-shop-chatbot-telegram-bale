#!/usr/bin/env python3
"""Shop cosmetics store – customer-service / shop bot. Long polling, plain requests.
Platform independent: Telegram (default), Bale, Rubika are selected with env SHOP_PLATFORM and
implemented in transport.py; this file only speaks the shared (Telegram-shaped) dialect."""
import os, sys, json, time, re, logging, fcntl, html, copy, hashlib, hmac, secrets
import urllib.parse

import tg
if not tg.TOKEN and __name__ == "__main__":
    print(f"{tg.TOKEN_ENV} not set", file=sys.stderr)
    sys.exit(1)

import store, texts, shop_api, notify, ui
from tg import btn, kb, pairs, ApiError, safe
from texts import tr
from ui import esc, out, lang_of

log = logging.getLogger("bot")
BASE = os.path.dirname(os.path.abspath(__file__))
ADMIN_USERNAME = os.environ.get("OWNER_USERNAME", "example_owner").lower()
BOT_USERNAME = ""
MAX_TEXT = 3500

# ============================================================================ state helpers
def now():
    return int(time.time())


def touch_user(tgu):
    """Create/refresh the user record. Returns (is_new, user)."""
    uid = str(tgu["id"])
    with store.transaction() as st:
        is_new = uid not in st["users"]
        u = st["users"].setdefault(uid, {"first_seen": now()})
        u["name"] = (tgu.get("first_name") or "")[:64]
        u["username"] = tgu.get("username") or ""
        u["last_seen"] = now()
        u.pop("blocked", None)          # they wrote to us, so they're reachable
        return is_new, copy.deepcopy(u)


def bind_admin_if_needed(tgu):
    """Auto-bind the admin numeric id the first time @example_owner writes. True if newly bound.
    Only on platforms where a username is trusted (tg.T.username_admin_bind); Bale/Rubika use /claim."""
    if not tg.T.username_admin_bind:
        return False
    if (tgu.get("username") or "").lower() != ADMIN_USERNAME:
        return False
    with store.transaction() as st:
        if st.get("admin_id"):
            return False
        st["admin_id"] = int(tgu["id"])
        return True


def is_owner(uid):
    a = store.admin_id()
    return a is not None and int(uid) == int(a)


def is_admin(uid):
    """Owner OR extra admin. Owner-only actions must use is_owner() (enforced in the handlers)."""
    if is_owner(uid):
        return True
    with store.transaction(write=False) as st:
        return int(uid) in st.get("admins", [])


def set_await(uid, kind, data=None):
    store.update_user(uid, awaiting=kind, await_data=data)


def clear_await(uid):
    store.update_user(uid, awaiting=None, await_data=None)


# ============================================================================ shop access
def shop(chat_id, lang, fn, mid=None):
    """Run fn(api) with friendly errors. Returns (result, api) or (None, None) after replying."""
    api = shop_api.get_api()
    try:
        return fn(api), api
    except shop_api.ShopNotConfigured:
        ui.show(chat_id, mid, lang, tr(lang, "shop_setup"), ui.menu_only(lang))
    except shop_api.VerificationUnavailable:
        ui.show(chat_id, mid, lang, tr(lang, "order_noverify"), ui.menu_only(lang))
    except shop_api.ShopError as e:
        log.warning("shop error: %s", safe(e))
        ui.show(chat_id, mid, lang, tr(lang, "shop_error"), ui.menu_only(lang))
    except Exception as e:      # adapter bug / unexpected payload: never crash, never leak details
        log.warning("shop unexpected error: %s", type(e).__name__)
        ui.show(chat_id, mid, lang, tr(lang, "shop_error"), ui.menu_only(lang))
    return None, None


def banner(lang, api):
    return tr(lang, "mock_banner") if getattr(api, "is_mock", False) else ""


# ============================================================================ menus / screens
def show_menu(chat_id, uid, lang, mid=None, title="menu_title"):
    ui.show(chat_id, mid, lang, tr(lang, title), ui.main_menu(lang, is_admin(uid)))


def send_welcome(chat_id, uid, lang, name):
    ui.send(chat_id, lang, tr(lang, "welcome", name=esc(name)), ui.main_menu(lang, is_admin(uid)))


def show_categories(chat_id, uid, lang, mid=None):
    cats, api = shop(chat_id, lang, lambda a: a.list_categories(), mid)
    if api is None:
        return
    cats = cats[:40]
    store.update_user(uid, cats=[[c.id, c.name] for c in cats], nav=None)
    rows = pairs([btn("🌸 " + c.name[:28], f"pc:{i}") for i, c in enumerate(cats)])
    rows.insert(0, [btn(tr(lang, "cat_all"), "pc:-1")])
    rows.append(ui.menu_row(lang))
    text = banner(lang, api) + (tr(lang, "cats_title") if cats else tr(lang, "cats_empty"))
    ui.show(chat_id, mid, lang, text, kb(rows))


def show_product_list(chat_id, uid, lang, page, mid=None):
    nav = store.get_user(uid).get("nav")
    if not nav:
        ui.show(chat_id, mid, lang, tr(lang, "expired"), ui.menu_only(lang)); return
    if nav["k"] == "c":
        res, api = shop(chat_id, lang, lambda a: a.list_products(nav.get("c"), page), mid)
    else:
        res, api = shop(chat_id, lang, lambda a: a.search_products(nav["q"], page), mid)
    if api is None:
        return
    page = res.page
    store.update_user(uid, nav={**nav, "p": page})
    if not res.items:
        if nav["k"] == "s":
            text = tr(lang, "search_empty", q=esc(nav["q"]))
        else:
            text = tr(lang, "prods_empty")
        rows = [[btn(tr(lang, "b_search"), "m:srch"), btn(tr(lang, "b_cats"), "m:prod")], ui.menu_row(lang)]
        ui.show(chat_id, mid, lang, banner(lang, api) + text, kb(rows)); return
    if nav["k"] == "c":
        title = tr(lang, "prods_title", cat=esc(nav.get("cn") or tr(lang, "cat_all")), page=ui.fmt_num(lang, page), pages=ui.fmt_num(lang, res.pages))
    else:
        title = tr(lang, "search_title", q=esc(nav["q"]), page=ui.fmt_num(lang, page), pages=ui.fmt_num(lang, res.pages))
    rows = pairs([btn("🧴 " + p.name[:28], f"pd:{p.id}") for p in res.items if len(f"pd:{p.id}".encode()) <= 64])
    nav_row = []
    if page > 1:
        nav_row.append(btn(tr(lang, "b_prev"), f"pn:{page - 1}"))
    if page < res.pages:
        nav_row.append(btn(tr(lang, "b_next"), f"pn:{page + 1}"))
    if nav_row:
        rows.append(nav_row)
    rows.append([btn(tr(lang, "b_cats"), "m:prod"), btn(tr(lang, "b_menu"), "m:menu")])
    ui.show(chat_id, mid, lang, banner(lang, api) + title, kb(rows))


def show_product(chat_id, uid, lang, pid, mid=None):
    p, api = shop(chat_id, lang, lambda a: a.get_product(pid), mid)
    if api is None:
        return
    nav = store.get_user(uid).get("nav")
    back = [btn(tr(lang, "b_back"), f"pn:{nav.get('p', 1)}")] if nav else []
    if not p:
        ui.show(chat_id, mid, lang, tr(lang, "product_gone"), kb([back + ui.menu_row(lang)])); return
    lines = [banner(lang, api) + "🌸 <b>" + esc(p.name) + "</b>", "",
             f"{tr(lang, 'price')}: <b>{ui.fmt_price(lang, p.price, p.currency)}</b>"]
    if p.in_stock is not None:
        lines.append(tr(lang, "in_stock" if p.in_stock else "out_stock"))
    if p.description:
        lines += ["", "✨ " + ui.clean_html(p.description, 350)]
    text = "\n".join(lines)
    rows = []
    if p.url and re.match(r"https?://", p.url):
        rows.append([btn(tr(lang, "b_site"), url=p.url)])
    rows.append(back + ui.menu_row(lang))
    markup = kb(rows)
    if p.image and re.match(r"https?://", p.image):
        cap = out(lang, text)
        if len(cap) > 1000:
            cap = cap[:997] + "…"
        if tg.send_photo(chat_id, p.image, cap, markup):
            return
    ui.send(chat_id, lang, text, markup)


# ---- tracking -------------------------------------------------------------------------------
ORDER_RE = re.compile(r"^[\w\-#./]{1,40}$")


def order_card(lang, o, api):
    lines = [banner(lang, api) + tr(lang, "order_card", num=esc(o.number), status=texts.status_label(lang, o.status, o.status_raw))]
    if o.date:
        lines.append(f"{tr(lang, 'o_date')}: {esc(o.date[:32])}")
    if o.total not in (None, ""):
        lines.append(f"{tr(lang, 'o_total')}: {ui.fmt_price(lang, o.total, o.currency)}")
    if o.tracking:
        lines.append(f"{tr(lang, 'o_track')}: <code>{esc(o.tracking[:60])}</code>")
    if o.items:
        lines.append(tr(lang, "o_items") + ":")
        for it in o.items[:8]:
            q = f" × {ui.fmt_num(lang, it.qty)}" if it.qty not in (None, "") else ""
            lines.append("  • " + esc(it.name[:60]) + q)
    return "\n".join(lines)


def phone_ok(text):
    d = shop_api.norm_phone(text)
    return d if d and d.startswith("9") else ""


def canonical_phone(d10):
    return "0" + d10


def rate_limited(uid):
    u = store.get_user(uid)
    recent = [t for t in u.get("fails", []) if t > now() - 600]
    return len(recent) >= 5


def note_fail(uid):
    u = store.get_user(uid)
    recent = [t for t in u.get("fails", []) if t > now() - 600] + [now()]
    store.update_user(uid, fails=recent[-10:])


def do_track(chat_id, uid, lang, number, phone):
    """Verify + show. Returns True if shown."""
    o, api = shop(chat_id, lang, lambda a: a.get_order(number, phone))
    if api is None:
        return None
    if not o:
        note_fail(uid)
        return False
    notify.subscribe(uid, o.number)
    log_order(uid, o)
    ui.send(chat_id, lang, order_card(lang, o, api),
            kb([[btn(tr(lang, "b_track"), "m:trk"), btn(tr(lang, "b_menu"), "m:menu")]]))
    return True


def contact_keyboard(lang):
    return tg.contact_keyboard(tr(lang, "b_share"), tr(lang, "b_menu"))


def show_my_orders(chat_id, uid, lang, mid=None):
    phone = store.get_user(uid).get("phone")
    if not phone:
        ui.send(chat_id, lang, tr(lang, "orders_link"), contact_keyboard(lang)); return
    orders, api = shop(chat_id, lang, lambda a: a.list_user_orders(uid, phone), mid)
    if api is None:
        return
    text = banner(lang, api) + tr(lang, "orders_title", phone=esc(phone))
    rows = []
    if not orders:
        text += "\n\n" + tr(lang, "orders_empty")
    else:
        shown = orders[:10]
        for o in shown:
            notify.subscribe(uid, o.number)
        rows = pairs([btn(f"{texts.status_label(lang, o.status, o.status_raw).split(' ')[0]} {o.number[:22]}", f"ov:{o.number}")
                      for o in shown if len(f"ov:{o.number}".encode()) <= 64])
        if len(orders) > 10:
            text += "\n" + tr(lang, "orders_more", n=len(orders) - 10)
    rows.append([btn(tr(lang, "b_unlink"), "m:unlink"), btn(tr(lang, "b_menu"), "m:menu")])
    ui.show(chat_id, mid, lang, text, kb(rows))


# ---- support --------------------------------------------------------------------------------
def who_label(uid):
    u = store.get_user(uid)
    s = esc(u.get("name") or "?")
    if u.get("username"):
        s += " @" + esc(u["username"])
    return f"{s} (<code>{uid}</code>)"


def preview_of(msg):
    t = msg.get("text") or msg.get("caption") or ""
    if t:
        return t[:300]
    for k in ("photo", "video", "voice", "audio", "document", "sticker", "video_note", "animation", "location", "contact"):
        if k in msg:
            return f"[{k}]"
    return "[message]"


def support_message(chat_id, uid, lang, msg):
    """User writes in support mode: log into a ticket and forward to the admin."""
    with store.transaction() as st:
        t = next((t for t in sorted(st["tickets"].values(), key=lambda x: -x["id"]) if t["uid"] == uid and t["status"] == "open"), None)
        new = t is None
        if new:
            tid = st["next_ticket"]
            st["next_ticket"] += 1
            t = st["tickets"][str(tid)] = {"id": tid, "uid": uid, "status": "open", "created": now(), "msgs": []}
        t["msgs"].append({"from": "user", "text": preview_of(msg), "ts": now()})
        t["msgs"] = t["msgs"][-30:]
        t["updated"] = now()
        tid = t["id"]
    adm = store.admin_id()
    if not adm:
        ui.send(chat_id, lang, tr(lang, "support_saved"), ui.menu_only(lang)); return
    try:
        head = tg.send(adm, out("fa", tr("fa", "a_t_new", id=tid, who=who_label(uid))), ticket_kb("fa", tid), raise_errors=True)
        cp = tg.copy_message(adm, chat_id, msg["message_id"], raise_errors=True, msg=msg)
        with store.transaction() as st:
            for m in (head, cp):
                if m and m.get("message_id") is not None:
                    st["admin_map"][str(m["message_id"])] = tid
            if len(st["admin_map"]) > 2000:
                for k in list(st["admin_map"])[:500]:
                    del st["admin_map"][k]
        ui.send(chat_id, lang, tr(lang, "support_sent"), ui.menu_only(lang))
    except ApiError as e:
        log.warning("forward to admin failed: %s", safe(e))
        ui.send(chat_id, lang, tr(lang, "support_saved"), ui.menu_only(lang))


def ticket_kb(lang, tid, closed=False):
    return kb([[btn(tr(lang, "a_t_reply"), f"a:tr:{tid}"),
                btn(tr(lang, "a_t_reopen" if closed else "a_t_close"), f"a:{'to' if closed else 'tc'}:{tid}")],
               [btn(tr(lang, "a_t_inbox"), "a:inbox"), btn(tr(lang, "b_admin"), "a:home")]])


def admin_reply_to_ticket(chat_id, tid, msg):
    lang = lang_of(chat_id)
    with store.transaction() as st:
        t = st["tickets"].get(str(tid))
        if not t:
            ui.send(chat_id, lang, tr(lang, "a_t_none")); return
        uid = t["uid"]
        t["msgs"].append({"from": "admin", "text": preview_of(msg), "ts": now()})
        t["msgs"] = t["msgs"][-30:]
        t["status"] = "open"
        t["updated"] = now()
    ulang = lang_of(uid)
    try:
        tg.send(uid, out(ulang, tr(ulang, "support_reply")), raise_errors=True)
        tg.copy_message(uid, chat_id, msg["message_id"], raise_errors=True, msg=msg)
        # keep the user in support mode so they can answer back
        store.update_user(uid, awaiting="support")
        ui.send(chat_id, lang, tr(lang, "a_t_sent"), ticket_kb(lang, tid))
    except ApiError as e:
        log.warning("reply to user failed: %s", safe(e))
        if tg.is_dead_chat(e):
            store.update_user(uid, blocked=True)
        ui.send(chat_id, lang, tr(lang, "a_t_fail"), ticket_kb(lang, tid))


# ---- faq / about ------------------------------------------------------------------------------
def editable_text(key_base, lang):
    return store.get_text(f"{key_base}_{lang}") or store.get_text(f"{key_base}_{'en' if lang == 'fa' else 'fa'}")


def show_faq(chat_id, uid, lang, mid=None):
    t = editable_text("faq", lang)
    body = tr(lang, "faq_title") + esc(t[:MAX_TEXT]) if t else tr(lang, "faq_empty")
    ui.show(chat_id, mid, lang, body, kb([[btn(tr(lang, "b_support"), "m:sup"), btn(tr(lang, "b_menu"), "m:menu")]]))


def show_about(chat_id, uid, lang, mid=None):
    t = editable_text("about", lang)
    body = tr(lang, "about_title") + esc(t[:MAX_TEXT]) if t else tr(lang, "about_empty")
    ui.show(chat_id, mid, lang, body, kb([[btn(tr(lang, "b_support"), "m:sup"), btn(tr(lang, "b_menu"), "m:menu")]]))


# ============================================================================ admin panel
def admin_home(chat_id, uid, lang, mid=None):
    st = store.snapshot()
    n_open = sum(1 for t in st["tickets"].values() if t["status"] == "open")
    b = [btn(tr(lang, "a_b_users"), "a:us:1"), btn(tr(lang, "a_b_orders"), "a:o"),
         btn(tr(lang, "a_b_inbox", n=n_open), "a:inbox")]
    if is_owner(uid):
        b.append(btn(tr(lang, "a_b_stats"), "a:stats"))
    b += [btn(tr(lang, "a_b_faq"), "a:tm:faq"), btn(tr(lang, "a_b_about"), "a:tm:about")]
    if is_owner(uid):
        b += [btn(tr(lang, "a_b_bc"), "a:bc"), btn(tr(lang, "a_b_api"), "a:api"), btn(tr(lang, "a_b_admins"), "a:ad")]
    rows = pairs(b)
    rows.append(ui.menu_row(lang))
    ui.show(chat_id, mid, lang, tr(lang, "a_home"), kb(rows))


def admin_stats(chat_id, lang, mid):
    st = store.snapshot()
    us = st["users"].values()
    api = {"real": "a_api_real", "mock": "a_api_mock", "none": "a_api_none"}[shop_api.api_state()]
    text = tr(lang, "a_stats", users=len(st["users"]), active=sum(1 for u in us if u.get("last_seen", 0) > now() - 7 * 86400),
              phones=sum(1 for u in us if u.get("phone")), blocked=sum(1 for u in us if u.get("blocked")),
              open_t=sum(1 for t in st["tickets"].values() if t["status"] == "open"), all_t=len(st["tickets"]),
              bc=st.get("broadcasts", 0), api=tr(lang, api))
    ui.show(chat_id, mid, lang, text, kb([[btn(tr(lang, "b_back"), "a:home")]]))


def admin_text_menu(chat_id, lang, mid, base):
    unset = tr(lang, "a_text_unset")
    fa, en = store.get_text(f"{base}_fa"), store.get_text(f"{base}_en")
    text = tr(lang, "a_text_menu", title=tr(lang, f"a_title_{base}"),
              fa=esc(fa[:1200]) if fa else unset, en=esc(en[:1200]) if en else unset)
    rows = [[btn(tr(lang, "a_edit_fa"), f"a:te:{base}_fa"), btn(tr(lang, "a_edit_en"), f"a:te:{base}_en")],
            [btn(tr(lang, "a_reset_fa"), f"a:tz:{base}_fa"), btn(tr(lang, "a_reset_en"), f"a:tz:{base}_en")],
            [btn(tr(lang, "b_back"), "a:home")]]
    ui.show(chat_id, mid, lang, text, kb(rows))


def admin_inbox(chat_id, lang, mid):
    st = store.snapshot()
    ts = sorted(st["tickets"].values(), key=lambda t: (t["status"] != "open", -t.get("updated", t["created"])))[:10]
    n_open = sum(1 for t in st["tickets"].values() if t["status"] == "open")
    if not ts:
        ui.show(chat_id, mid, lang, tr(lang, "a_inbox_empty"), kb([[btn(tr(lang, "b_back"), "a:home")]])); return
    rows = pairs([btn(("🟢" if t["status"] == "open" else "⚪️") + f" #{t['id']} " +
                      (st["users"].get(str(t["uid"]), {}).get("name") or "?")[:14], f"a:tv:{t['id']}") for t in ts])
    rows.append([btn(tr(lang, "b_back"), "a:home")])
    ui.show(chat_id, mid, lang, tr(lang, "a_inbox", n=n_open), kb(rows))


def admin_ticket(chat_id, lang, mid, tid):
    t = store.snapshot()["tickets"].get(str(tid))
    if not t:
        ui.show(chat_id, mid, lang, tr(lang, "a_t_none"), kb([[btn(tr(lang, "a_t_inbox"), "a:inbox")]])); return
    msgs = "\n".join(("👤 " if m["from"] == "user" else "🛠 ") + esc(m["text"][:200]) for m in t["msgs"][-6:])
    text = tr(lang, "a_ticket", id=t["id"], status=tr(lang, "a_t_open" if t["status"] == "open" else "a_t_closed"),
              who=who_label(t["uid"]), msgs=msgs)
    ui.show(chat_id, mid, lang, text, ticket_kb(lang, tid, closed=t["status"] != "open"))


def broadcast_recipients():
    st = store.snapshot()
    return [int(k) for k, u in st["users"].items() if not u.get("blocked") and not u.get("banned")]


def run_broadcast(admin_chat, lang):
    st = store.snapshot()
    pb = st.get("pending_broadcast")
    if not pb:
        ui.send(admin_chat, lang, tr(lang, "a_bc_gone")); return
    with store.transaction() as s2:
        s2["pending_broadcast"] = None
        s2["broadcasts"] = s2.get("broadcasts", 0) + 1
    rec = broadcast_recipients()
    if not rec:
        ui.send(admin_chat, lang, tr(lang, "a_bc_none")); return
    ui.send(admin_chat, lang, tr(lang, "a_bc_started"))
    ok = fail = 0
    for uid in rec:
        try:
            tg.copy_message(uid, pb["chat_id"], pb["message_id"], raise_errors=True, msg=pb.get("msg"))
            ok += 1
        except ApiError as e:
            fail += 1
            if tg.is_dead_chat(e):
                store.update_user(uid, blocked=True)
        time.sleep(0.05)
    ui.send(admin_chat, lang, tr(lang, "a_bc_done", ok=ok, fail=fail), kb([[btn(tr(lang, "b_admin"), "a:home")]]))


# ---- users / orders / admins sections ---------------------------------------------------------
OWNER_ONLY = {"stats", "bc", "bcy", "bcn", "api", "ad", "ada", "adx", "adxy", "ow", "owy", "uad", "urm", "urmy"}
U_PER, O_PER = 8, 6
FILTERS = ["pending", "paid", "processing", "packed", "shipped", "delivered", "canceled"]


def back_home(lang, cb="a:home", key="b_back"):
    return [btn(tr(lang, key), cb)]


def user_tickets(st, uid):
    mine = [t for t in st["tickets"].values() if t["uid"] == int(uid)]
    return len(mine), sum(1 for t in mine if t["status"] == "open")


def user_label(st, uid):
    u = st["users"].get(str(uid), {})
    return (u.get("name") or "?")[:18] + (" @" + u["username"][:14] if u.get("username") else "")


def resolve_user(text):
    """'123456' or '@name' → uid of a user the bot has SEEN on this platform, else None."""
    t = (text or "").strip()
    users = store.snapshot()["users"]
    if re.fullmatch(r"-?\d{1,20}", t):
        return int(t) if t in users else None
    t = t.lstrip("@").lower()
    if re.fullmatch(r"[A-Za-z0-9_.]{2,64}", t):
        for k, u in users.items():
            if (u.get("username") or "").lower() == t:
                return int(k)
    return None


def match_users(q):
    q = q.strip().lstrip("@").lower()
    digits = shop_api.to_ascii_digits(q)
    res = []
    for k, u in store.snapshot()["users"].items():
        ph = re.sub(r"\D", "", u.get("phone") or "")
        hit = (q == k or (q and q in (u.get("username") or "").lower()) or (q and q in (u.get("name") or "").lower())
               or (len(re.sub(r"\D", "", digits)) >= 4 and re.sub(r"\D", "", digits)[-10:] in ph))
        if hit:
            res.append((u.get("last_seen", 0), k))
    return [k for _, k in sorted(res, reverse=True)]


def admin_users(chat_id, uid, lang, mid, page=1, query=None):
    st = store.snapshot()
    if query is not None:
        ids = match_users(query)[:U_PER]
        store.update_user(uid, ulist={"q": query, "p": 1})
        head = tr(lang, "a_us_found", q=esc(query[:40]), n=len(ids))
        pages = 1
    else:
        ids = [k for _, k in sorted(((u.get("last_seen", 0), k) for k, u in st["users"].items()), reverse=True)]
        pages = max(1, -(-len(ids) // U_PER))
        page = min(max(1, page), pages)
        ids = ids[(page - 1) * U_PER: page * U_PER]
        store.update_user(uid, ulist={"q": None, "p": page})
        head = tr(lang, "a_us_title", n=len(st["users"]), page=ui.fmt_num(lang, page), pages=ui.fmt_num(lang, pages))
    rows = pairs([btn(("🚫 " if st["users"][k].get("banned") else "👤 ") + user_label(st, k), f"a:uc:{k}") for k in ids])
    if query is None:
        nav = []
        if page > 1:
            nav.append(btn(tr(lang, "b_prev"), f"a:us:{page - 1}"))
        if page < pages:
            nav.append(btn(tr(lang, "b_next"), f"a:us:{page + 1}"))
        if nav:
            rows.append(nav)
    elif not ids:
        head += "\n" + tr(lang, "a_us_none")
    rows.append([btn(tr(lang, "a_us_search"), "a:uq")] + ([btn(tr(lang, "a_us_all"), "a:us:1")] if query is not None else []))
    rows.append(back_home(lang))
    ui.show(chat_id, mid, lang, head, kb(rows))


def user_card_text(lang, k):
    st = store.snapshot()
    u = st["users"].get(str(k), {})
    nt, no = user_tickets(st, k)
    role = "👑 " + tr(lang, "a_role_owner") if is_owner(int(k)) else "🛡 " + tr(lang, "a_role_admin") if is_admin(int(k)) else ""
    flags = []
    if u.get("banned"):
        flags.append(tr(lang, "a_us_banned"))
    if u.get("blocked"):
        flags.append(tr(lang, "a_us_blockedbot"))
    return tr(lang, "a_us_card", name=esc(u.get("name") or "?"), id=k, un=("@" + esc(u["username"])) if u.get("username") else "—",
              joined=ui.fmt_ts(lang, u.get("first_seen")), seen=ui.fmt_ts(lang, u.get("last_seen")),
              lg="فارسی" if u.get("lang") == "fa" else "English" if u.get("lang") == "en" else "—",
              phone=f"<code>{esc(u['phone'])}</code>" if u.get("phone") else "—", nt=nt, no=no,
              extra=("\n" + " · ".join(([role] if role else []) + flags)) if (role or flags) else "")


def admin_user_card(chat_id, lang, mid, k, viewer=None):
    u = store.snapshot()["users"].get(str(k))
    if not u:
        ui.show(chat_id, mid, lang, tr(lang, "a_us_gone"), kb([back_home(lang, "a:usb")])); return
    ban = [] if is_admin(int(k)) else [btn(tr(lang, "a_us_unban" if u.get("banned") else "a_us_ban"), f"a:ub:{k}")]
    rows = [[btn(tr(lang, "a_us_msg"), f"a:um:{k}")] + ban, [btn(tr(lang, "a_us_orders"), f"a:uo:{k}")]]
    if is_owner(viewer) and not is_owner(int(k)):          # role toggle: owner only, owner can't be demoted
        extra = int(k) in store.snapshot()["admins"]
        rows.append([btn(tr(lang, "a_us_rmadm" if extra else "a_us_mkadm"), f"a:{'urm' if extra else 'uad'}:{k}")])
    rows.append([btn(tr(lang, "b_back"), "a:usb"), btn(tr(lang, "b_admin"), "a:home")])
    ui.show(chat_id, mid, lang, user_card_text(lang, k), kb(rows))


def log_order(uid, o):
    """Local log of orders users looked up via the bot (verified lookups only)."""
    with store.transaction() as st:
        lg = [e for e in st["orders_log"] if not (e["n"] == o.number and e["uid"] == int(uid))]
        lg.append({"n": o.number, "uid": int(uid), "ts": now(), "s": o.status, "sr": (o.status_raw or "")[:40]})
        st["orders_log"] = lg[-500:]


def cbsafe(prefix, num):
    d = f"{prefix}{num}"
    return d if len(d.encode()) <= 64 else None


def order_btns(lang, orders):
    out_ = []
    for o in orders:
        d = cbsafe("a:ov:", o.number)
        if d:
            out_.append(btn(f"{texts.status_label(lang, o.status, o.status_raw).split(' ')[0]} {o.number[:22]}", d))
    return pairs(out_)


def admin_shop(chat_id, lang, mid, fn, back="a:o"):
    """Like shop(), but admin-flavoured messages (with the real reason) and admin navigation."""
    api = shop_api.get_api()
    bk = kb([[btn(tr(lang, "b_back"), back), btn(tr(lang, "b_admin"), "a:home")]])
    try:
        return fn(api), api
    except shop_api.ShopNotConfigured:
        ui.show(chat_id, mid, lang, tr(lang, "a_o_setup"), bk)
    except shop_api.ShopError as e:
        log.warning("admin shop error: %s", safe(e))
        ui.show(chat_id, mid, lang, tr(lang, "a_o_err", err=esc(str(e)[:120])), bk)
    except Exception as e:
        log.warning("admin shop unexpected error: %s", type(e).__name__)
        ui.show(chat_id, mid, lang, tr(lang, "a_o_err", err=esc(type(e).__name__)), bk)
    return None, None


def admin_orders(chat_id, uid, lang, mid, page=1):
    """Recent orders from the shop API. If the API isn't configured / the list endpoint fails: a friendly
    message (never made-up data) that still offers search-by-number and the local lookup log."""
    flt = store.get_user(uid).get("ofilter")
    fallback = kb([[btn(tr(lang, "a_o_search"), "a:oq"), btn(tr(lang, "a_o_log"), "a:ol")], [btn(tr(lang, "b_admin"), "a:home")]])
    api = shop_api.get_api()
    try:
        res = api.list_orders(page, flt)
    except shop_api.ShopNotConfigured:
        ui.show(chat_id, mid, lang, tr(lang, "a_o_setup"), fallback); return
    except shop_api.ShopError as e:
        log.warning("admin orders list failed: %s", safe(e))
        ui.show(chat_id, mid, lang, tr(lang, "a_o_err", err=esc(str(e)[:120])), fallback); return
    except Exception as e:
        log.warning("admin orders list unexpected: %s", type(e).__name__)
        ui.show(chat_id, mid, lang, tr(lang, "a_o_err", err=esc(type(e).__name__)), fallback); return
    page = res.page
    head = banner(lang, api) + tr(lang, "a_o_title", page=ui.fmt_num(lang, page), pages=ui.fmt_num(lang, res.pages),
                                  flt=texts.status_label(lang, flt) if flt else tr(lang, "a_o_all"))
    rows = order_btns(lang, res.items[:O_PER * 2])
    if not res.items:
        head += "\n" + tr(lang, "a_o_empty")
    nav = []
    if page > 1:
        nav.append(btn(tr(lang, "b_prev"), f"a:on:{page - 1}"))
    if page < res.pages:
        nav.append(btn(tr(lang, "b_next"), f"a:on:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn(tr(lang, "a_o_search"), "a:oq"), btn(tr(lang, "a_o_filter"), "a:of")])
    rows.append([btn(tr(lang, "a_o_log"), "a:ol"), btn(tr(lang, "b_admin"), "a:home")])
    ui.show(chat_id, mid, lang, head, kb(rows))


admin_orders_entry = admin_orders


def admin_order_filter(chat_id, lang, mid):
    rows = pairs([btn(tr(lang, "a_o_all"), "a:ofs:all")] + [btn(texts.status_label(lang, k), f"a:ofs:{k}") for k in FILTERS])
    rows.append(back_home(lang, "a:o"))
    ui.show(chat_id, mid, lang, tr(lang, "a_o_filter_ask"), kb(rows))


def find_bot_users(o):
    """Bot users linked to an order: via the local lookup log, or a matching linked phone."""
    st = store.snapshot()
    ids = [e["uid"] for e in st["orders_log"] if e["n"] == o.number]
    if o.phone:
        ids += [int(k) for k, u in st["users"].items() if u.get("phone") and shop_api.phones_match(u["phone"], o.phone)]
    seen = []
    for i in ids:
        if i not in seen and str(i) in st["users"]:
            seen.append(i)
    return seen[:4]


def admin_order_detail(chat_id, uid, lang, mid, number, back="a:o"):
    o, api = admin_shop(chat_id, lang, mid, lambda a: a.find_order(number), back=back)
    if api is None:
        return
    if not o:
        ui.show(chat_id, mid, lang, tr(lang, "a_o_notfound", n=esc(number[:40])), kb([[btn(tr(lang, "a_o_search"), "a:oq")], back_home(lang, back)])); return
    lines = [banner(lang, api) + tr(lang, "order_card", num=esc(o.number), status=texts.status_label(lang, o.status, o.status_raw))]
    if o.date:
        lines.append(f"{tr(lang, 'o_date')}: {esc(o.date[:32])}")
    if o.total not in (None, ""):
        lines.append(f"{tr(lang, 'o_total')}: {ui.fmt_price(lang, o.total, o.currency)}")
    if o.tracking:
        lines.append(f"{tr(lang, 'o_track')}: <code>{esc(o.tracking[:60])}</code>")
    if o.customer:
        lines.append(f"{tr(lang, 'a_o_cust')}: {esc(o.customer[:60])}")
    lines.append(f"{tr(lang, 'a_o_phone')}: " + (f"<code>{esc(o.phone[:30])}</code>" if o.phone else "—"))
    if o.items:
        lines.append(tr(lang, "o_items") + ":")
        for it in o.items[:12]:
            q = f" × {ui.fmt_num(lang, it.qty)}" if it.qty not in (None, "") else ""
            lines.append("  • " + esc(it.name[:60]) + q)
    users = find_bot_users(o)
    st = store.snapshot()
    rows = pairs([btn("👤 " + user_label(st, k), f"a:uc:{k}") for k in users])
    rows.append([btn(tr(lang, "b_back"), back), btn(tr(lang, "b_admin"), "a:home")])
    ui.show(chat_id, mid, lang, "\n".join(lines), kb(rows))


def admin_orders_log(chat_id, lang, mid, uid_filter=None):
    st = store.snapshot()
    ents = [e for e in reversed(st["orders_log"]) if uid_filter is None or e["uid"] == uid_filter][:10]
    if not ents:
        ui.show(chat_id, mid, lang, tr(lang, "a_o_log_empty"), kb([back_home(lang, "a:o")])); return
    lines = [f"• <code>{esc(e['n'][:24])}</code> — {esc(user_label(st, e['uid']))} · {ui.fmt_ts(lang, e['ts'])}" for e in ents]
    rows = []
    for e in ents:
        d = cbsafe("a:ov:", e["n"])
        if d:
            rows.append(btn(f"{texts.status_label(lang, e['s'], e['sr']).split(' ')[0]} {e['n'][:22]}", d))
    rows = pairs(rows) + [back_home(lang, "a:o" if uid_filter is None else f"a:uc:{uid_filter}")]
    ui.show(chat_id, mid, lang, tr(lang, "a_o_log_title", lines="\n".join(lines)), kb(rows))


def admin_user_orders(chat_id, lang, mid, k):
    u = store.snapshot()["users"].get(str(k), {})
    st = store.snapshot()
    ents = [e for e in reversed(st["orders_log"]) if e["uid"] == int(k)][:10]
    api_orders, note = [], ""
    if u.get("phone"):
        try:
            api_orders = shop_api.get_api().list_user_orders(int(k), u["phone"])[:10]
        except shop_api.ShopNotConfigured:
            note = tr(lang, "a_o_setup")
        except shop_api.ShopError as e:
            log.warning("admin user orders failed: %s", safe(e))
            note = tr(lang, "a_o_err", err=esc(str(e)[:120]))
        except Exception as e:
            note = tr(lang, "a_o_err", err=esc(type(e).__name__))
    else:
        note = tr(lang, "a_uo_nophone")
    known = {o.number for o in api_orders}
    lines = [tr(lang, "a_uo_title", who=esc(user_label(st, k)))]
    if api_orders:
        lines.append(tr(lang, "a_uo_api"))
        lines += [f"• <code>{esc(o.number[:24])}</code> {texts.status_label(lang, o.status, o.status_raw)}" for o in api_orders]
    local = [e for e in ents if e["n"] not in known]
    if local:
        lines.append(tr(lang, "a_uo_log"))
        lines += [f"• <code>{esc(e['n'][:24])}</code> · {ui.fmt_ts(lang, e['ts'])}" for e in local]
    if not api_orders and not local:
        lines.append(tr(lang, "a_uo_none"))
    if note:
        lines.append("\n" + note)
    rows = order_btns(lang, api_orders)
    seen = set(known)
    extra = []
    for e in local:
        d = cbsafe("a:ov:", e["n"])
        if d and e["n"] not in seen:
            seen.add(e["n"]); extra.append(btn(f"🔎 {e['n'][:22]}", d))
    rows += pairs(extra)
    rows.append([btn(tr(lang, "b_back"), f"a:uc:{k}"), btn(tr(lang, "b_admin"), "a:home")])
    ui.show(chat_id, mid, lang, "\n".join(lines), kb(rows))


def set_ban(chat_id, uid, lang, mid, k, banned):
    if is_admin(int(k)):
        ui.send(chat_id, lang, tr(lang, "a_us_noban")); return
    if str(k) not in store.snapshot()["users"]:
        admin_user_card(chat_id, lang, mid, k, uid); return
    store.update_user(int(k), banned=True if banned else None, awaiting=None if banned else store.get_user(k).get("awaiting"))
    log.info("user %s %s by admin %s", k, "banned" if banned else "unbanned", uid)
    admin_user_card(chat_id, lang, mid, k, uid)


def admin_message_user(chat_id, lang, tid, msg):
    """Admin wrote a free message to a user (not tied to a ticket)."""
    ulang = lang_of(tid)
    try:
        tg.send(tid, out(ulang, tr(ulang, "support_reply")), raise_errors=True)
        tg.copy_message(tid, chat_id, msg["message_id"], raise_errors=True, msg=msg)
        if not store.get_user(tid).get("banned"):
            store.update_user(tid, awaiting="support")      # their answer lands in the support inbox
        ui.send(chat_id, lang, tr(lang, "a_um_sent"), kb([[btn(tr(lang, "b_back"), f"a:uc:{tid}"), btn(tr(lang, "b_admin"), "a:home")]]))
    except ApiError as e:
        log.warning("admin→user message failed: %s", safe(e))
        if tg.is_dead_chat(e):
            store.update_user(tid, blocked=True)
        ui.send(chat_id, lang, tr(lang, "a_t_fail"), kb([[btn(tr(lang, "b_back"), f"a:uc:{tid}")]]))


# ---- admins (owner only) --------------------------------------------------------------------------
def admin_admins(chat_id, lang, mid):
    st = store.snapshot()
    lines = ["👑 " + tr(lang, "a_role_owner") + ": " + who_label(st["admin_id"])]
    rows = []
    for a in st["admins"]:
        lines.append("🛡 " + who_label(a))
        rows.append(btn("❌ " + user_label(st, a), f"a:adx:{a}"))
    if not st["admins"]:
        lines.append(tr(lang, "a_ad_none"))
    rows = pairs(rows)
    rows.insert(0, [btn(tr(lang, "a_ad_add"), "a:ada"), btn(tr(lang, "a_ad_owner"), "a:ow")])
    rows.append(back_home(lang))
    ui.show(chat_id, mid, lang, tr(lang, "a_ad_title", lines="\n".join(lines)), kb(rows))


def notify_role(target, key, **kw):
    tl = lang_of(target)
    try:
        tg.send(target, out(tl, tr(tl, key, **kw)), kb([[btn(tr(tl, "b_admin"), "a:home")]]) if key != "a_ad_removed_note" else None, raise_errors=True)
        return True
    except ApiError as e:
        log.warning("role notification failed: %s", safe(e))
        return False


def grant_admin(chat_id, lang, k, back="a:ad"):
    """Shared add-extra-admin logic (Admins section AND user card). Owner-only callers. True = handled."""
    if is_admin(k):
        ui.send(chat_id, lang, tr(lang, "a_ad_already"), kb([back_home(lang, back)])); return True
    if store.get_user(k).get("banned"):
        ui.send(chat_id, lang, tr(lang, "a_ad_banned"), kb([[btn(tr(lang, "a_us_unban"), f"a:ub:{k}"), btn(tr(lang, "b_back"), back)]])); return True
    with store.transaction() as st:
        st["admins"] = [a for a in st["admins"] if a != k] + [k]
    log.info("extra admin %s added by owner", k)
    sent = notify_role(k, "a_ad_added_note")
    ui.send(chat_id, lang, tr(lang, "a_ad_added", who=who_label(k)) + ("" if sent else "\n" + tr(lang, "a_ad_notify_fail")),
            kb([back_home(lang, back)]))
    return True


def add_admin(chat_id, uid, lang, text):
    k = resolve_user(text)
    if k is None:
        ui.send(chat_id, lang, tr(lang, "a_ad_unknown"), kb([back_home(lang, "a:ad", "b_cancel")])); return False
    return grant_admin(chat_id, lang, k)


def remove_admin(chat_id, lang, mid, k, viewer=None):
    with store.transaction() as st:
        had = k in st["admins"]
        st["admins"] = [a for a in st["admins"] if a != k]
    if had:
        store.update_user(k, awaiting=None, await_data=None)
        log.info("extra admin %s removed", k)
        notify_role(k, "a_ad_removed_note")
    if viewer is not None:                      # invoked from the user card
        admin_user_card(chat_id, lang, mid, k, viewer)
    else:
        admin_admins(chat_id, lang, mid)


def owner_change_confirm(chat_id, lang, text):
    k = resolve_user(text)
    if k is None:
        ui.send(chat_id, lang, tr(lang, "a_ad_unknown"), kb([back_home(lang, "a:ad", "b_cancel")])); return False
    if is_owner(k):
        ui.send(chat_id, lang, tr(lang, "a_ow_same"), kb([back_home(lang, "a:ad")])); return True
    ui.send(chat_id, lang, tr(lang, "a_ow_confirm", who=who_label(k)),
            kb([[btn(tr(lang, "a_ow_yes"), f"a:owy:{k}"), btn(tr(lang, "a_bc_no"), "a:ad")]]))
    return True


def change_owner(chat_id, uid, lang, mid, k):
    if str(k) not in store.snapshot()["users"] or is_owner(k):
        admin_admins(chat_id, lang, mid); return
    with store.transaction() as st:
        old = st["admin_id"]
        st["admin_id"] = int(k)
        st["admins"] = [a for a in st["admins"] if a != k] + ([old] if old and old != k else [])   # old owner stays an extra admin
        st["pending_broadcast"] = None
        st["claim"] = {"code": None, "fails": {}}
    store.update_user(k, banned=None)
    log.info("owner changed %s → %s", uid, k)
    store.update_user(uid, awaiting=None, await_data=None)
    notify_role(k, "a_ow_note_new")
    ui.show(chat_id, mid, lang, tr(lang, "a_ow_done", who=who_label(k)), kb([[btn(tr(lang, "b_menu"), "m:menu")]]))


# ---- API connection screens ------------------------------------------------------------------
def safe_url(u):
    """Display form of the base URL: no userinfo / query."""
    if not u:
        return ""
    try:
        p = urllib.parse.urlsplit(u)
        host = p.hostname or ""
        if p.port:
            host += f":{p.port}"
        return f"{p.scheme}://{host}{p.path}"[:120]
    except ValueError:
        return "?"


def api_sources(saved, cfg):
    panel = bool(saved.get("base_url") or saved.get("api_key"))
    env = bool((not saved.get("base_url") and cfg["base_url"]) or (not saved.get("api_key") and cfg["api_key"]))
    return "api_src_mixed" if panel and env else "api_src_env" if env else "api_src_panel"


def admin_api_home(chat_id, lang, mid):
    saved, cfg = store.api_config(), shop_api.load_config()
    if not cfg["enabled"]:
        state = tr(lang, "api_off")
    elif not (cfg["base_url"] and cfg["api_key"]):
        state = tr(lang, "api_incomplete")
    else:
        state = tr(lang, "api_on")
    auth = {"bearer": "Bearer", "header": f"Header {cfg['auth_name']}", "query": f"Query ?{cfg['auth_name']}="}[cfg["auth_style"]]
    text = tr(lang, "api_home", state=state, url=esc(safe_url(cfg["base_url"]) or tr(lang, "api_unset")),
              key=esc(shop_api.mask_secret(cfg["api_key"]) if cfg["api_key"] else tr(lang, "api_unset")),
              auth=esc(auth), src=tr(lang, api_sources(saved, cfg)))
    rows = pairs([btn(tr(lang, "api_b_url"), "a:api:url"), btn(tr(lang, "api_b_key"), "a:api:key"),
                  btn(tr(lang, "api_b_auth"), "a:api:auth"), btn(tr(lang, "api_b_paths"), "a:api:paths"),
                  btn(tr(lang, "api_b_fields"), "a:api:fields"), btn(tr(lang, "api_b_test"), "a:api:test")])
    rows.append([btn(tr(lang, "api_b_toggle_on" if cfg["enabled"] else "api_b_toggle_off"), "a:api:tog")]
                + ([btn(tr(lang, "api_b_clear"), "a:api:keyx")] if saved.get("api_key") else []))
    rows.append([btn(tr(lang, "b_back"), "a:home")])
    ui.show(chat_id, mid, lang, text, kb(rows))


def back_api(lang, target="a:api"):
    return [btn(tr(lang, "b_back"), target)]


def admin_api_auth(chat_id, lang, mid):
    cfg = shop_api.load_config()
    cur = tr(lang, "api_as_" + cfg["auth_style"])
    rows = [[btn(("✅ " if cfg["auth_style"] == s else "") + tr(lang, "api_as_" + s), f"a:api:as:{s}")] for s in shop_api.AUTH_STYLES]
    rows.append([btn(tr(lang, "api_b_authname"), "a:api:an")])
    rows.append(back_api(lang))
    ui.show(chat_id, mid, lang, tr(lang, "api_auth_menu", cur=cur, name=esc(cfg["auth_name"])), kb(rows))


def admin_api_paths(chat_id, lang, mid):
    cfg = shop_api.load_config()
    saved = store.api_config().get("paths") or {}
    keys = list(shop_api.DEFAULT_PATHS)
    lines = "\n".join(f"{i + 1}. <b>{texts.PATH_LABELS[k][0 if lang == 'fa' else 1]}</b> "
                      f"{tr(lang, 'api_custom' if saved.get(k) else 'api_default')}\n<code>{esc(cfg['paths'][k])}</code>"
                      for i, k in enumerate(keys))
    rows = pairs([btn(f"✏️ {texts.PATH_LABELS[k][0 if lang == 'fa' else 1]}", f"a:api:p:{i}") for i, k in enumerate(keys)])
    rows.append(back_api(lang))
    text = tr(lang, "api_paths_menu", lines=lines)
    ui.show(chat_id, mid, lang, text, kb(rows))


def admin_api_path_edit(chat_id, uid, lang, mid, idx):
    k = list(shop_api.DEFAULT_PATHS)[idx]
    cfg = shop_api.load_config()
    set_await(uid, "api_path", k)
    ui.show(chat_id, mid, lang, tr(lang, "api_path_ask", name=texts.PATH_LABELS[k][0 if lang == "fa" else 1],
                                    cur=esc(cfg["paths"][k]), dflt=esc(shop_api.DEFAULT_PATHS[k])),
            kb([[btn(tr(lang, "api_reset"), f"a:api:pr:{idx}"), btn(tr(lang, "b_cancel"), "a:api:paths")]]))


GROUPS = ["general", "category", "product", "order"]


def admin_api_fields(chat_id, lang, mid):
    rows = pairs([btn(tr(lang, f"api_fg_{g}"), f"a:api:fg:{g}") for g in GROUPS])
    rows.append(back_api(lang))
    ui.show(chat_id, mid, lang, tr(lang, "api_fields_menu"), kb(rows))


def admin_api_fgroup(chat_id, lang, mid, g):
    cfg = shop_api.load_config()
    saved = store.api_config().get("fields") or {}
    idxs = [i for i, (_, _, grp) in enumerate(shop_api.FIELD_META) if grp == g]
    lines, btns = [], []
    for i in idxs:
        k = shop_api.FIELD_META[i][0]
        lab = texts.FIELD_LABELS[k][0 if lang == "fa" else 1]
        lines.append(f"• <b>{lab}</b> {'✏️' if saved.get(k) else ''}\n  <code>{esc(cfg['fields'][k] or '(auto)')}</code>")
        btns.append(btn("✏️ " + lab[:26], f"a:api:f:{i}"))
    rows = pairs(btns) + [back_api(lang, "a:api:fields")]
    ui.show(chat_id, mid, lang, tr(lang, "api_fgroup", g=tr(lang, f"api_fg_{g}"), lines="\n".join(lines)), kb(rows))


def admin_api_field_edit(chat_id, uid, lang, mid, idx):
    k, dflt, g = shop_api.FIELD_META[idx]
    cfg = shop_api.load_config()
    set_await(uid, "api_field", k)
    ui.show(chat_id, mid, lang, tr(lang, "api_field_ask", name=texts.FIELD_LABELS[k][0 if lang == "fa" else 1],
                                    cur=esc(cfg["fields"][k] or "(auto)"), dflt=esc(dflt or "(auto)")),
            kb([[btn(tr(lang, "api_reset"), f"a:api:fr:{idx}"), btn(tr(lang, "b_cancel"), f"a:api:fg:{g}")]]))


def admin_api_test(chat_id, lang, mid):
    cfg = shop_api.load_config()
    if not (cfg["enabled"] and cfg["base_url"] and cfg["api_key"]):
        ui.show(chat_id, mid, lang, tr(lang, "api_test_missing"), kb([back_api(lang)])); return
    ui.show(chat_id, mid, lang, tr(lang, "api_test_running"))
    r = shop_api.HttpShopAPI(cfg).test_connection()
    if r["ok"]:
        if r["sample"]:
            extra = tr(lang, "api_test_ok_sample", s=esc(r["sample"]))
        else:
            extra = tr(lang, "api_test_ok_nomap", keys=esc(", ".join(r["keys"]) or "-"))
        text = tr(lang, "api_test_ok", status=r["status"], ms=r["ms"], count=r["count"], extra=extra)
    else:
        text = tr(lang, "api_test_fail", err=esc(r["error"]), ms=r["ms"])
    ui.send(chat_id, lang, text, kb([[btn(tr(lang, "api_b_test"), "a:api:test"), btn(tr(lang, "b_back"), "a:api")]]))


def valid_base_url(u):
    if not re.fullmatch(r"https?://[^\s]{3,200}", u):
        return False
    try:
        p = urllib.parse.urlsplit(u)
        return bool(p.hostname) and not p.username and not p.password and not p.query and not p.fragment
    except ValueError:
        return False


def handle_api_input(chat_id, uid, lang, msg, kind, data):
    """Admin typed a value for the API panel. Returns True when consumed."""
    text = (msg.get("text") or "").strip()
    if kind == "api_key":
        # SECRET: never log, never echo, delete the message right after saving.
        key = text[7:].strip() if text.lower().startswith("bearer ") else text
        if not msg.get("text") or not (4 <= len(key) <= 500) or re.search(r"\s", key):
            tg.delete_message(chat_id, msg["message_id"])
            ui.send(chat_id, lang, tr(lang, "api_key_bad")); return True
        store.update_api(api_key=key)
        tg.add_secret(key)
        clear_await(uid)
        deleted = tg.delete_message(chat_id, msg["message_id"])
        ui.send(chat_id, lang, tr(lang, "api_key_saved", key=esc(shop_api.mask_secret(key))) + ("" if deleted else tr(lang, "api_key_del_fail")),
                kb([back_api(lang)]))
        return True
    if not text:
        return False
    if kind == "api_url":
        u = text.rstrip("/")
        if not valid_base_url(u):
            ui.send(chat_id, lang, tr(lang, "api_url_bad")); return True
        store.update_api(base_url=u)
        clear_await(uid)
        ui.send(chat_id, lang, tr(lang, "a_saved") + (tr(lang, "api_url_http") if u.startswith("http://") else ""), kb([back_api(lang)]))
    elif kind == "api_authname":
        if text == "-":
            store.update_api(auth_name=None)
        elif re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", text):
            store.update_api(auth_name=text)
        else:
            ui.send(chat_id, lang, tr(lang, "api_authname_bad")); return True
        clear_await(uid)
        ui.send(chat_id, lang, tr(lang, "a_saved"), kb([back_api(lang, "a:api:auth")]))
    elif kind == "api_path":
        if not re.fullmatch(r"/?[\w\-./{}?&=%,:~]{1,200}", text) or "://" in text or ".." in text or text.startswith("//"):
            ui.send(chat_id, lang, tr(lang, "api_path_bad")); return True
        store.set_api_map("paths", data, text)
        clear_await(uid)
        ui.send(chat_id, lang, tr(lang, "a_saved"), kb([back_api(lang, "a:api:paths")]))
    elif kind == "api_field":
        if not re.fullmatch(r"[\w.,\-]{1,100}", text):
            ui.send(chat_id, lang, tr(lang, "api_field_bad")); return True
        store.set_api_map("fields", data, text.replace(" ", ""))
        clear_await(uid)
        g = next(grp for kk, _, grp in shop_api.FIELD_META if kk == data)
        ui.send(chat_id, lang, tr(lang, "a_saved"), kb([back_api(lang, f"a:api:fg:{g}")]))
    return True


def handle_api_callback(chat_id, uid, lang, mid, parts):
    a = parts[0] if parts else ""
    arg = parts[1] if len(parts) > 1 else ""
    if a == "":
        admin_api_home(chat_id, lang, mid)
    elif a == "url":
        set_await(uid, "api_url")
        ui.show(chat_id, mid, lang, tr(lang, "api_url_ask"), kb([[btn(tr(lang, "b_cancel"), "a:api")]]))
    elif a == "key":
        set_await(uid, "api_key")
        ui.show(chat_id, mid, lang, tr(lang, "api_key_ask"), kb([[btn(tr(lang, "b_cancel"), "a:api")]]))
    elif a == "keyx":
        store.update_api(api_key=None)
        ui.show(chat_id, mid, lang, tr(lang, "api_key_cleared"), kb([back_api(lang)]))
    elif a == "auth":
        admin_api_auth(chat_id, lang, mid)
    elif a == "as" and arg in shop_api.AUTH_STYLES:
        store.update_api(auth_style=arg)
        admin_api_auth(chat_id, lang, mid)
    elif a == "an":
        set_await(uid, "api_authname")
        ui.show(chat_id, mid, lang, tr(lang, "api_authname_ask"), kb([back_api(lang, "a:api:auth")]))
    elif a == "tog":
        store.update_api(enabled=not store.api_config().get("enabled", True))
        admin_api_home(chat_id, lang, mid)
    elif a == "paths":
        admin_api_paths(chat_id, lang, mid)
    elif a in ("p", "pr") and arg.isdigit() and int(arg) < len(shop_api.DEFAULT_PATHS):
        if a == "p":
            admin_api_path_edit(chat_id, uid, lang, mid, int(arg))
        else:
            store.set_api_map("paths", list(shop_api.DEFAULT_PATHS)[int(arg)], None)
            clear_await(uid)
            admin_api_paths(chat_id, lang, mid)
    elif a == "fields":
        admin_api_fields(chat_id, lang, mid)
    elif a == "fg" and arg in GROUPS:
        admin_api_fgroup(chat_id, lang, mid, arg)
    elif a in ("f", "fr") and arg.isdigit() and int(arg) < len(shop_api.FIELD_META):
        i = int(arg)
        if a == "f":
            admin_api_field_edit(chat_id, uid, lang, mid, i)
        else:
            store.set_api_map("fields", shop_api.FIELD_META[i][0], None)
            clear_await(uid)
            admin_api_fgroup(chat_id, lang, mid, shop_api.FIELD_META[i][2])
    elif a == "test":
        admin_api_test(chat_id, lang, mid)


def handle_admin_callback(chat_id, uid, lang, mid, data):
    parts = data.split(":")
    act = parts[1] if len(parts) > 1 else "home"
    arg = ":".join(parts[2:])
    clear_await(uid)
    if act in OWNER_ONLY and not is_owner(uid):          # server-side enforcement
        ui.show(chat_id, mid, lang, tr(lang, "a_owner_only"), kb([back_home(lang, "a:home")])); return
    if act == "home":
        admin_home(chat_id, uid, lang, mid)
    elif act == "us":
        admin_users(chat_id, uid, lang, mid, int(arg) if arg.isdigit() else 1)
    elif act == "usb":
        ul = store.get_user(uid).get("ulist") or {}
        admin_users(chat_id, uid, lang, mid, ul.get("p", 1), ul.get("q"))
    elif act == "uq":
        set_await(uid, "a_usq")
        ui.show(chat_id, mid, lang, tr(lang, "a_us_ask"), kb([back_home(lang, "a:usb", "b_cancel")]))
    elif act == "uc" and arg.lstrip("-").isdigit():
        admin_user_card(chat_id, lang, mid, int(arg), uid)
    elif act == "um" and arg.lstrip("-").isdigit():
        if str(int(arg)) not in store.snapshot()["users"]:
            admin_user_card(chat_id, lang, mid, int(arg), uid); return
        set_await(uid, "a_umsg", int(arg))
        ui.show(chat_id, mid, lang, tr(lang, "a_um_ask", who=who_label(int(arg))), kb([back_home(lang, f"a:uc:{int(arg)}", "b_cancel")]))
    elif act == "ub" and arg.lstrip("-").isdigit():
        k = int(arg)
        set_ban(chat_id, uid, lang, mid, k, not store.get_user(k).get("banned"))
    elif act == "uo" and arg.lstrip("-").isdigit():
        admin_user_orders(chat_id, lang, mid, int(arg))
    elif act == "o":
        admin_orders(chat_id, uid, lang, mid)
    elif act == "on" and arg.isdigit():
        admin_orders(chat_id, uid, lang, mid, int(arg))
    elif act == "of":
        admin_order_filter(chat_id, lang, mid)
    elif act == "ofs" and (arg == "all" or arg in FILTERS):
        store.update_user(uid, ofilter=None if arg == "all" else arg)
        admin_orders_entry(chat_id, uid, lang, mid)
    elif act == "oq":
        set_await(uid, "a_osrch")
        ui.show(chat_id, mid, lang, tr(lang, "a_o_ask"), kb([back_home(lang, "a:o", "b_cancel")]))
    elif act == "ol":
        admin_orders_log(chat_id, lang, mid)
    elif act == "ov" and arg:
        admin_order_detail(chat_id, uid, lang, mid, arg)
    elif act == "ad":
        admin_admins(chat_id, lang, mid)
    elif act == "ada":
        set_await(uid, "a_addadm")
        ui.show(chat_id, mid, lang, tr(lang, "a_ad_ask"), kb([back_home(lang, "a:ad", "b_cancel")]))
    elif act in ("uad", "urm", "urmy") and arg.lstrip("-").isdigit():      # user-card role toggle (OWNER_ONLY, re-checked here)
        k = int(arg)
        if not is_owner(uid):
            ui.show(chat_id, mid, lang, tr(lang, "a_owner_only"), kb([back_home(lang, "a:home")])); return
        if str(k) not in store.snapshot()["users"]:
            admin_user_card(chat_id, lang, mid, k, uid); return
        if is_owner(k):
            ui.show(chat_id, mid, lang, tr(lang, "a_ad_owner_fixed"), kb([back_home(lang, f"a:uc:{k}")])); return
        if act == "uad":
            grant_admin(chat_id, lang, k, back=f"a:uc:{k}")
        elif k not in store.snapshot()["admins"]:
            admin_user_card(chat_id, lang, mid, k, uid)
        elif act == "urm":
            ui.show(chat_id, mid, lang, tr(lang, "a_ad_rm_confirm", who=who_label(k)),
                    kb([[btn(tr(lang, "a_ad_rm_yes"), f"a:urmy:{k}"), btn(tr(lang, "a_bc_no"), f"a:uc:{k}")]]))
        else:
            remove_admin(chat_id, lang, mid, k, viewer=uid)
    elif act == "adx" and arg.lstrip("-").isdigit():
        k = int(arg)
        ui.show(chat_id, mid, lang, tr(lang, "a_ad_rm_confirm", who=who_label(k)),
                kb([[btn(tr(lang, "a_ad_rm_yes"), f"a:adxy:{k}"), btn(tr(lang, "a_bc_no"), "a:ad")]]))
    elif act == "adxy" and arg.lstrip("-").isdigit():
        remove_admin(chat_id, lang, mid, int(arg))
    elif act == "ow":
        set_await(uid, "a_newowner")
        ui.show(chat_id, mid, lang, tr(lang, "a_ow_ask"), kb([back_home(lang, "a:ad", "b_cancel")]))
    elif act == "owy" and arg.lstrip("-").isdigit():
        change_owner(chat_id, uid, lang, mid, int(arg))
    elif act == "stats":
        admin_stats(chat_id, lang, mid)
    elif act == "bc":
        set_await(uid, "a_bc")
        ui.show(chat_id, mid, lang, tr(lang, "a_bc_ask"), kb([[btn(tr(lang, "b_cancel"), "a:home")]]))
    elif act == "bcy":
        clear_kb(chat_id, mid)
        run_broadcast(chat_id, lang)
    elif act == "bcn":
        with store.transaction() as st:
            st["pending_broadcast"] = None
        ui.show(chat_id, mid, lang, tr(lang, "cancelled"), kb([[btn(tr(lang, "b_admin"), "a:home")]]))
    elif act == "tm" and arg in ("faq", "about"):
        admin_text_menu(chat_id, lang, mid, arg)
    elif act == "te" and arg in ("faq_fa", "faq_en", "about_fa", "about_en"):
        set_await(uid, "a_text", arg)
        ui.show(chat_id, mid, lang, tr(lang, "a_text_ask", which="فارسی" if arg.endswith("fa") else "English"),
                kb([[btn(tr(lang, "b_cancel"), "a:tm:" + arg.split("_")[0])]]))
    elif act == "tz" and arg in ("faq_fa", "faq_en", "about_fa", "about_en"):
        store.set_text(arg, None)
        admin_text_menu(chat_id, lang, mid, arg.split("_")[0])
    elif act == "inbox":
        admin_inbox(chat_id, lang, mid)
    elif act == "tv" and arg.isdigit():
        admin_ticket(chat_id, lang, mid, int(arg))
    elif act == "tr" and arg.isdigit():
        set_await(uid, "a_reply", int(arg))
        ui.show(chat_id, mid, lang, tr(lang, "a_t_ask", id=arg), kb([[btn(tr(lang, "b_cancel"), f"a:tv:{arg}")]]))
    elif act in ("tc", "to") and arg.isdigit():
        with store.transaction() as st:
            t = st["tickets"].get(arg)
            if t:
                t["status"] = "closed" if act == "tc" else "open"
        if act == "tc":
            t = store.snapshot()["tickets"].get(arg)
            if t:
                ul = lang_of(t["uid"])
                store.update_user(t["uid"], awaiting=None)
                ui.send(t["uid"], ul, tr(ul, "support_closed"), ui.menu_only(ul))
        admin_ticket(chat_id, lang, mid, int(arg))
    elif act == "api":
        handle_api_callback(chat_id, uid, lang, mid, parts[2:])


def clear_kb(chat_id, mid):
    tg.clear_markup(chat_id, mid)


# ============================================================================ dispatch
def handle_awaiting(chat_id, uid, lang, msg, aw, data):
    """Returns True when the message was consumed by a pending prompt."""
    text = (msg.get("text") or "").strip()
    if aw == "search":
        if len(text) < 2:
            ui.send(chat_id, lang, tr(lang, "search_bad")); return True
        clear_await(uid)
        store.update_user(uid, nav={"k": "s", "q": text[:80], "p": 1})
        show_product_list(chat_id, uid, lang, 1)
    elif aw == "track_num":
        num = ui_ascii(text)
        if not ORDER_RE.match(num):
            ui.send(chat_id, lang, tr(lang, "track_bad")); return True
        if rate_limited(uid):
            clear_await(uid); ui.send(chat_id, lang, tr(lang, "too_many"), ui.menu_only(lang)); return True
        phone = store.get_user(uid).get("phone")
        if phone:
            clear_await(uid)
            r = do_track(chat_id, uid, lang, num, phone)
            if r is None or r:
                return True
        set_await(uid, "track_phone", num)
        ui.send(chat_id, lang, tr(lang, "phone_ask"), contact_keyboard(lang))
    elif aw == "track_phone":
        contact = msg.get("contact")
        if contact:
            raw = contact.get("phone_number", "")
        else:
            raw = text
        d = phone_ok(raw)
        if not d:
            ui.send(chat_id, lang, tr(lang, "phone_bad")); return True
        if rate_limited(uid):
            clear_await(uid); ui.send(chat_id, lang, tr(lang, "too_many"), ui.menu_only(lang)); return True
        clear_await(uid)
        if contact and tg.is_own_contact(contact, uid):       # only a PROVEN own number is remembered
            store.update_user(uid, phone=canonical_phone(d))
        r = do_track(chat_id, uid, lang, data, canonical_phone(d))
        if r is False:
            ui.send(chat_id, lang, tr(lang, "order_notfound"), kb([[btn(tr(lang, "b_again"), "m:trk"), btn(tr(lang, "b_support_short"), "m:sup")], ui.menu_row(lang)]))
    elif aw == "link_phone":
        contact = msg.get("contact")
        if not contact:
            d = phone_ok(text)
            # typed numbers are NOT accepted for account linking (unverifiable): require the contact button
            ui.send(chat_id, lang, tr(lang, "orders_link"), contact_keyboard(lang)); return True
        if not tg.is_own_contact(contact, uid):
            ui.send(chat_id, lang, tr(lang, "contact_foreign")); return True
        d = phone_ok(contact.get("phone_number", ""))
        if not d:
            ui.send(chat_id, lang, tr(lang, "phone_bad")); return True
        clear_await(uid)
        store.update_user(uid, phone=canonical_phone(d))
        tg.remove_keyboard(chat_id, tr(lang, "linked"))
        show_my_orders(chat_id, uid, lang)
    elif aw == "support":
        support_message(chat_id, uid, lang, msg)
    elif aw == "a_usq" and is_admin(uid):
        if len(text) < 2:
            return False
        clear_await(uid)
        admin_users(chat_id, uid, lang, None, query=text[:60])
    elif aw == "a_umsg" and is_admin(uid):
        clear_await(uid)
        admin_message_user(chat_id, lang, data, msg)
    elif aw == "a_osrch" and is_admin(uid):
        num = ui_ascii(text)
        if not ORDER_RE.match(num):
            ui.send(chat_id, lang, tr(lang, "track_bad")); return True
        clear_await(uid)
        admin_order_detail(chat_id, uid, lang, None, num)
    elif aw == "a_addadm" and is_owner(uid):
        if not text:
            return False
        if add_admin(chat_id, uid, lang, text):
            clear_await(uid)
    elif aw == "a_newowner" and is_owner(uid):
        if not text:
            return False
        if owner_change_confirm(chat_id, lang, text):
            clear_await(uid)
    elif aw == "a_bc" and is_owner(uid):
        clear_await(uid)
        with store.transaction() as st:
            st["pending_broadcast"] = {"chat_id": chat_id, "message_id": msg["message_id"], "ts": now(),
                                       "msg": {"text": msg.get("text"), "caption": msg.get("caption"),
                                               "photo": bool(msg.get("photo"))}}
        n = len(broadcast_recipients())
        ui.send(chat_id, lang, tr(lang, "a_bc_preview"))
        tg.copy_message(chat_id, chat_id, msg["message_id"], msg=msg)
        ui.send(chat_id, lang, tr(lang, "a_bc_confirm", n=n),
                kb([[btn(tr(lang, "a_bc_yes"), "a:bcy"), btn(tr(lang, "a_bc_no"), "a:bcn")]]))
    elif aw == "a_text" and is_admin(uid):
        if not text:
            return False
        store.set_text(data, text[:MAX_TEXT])
        clear_await(uid)
        ui.send(chat_id, lang, tr(lang, "a_saved"), kb([[btn(tr(lang, "b_back"), "a:tm:" + data.split("_")[0])]]))
    elif aw == "a_reply" and is_admin(uid):
        clear_await(uid)
        admin_reply_to_ticket(chat_id, data, msg)
    elif aw and aw.startswith("api_") and is_owner(uid):
        return handle_api_input(chat_id, uid, lang, msg, aw, data)
    else:
        return False
    return True


def ui_ascii(s):
    return shop_api.to_ascii_digits(s.strip())


def handle_command(chat_id, uid, lang, tgu, cmd, arg, is_new):
    if cmd == "/start":
        if not store.get_user(uid).get("lang"):
            ui.send(chat_id, "fa", tr("fa", "pick_lang"), ui.lang_menu())
        else:
            send_welcome(chat_id, uid, lang, tgu.get("first_name") or "")
    elif cmd == "/lang":
        ui.send(chat_id, lang, tr(lang, "pick_lang"), ui.lang_menu())
    elif cmd == "/help":
        ui.send(chat_id, lang, tr(lang, "help"), ui.main_menu(lang, is_admin(uid)))
    elif cmd == "/cancel":
        ui.send(chat_id, lang, tr(lang, "cancelled"), ui.main_menu(lang, is_admin(uid)))
    elif cmd == "/admin":
        if is_admin(uid):
            admin_home(chat_id, uid, lang)
        else:
            ui.send(chat_id, lang, tr(lang, "a_denied"))
    else:
        ui.send(chat_id, lang, tr(lang, "hint_menu"), ui.main_menu(lang, is_admin(uid)))


# ---- owner claim (Bale / Rubika) ------------------------------------------------------------------
# On platforms where a @username is not proof of identity, ownership is bound with a random one-time
# code.  The code is generated on first start, stored (state file is mode 600) and printed ONLY to this
# platform's log file; the owner sends `/claim <code>` to the bot.  Wrong attempts are rate-limited.
def ensure_claim_code():
    """Returns the pending code, or None when an owner is already bound / username binding is used."""
    if tg.T.username_admin_bind:
        return None
    with store.transaction() as st:
        if st.get("admin_id"):
            st["claim"] = {"code": None, "fails": {}}
            return None
        c = st.setdefault("claim", {})
        if not c.get("code"):
            c["code"] = "-".join(secrets.token_hex(2).upper() for _ in range(3))    # e.g. 3F9A-0B7C-D412
            c["fails"] = {}
        return c["code"]


def handle_claim(chat_id, uid, lang, msg, text):
    # always remove the message that carries the code
    parts = text.split(None, 1)
    given = parts[1].strip() if len(parts) > 1 else ""
    if given:
        tg.delete_message(chat_id, msg["message_id"])
    with store.transaction() as st:
        if st.get("admin_id"):
            ui.send(chat_id, lang, tr(lang, "claim_done")); return
        c = st.setdefault("claim", {"code": None, "fails": {}})
        fails = [t for t in c.setdefault("fails", {}).get(str(uid), []) if t > now() - 3600]
        total = sum(len([t for t in v if t > now() - 3600]) for v in c["fails"].values())
        if len(fails) >= 5 or total >= 20 or not c.get("code"):
            ui.send(chat_id, lang, tr(lang, "claim_locked")); return
        if given and hmac.compare_digest(given.upper().replace(" ", ""), c["code"]):
            st["admin_id"] = int(uid)
            st["claim"] = {"code": None, "fails": {}}
            ok = True
        else:
            c["fails"][str(uid)] = fails + [now()]
            ok = False
    if ok:
        log.info("admin bound via claim code to id %s", uid)
        ui.send(chat_id, lang, tr(lang, "claim_ok", uid=uid), ui.main_menu(lang, True))
    else:
        ui.send(chat_id, lang, tr(lang, "claim_bad"))


def ban_notice(chat_id, uid, lang, u):
    """Banned users are ignored; they get one short notice per hour."""
    if now() - u.get("ban_note", 0) > 3600:
        store.update_user(uid, ban_note=now())
        ui.send(chat_id, lang, tr(lang, "banned_note"))


def handle_message(msg, bot_username=""):
    chat = msg.get("chat", {})
    if chat.get("type", "private") != "private":
        return
    chat_id = chat["id"]
    if "from" not in msg or msg["from"].get("is_bot"):
        return
    tgu = msg["from"]
    uid = tgu["id"]
    is_new, _ = touch_user(tgu)
    if bind_admin_if_needed(tgu):
        log.info("admin bound to numeric id %s", uid)
        tg.send(chat_id, out(lang_of(uid), tr(lang_of(uid), "a_bound", uid=uid)))
    lang = lang_of(uid)
    text = (msg.get("text") or "").strip()
    u = store.get_user(uid)
    if u.get("banned") and not is_admin(uid):
        ban_notice(chat_id, uid, lang, u); return
    aw, data = u.get("awaiting"), u.get("await_data")

    # one-time owner claim (platforms without trusted usernames): /claim <code>
    if text.lower().split("@")[0].startswith("/claim"):
        handle_claim(chat_id, uid, lang, msg, text); return

    # reply keyboard "Menu" button
    if text in (tr("fa", "b_menu"), tr("en", "b_menu")):
        clear_await(uid)
        tg.remove_keyboard(chat_id, "🌸")
        show_menu(chat_id, uid, lang); return

    KNOWN = ("/start", "/lang", "/help", "/cancel", "/admin")
    if text.startswith("/") and not (aw and text.split(None, 1)[0].split("@")[0].lower() not in KNOWN):
        # (while a prompt is pending, an unknown "/something" is input – e.g. an endpoint path "/products")
        parts = text.split(None, 1)
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if aw:
            clear_await(uid)
            if cmd == "/cancel":
                tg.remove_keyboard(chat_id, "✅")
        handle_command(chat_id, uid, lang, tgu, cmd, arg, is_new)
        return

    # admin answering a forwarded ticket message with Telegram's native Reply
    if is_owner(uid) and msg.get("reply_to_message") and aw != "a_reply":
        tid = store.snapshot()["admin_map"].get(str(msg["reply_to_message"].get("message_id")))
        if tid:
            admin_reply_to_ticket(chat_id, tid, msg); return

    if aw and handle_awaiting(chat_id, uid, lang, msg, aw, data):
        return
    if msg.get("contact") and not aw:
        # contact shared while no prompt is active → treat as linking their own number
        if tg.is_own_contact(msg["contact"], uid) and phone_ok(msg["contact"].get("phone_number", "")):
            store.update_user(uid, phone=canonical_phone(phone_ok(msg["contact"]["phone_number"])))
            tg.remove_keyboard(chat_id, tr(lang, "linked"))
            return
    ui.send(chat_id, lang, tr(lang, "hint_menu"), ui.main_menu(lang, is_admin(uid)))


def handle_callback(cq, bot_username=""):
    tg.answer_cb(cq["id"])
    msg = cq.get("message")
    if not msg:
        return
    chat_id, mid = msg["chat"]["id"], msg["message_id"]
    tgu = cq["from"]
    uid = tgu["id"]
    data = cq.get("data") or ""
    touch_user(tgu)
    bind_admin_if_needed(tgu)
    lang = lang_of(uid)
    if store.get_user(uid).get("banned") and not is_admin(uid):
        ban_notice(chat_id, uid, lang, store.get_user(uid)); return

    if data.startswith("l:"):
        nl = data[2:]
        if nl in ("fa", "en"):
            store.update_user(uid, lang=nl)
            ui.send(chat_id, nl, tr(nl, "lang_set"))
            send_welcome(chat_id, uid, nl, tgu.get("first_name") or "")
        return
    if data.startswith("a:"):
        if not is_admin(uid):
            ui.send(chat_id, lang, tr(lang, "a_denied")); return
        handle_admin_callback(chat_id, uid, lang, mid, data)
        return

    # any user-side navigation ends a pending prompt
    if store.get_user(uid).get("awaiting"):
        clear_await(uid)
    if data == "m:menu":
        show_menu(chat_id, uid, lang, mid)
    elif data == "m:prod":
        show_categories(chat_id, uid, lang, mid)
    elif data.startswith("pc:"):
        try:
            i = int(data[3:])
        except ValueError:
            return
        cats = store.get_user(uid).get("cats") or []
        if i == -1:
            nav = {"k": "c", "c": None, "cn": "", "p": 1}
        elif 0 <= i < len(cats):
            nav = {"k": "c", "c": cats[i][0], "cn": cats[i][1], "p": 1}
        else:
            ui.show(chat_id, mid, lang, tr(lang, "expired"), ui.menu_only(lang)); return
        store.update_user(uid, nav=nav)
        show_product_list(chat_id, uid, lang, 1, mid)
    elif data.startswith("pn:") and data[3:].isdigit():
        show_product_list(chat_id, uid, lang, int(data[3:]), mid)
    elif data.startswith("pd:"):
        show_product(chat_id, uid, lang, data[3:], mid)
    elif data == "m:srch":
        set_await(uid, "search")
        ui.show(chat_id, mid, lang, tr(lang, "search_ask"), ui.menu_only(lang))
    elif data == "m:trk":
        set_await(uid, "track_num")
        ui.show(chat_id, mid, lang, tr(lang, "track_ask"), ui.menu_only(lang))
    elif data == "m:ord":
        if not tg.T.verified_contact:
            ui.show(chat_id, mid, lang, tr(lang, "orders_unavailable"),
                    kb([[btn(tr(lang, "b_track"), "m:trk"), btn(tr(lang, "b_menu"), "m:menu")]]))
        elif store.get_user(uid).get("phone"):
            show_my_orders(chat_id, uid, lang, mid)
        else:
            set_await(uid, "link_phone")
            ui.send(chat_id, lang, tr(lang, "orders_link"), contact_keyboard(lang))
    elif data == "m:unlink":
        store.update_user(uid, phone=None)
        ui.show(chat_id, mid, lang, tr(lang, "unlinked"), ui.menu_only(lang))
    elif data.startswith("ov:"):
        phone = store.get_user(uid).get("phone")
        if not phone:
            set_await(uid, "link_phone"); ui.send(chat_id, lang, tr(lang, "orders_link"), contact_keyboard(lang)); return
        o, api = shop(chat_id, lang, lambda a: a.get_order(data[3:], phone), mid)
        if api is None:
            return
        if not o:
            ui.show(chat_id, mid, lang, tr(lang, "order_notfound"), ui.menu_only(lang)); return
        log_order(uid, o)
        ui.show(chat_id, mid, lang, order_card(lang, o, api),
                kb([[btn(tr(lang, "b_back"), "m:ord"), btn(tr(lang, "b_menu"), "m:menu")]]))
    elif data == "m:sup":
        set_await(uid, "support")
        ui.show(chat_id, mid, lang, tr(lang, "support_ask", menu=tr(lang, "b_menu")), ui.menu_only(lang))
    elif data == "m:faq":
        show_faq(chat_id, uid, lang, mid)
    elif data == "m:abt":
        show_about(chat_id, uid, lang, mid)
    elif data == "m:lang":
        ui.show(chat_id, mid, lang, tr(lang, "pick_lang"), ui.lang_menu())


# ============================================================================ main loop
_lock_fd = None


def single_instance():
    global _lock_fd
    name = "bot.lock" if tg.PLATFORM == "telegram" else f"bot_{tg.PLATFORM}.lock"
    _lock_fd = open(os.path.join(BASE, name), "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another bot instance is already running", file=sys.stderr)
        sys.exit(0)
    _lock_fd.write(str(os.getpid())); _lock_fd.flush()


def main():
    global BOT_USERNAME
    tg.setup_logging()          # stderr → run*.sh appends to the platform log; secrets redacted by the formatter
    single_instance()
    tg.add_secret(shop_api.load_config()["api_key"])
    while True:
        try:
            me = tg.get_me()
            break
        except ApiError as e:               # platform API unreachable / token rejected: wait, don't crash-loop
            log.warning("getMe failed (%s); retrying in 60 s", safe(e)); time.sleep(60)
    BOT_USERNAME = me["username"]
    log.info("Bot started on %s: @%s (%s) (shop API: %s)", tg.LABEL, BOT_USERNAME, me.get("name"), shop_api.api_state())
    code = ensure_claim_code()
    if code:
        # deliberately printed ONLY here (this platform's log file). Owner: send  /claim <code>  to the bot.
        log.info("CLAIM CODE (one-time, send '/claim <code>' to the bot to become admin): %s", code)
    elif not store.admin_id():
        log.info("No admin bound yet: the first message from @%s binds the admin", ADMIN_USERNAME)
    tg.prepare_polling()
    state = {}
    while True:
        try:
            updates = tg.get_updates(state)
        except ApiError as e:
            log.warning("getUpdates: %s", safe(e)); time.sleep(5); continue
        for up in updates:
            try:
                if up.get("message"):
                    handle_message(up["message"], BOT_USERNAME)
                elif up.get("callback_query"):
                    handle_callback(up["callback_query"], BOT_USERNAME)
            except Exception as e:
                log.exception("handler error: %s", type(e).__name__)
                try:
                    chat = (up.get("message") or up.get("callback_query", {}).get("message") or {}).get("chat", {})
                    if chat.get("id"):
                        tg.send(chat["id"], "❌ خطای غیرمنتظره رخ داد. دوباره امتحان کن.\nUnexpected error, please try again.", html=False)
                except Exception:
                    pass


if __name__ == "__main__":
    main()

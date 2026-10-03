"""Shared UI helpers: RTL-friendly output, menus, formatting."""
import re, html
import store, tg, texts
from tg import btn, kb, pairs
from texts import tr

RLM = "\u200f"
FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def out(lang, text):
    """RTL-friendly: prefix each non-empty line with a right-to-left mark for Persian."""
    if lang != "fa":
        return text
    return "\n".join((RLM + l) if l.strip() else l for l in text.split("\n"))


def lang_of(uid):
    l = store.get_user(uid).get("lang")
    return l if l in ("fa", "en") else "fa"


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=False)


def clean_html(s, limit=300):
    s = re.sub(r"<[^>]+>", " ", str(s or ""))
    s = html.unescape(re.sub(r"\s+", " ", s)).strip()
    return esc(s[:limit] + ("…" if len(s) > limit else ""))


def fmt_num(lang, n):
    try:
        f = float(str(n).replace(",", ""))
        s = f"{int(f):,}" if f == int(f) else f"{f:,.2f}"
    except (TypeError, ValueError):
        s = str(n)
    return s.translate(FA_DIGITS) if lang == "fa" else s


def fmt_price(lang, price, currency=""):
    if price in (None, ""):
        return tr(lang, "price_unknown")
    return esc(fmt_num(lang, price) + (" " + currency if currency else ""))


def send(chat_id, lang, text, markup=None):
    return tg.send(chat_id, out(lang, text), markup)


def show(chat_id, mid, lang, text, markup=None):
    return tg.show(chat_id, mid, out(lang, text), markup)


def main_menu(lang, admin=False):
    items = [("b_products", "m:prod"), ("b_search", "m:srch"), ("b_track", "m:trk"), ("b_orders", "m:ord"),
             ("b_support", "m:sup"), ("b_faq", "m:faq"), ("b_about", "m:abt"), ("b_lang", "m:lang")]
    if not tg.T.verified_contact:            # e.g. Rubika: shared contacts can't be proven to be the sender's own
        items = [x for x in items if x[1] != "m:ord"]
    rows = pairs([btn(tr(lang, k), d) for k, d in items])
    if admin:
        rows.append([btn(tr(lang, "b_admin"), "a:home")])
    return kb(rows)


def lang_menu():
    return kb([[btn("🇮🇷 فارسی", "l:fa"), btn("🇬🇧 English", "l:en")]])


def menu_row(lang):
    return [btn(tr(lang, "b_menu"), "m:menu")]


def menu_only(lang):
    return kb([menu_row(lang)])


# ---- dates (Asia/Tehran; Jalali for Persian, Gregorian for English) ------------------------------
def _g2j(gy, gm, gd):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + g_d_m[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def fmt_ts(lang, ts, with_time=True):
    """Timestamp → 'YYYY/MM/DD HH:MM' in Tehran time (Jalali for fa)."""
    if not ts:
        return "—"
    import datetime as dt
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Asia/Tehran")
    except Exception:
        tz = dt.timezone(dt.timedelta(hours=3, minutes=30))
    d = dt.datetime.fromtimestamp(int(ts), tz)
    y, m, day = (_g2j(d.year, d.month, d.day) if lang == "fa" else (d.year, d.month, d.day))
    s = f"{y:04d}/{m:02d}/{day:02d}" + (f" {d.hour:02d}:{d.minute:02d}" if with_time else "")
    return s.translate(FA_DIGITS) if lang == "fa" else s

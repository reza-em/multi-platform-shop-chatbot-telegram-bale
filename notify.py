"""Order-status notifications (webhook-free).

`notify_user_order_update(order_number, status)` can be called from anywhere (a cron script, a
python shell, or — later — an HTTP endpoint).  It messages every Telegram user who has previously
verified / listed that order in the bot (subscription recorded automatically by the tracking flow).

────────────────────────────────────────────────────────────────────────────────────────────
FUTURE WEBHOOK HOOK  (NOT enabled; no port is opened by this project)
When the website API can push order updates, expose a tiny HTTP endpoint (e.g. behind nginx
with HTTPS, or `http.server` bound to 127.0.0.1) that verifies a shared-secret/HMAC header and
then calls:

    import notify
    notify.handle_order_webhook(json_payload)      # {"order_number": "...", "status": "shipped"}

`handle_order_webhook` validates the payload and delegates to `notify_user_order_update`.
Remember to compare secrets with hmac.compare_digest and to bind to localhost unless TLS-fronted.
────────────────────────────────────────────────────────────────────────────────────────────
"""
import re, logging, time
import store, tg, texts, shop_api, ui

log = logging.getLogger("notify")


def _key(order_number):
    return str(order_number).strip().lower()


def subscribe(uid, order_number):
    """Remember that this Telegram user may be notified about that order (called after verification)."""
    k = _key(order_number)
    with store.transaction() as st:
        lst = st["order_subs"].setdefault(k, [])
        if int(uid) not in lst:
            lst.append(int(uid))


def notify_user_order_update(order_number, status, force=False):
    """Send an order-status update to the subscribed Telegram users.

    status: canonical key (pending/paid/processing/packed/shipped/delivered/canceled/refunded/
            returned/failed/on_hold) or any raw text from the website (mapped via normalize_status).
    Returns {"sent": n, "failed": n, "skipped": reason|None}.
    """
    k = _key(order_number)
    raw = str(status or "").strip()
    canon = raw.lower() if ("st_" + raw.lower()) in texts.FA else shop_api.normalize_status(raw)
    with store.transaction() as st:
        uids = list(st["order_subs"].get(k, []))
        if not uids:
            return {"sent": 0, "failed": 0, "skipped": "no subscribers"}
        if not force and st["notified"].get(k) == raw:
            return {"sent": 0, "failed": 0, "skipped": "same status already notified"}
        st["notified"][k] = raw
    sent = failed = 0
    for uid in uids:
        if store.get_user(uid).get("banned"):
            continue
        lang = store.get_user(uid).get("lang") or "fa"
        label = texts.status_label(lang, canon, raw)
        import html
        text = texts.tr(lang, "notify_update", num=html.escape(str(order_number)), status=label)
        try:
            tg.send(uid, ui.out(lang, text), raise_errors=True)
            sent += 1
        except tg.ApiError as e:
            failed += 1
            if tg.is_dead_chat(e):
                store.update_user(uid, blocked=True)
            log.warning("order notify failed: %s", tg.safe(e))
        time.sleep(0.05)
    return {"sent": sent, "failed": failed, "skipped": None}


def handle_order_webhook(payload):
    """Documented hook for a future webhook receiver. Returns notify result or {'error': ...}."""
    if not isinstance(payload, dict):
        return {"error": "payload must be an object"}
    num, status = payload.get("order_number"), payload.get("status")
    if not num or not status or not re.fullmatch(r"[\w\-#./ ]{1,40}", str(num)):
        return {"error": "order_number/status missing or invalid"}
    return notify_user_order_update(str(num), str(status))

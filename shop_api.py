"""Shop adapter layer for the shop website.

    ShopAPI (abstract)
      ├─ NotConfiguredAPI  – used when nothing is configured; every call raises ShopNotConfigured
      ├─ MockShopAPI       – clearly-labelled example data, ONLY when env SHOP_USE_MOCK=1
      └─ HttpShopAPI       – real adapter, driven ENTIRELY by configuration edited from the bot's
                             admin panel (base URL, key, auth style, endpoint paths, response-field
                             mapping).  See README.md → "Plugging in the real API".

The bot only talks to `get_api()`; it never fabricates data when the API is missing.
"""
import os, re, time, logging, urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, List, Optional

import requests

import store, tg

log = logging.getLogger("shop")

PER_PAGE = 6            # products per page in the bot (2 columns x 3 rows)
TIMEOUT = (5, 10)       # connect, read (seconds)
MAX_BODY = 5 * 1024 * 1024


# ----------------------------------------------------------------------------- errors
class ShopError(Exception):
    """Generic failure talking to the shop (message is safe to show an admin; never contains secrets)."""


class ShopNotConfigured(ShopError):
    pass


class NotFound(ShopError):
    """HTTP 404 from the site (internal; adapters convert it to empty/None results where sensible)."""


class VerificationUnavailable(ShopError):
    """The API returned the order but gives us no way to verify the phone -> we refuse to show it."""


# ----------------------------------------------------------------------------- models
@dataclass
class Category:
    id: str
    name: str


@dataclass
class Product:
    id: str
    name: str
    price: Any = None            # number or raw string, as delivered
    currency: str = ""
    image: str = ""
    url: str = ""
    description: str = ""
    in_stock: Optional[bool] = None
    is_mock: bool = False


@dataclass
class Page:
    items: list
    page: int = 1
    pages: int = 1


@dataclass
class OrderItem:
    name: str
    qty: Any = None


@dataclass
class Order:
    number: str
    status_raw: str = ""
    status: str = "unknown"      # canonical key, see normalize_status()
    total: Any = None
    currency: str = ""
    date: str = ""
    tracking: str = ""
    phone: str = ""
    items: List[OrderItem] = field(default_factory=list)
    is_mock: bool = False
    customer: str = ""


# ----------------------------------------------------------------------------- helpers
def to_ascii_digits(s):
    return str(s).translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))


def norm_phone(p):
    """Normalise an (Iranian) phone number to its last 10 digits for comparison; '' if unusable."""
    d = re.sub(r"\D", "", to_ascii_digits(p or ""))
    return d[-10:] if len(d) >= 10 else ""


def phones_match(a, b):
    na, nb = norm_phone(a), norm_phone(b)
    return bool(na) and na == nb


STATUS_KEYWORDS = [
    ("canceled", ("cancel", "لغو", "void")),
    ("refunded", ("refund", "بازگشت وجه", "استرداد")),
    ("returned", ("return", "مرجوع", "برگشت")),
    ("failed", ("fail", "ناموفق", "خطا")),
    ("on_hold", ("hold", "معلق")),
    ("delivered", ("deliver", "complete", "تحویل", "تکمیل", "انجام شد")),
    ("packed", ("pack", "آماده", "بسته")),
    ("shipped", ("ship", "dispatch", "sent", "ارسال", "پست", "در راه")),
    ("paid", ("paid", "پرداخت شده", "پرداخت‌شده", "پرداخت موفق")),
    ("pending", ("pend", "wait", "await", "انتظار", "پرداخت")),
    ("processing", ("process", "confirm", "پردازش", "تایید", "تأیید", "در حال")),
]


def normalize_status(raw):
    s = (raw or "").strip().lower()
    if not s:
        return "unknown"
    for key, words in STATUS_KEYWORDS:
        if any(w in s for w in words):
            return key
    return "unknown"


def _walk(obj, path):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            i = int(part)
            cur = cur[i] if -len(cur) <= i < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def extract(obj, spec):
    """spec = 'a,b.c,images.0' -> first non-empty value among the alternatives (dot paths)."""
    for alt in (spec or "").split(","):
        alt = alt.strip()
        if not alt:
            continue
        v = _walk(obj, alt)
        if v not in (None, "", [], {}):
            return v
    return None


def _as_str(v):
    """Scalar-ise: dict -> src/url/name, list -> first element."""
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        for k in ("src", "url", "name", "title", "label", "value"):
            if v.get(k):
                v = v[k]
                break
        else:
            return ""
    return "" if v is None else str(v)


def _as_bool(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v > 0
    s = str(v).strip().lower()
    if s in ("instock", "in_stock", "true", "yes", "1", "available", "موجود"):
        return True
    if s in ("outofstock", "out_of_stock", "false", "no", "0", "unavailable", "ناموجود"):
        return False
    return None


def mask_secret(s):
    if not s:
        return "—"
    if len(s) <= 8:
        return "••••"
    return s[:2] + "••••••" + s[-2:]


# ----------------------------------------------------------------------------- configuration
# Endpoint paths are RELATIVE to the base URL. Placeholders: {id} {q} {page} {per_page}
# {category} {order} {phone} {user}.  Query parameters whose value ends up empty are dropped.
DEFAULT_PATHS = {
    "categories": "/categories",
    "products": "/products?category={category}&page={page}&per_page={per_page}",
    "product": "/products/{id}",
    "search": "/products?search={q}&page={page}&per_page={per_page}",
    "order": "/orders/{order}",
    "user_orders": "/orders?phone={phone}",
    # admin-only: recent orders list. Add &status={status} (canonical key: pending/processing/shipped/…) if the site
    # can filter server-side; otherwise the status filter is applied client-side on the fetched page.
    "orders_list": "/orders?page={page}&per_page={per_page}",
}

# key -> (default, group).  Values are comma-separated dot-paths tried in order.
FIELD_META = [
    ("list_key", "", "general"),     # where the array lives; '' = auto (root array, data, items, results...)
    ("pages_key", "total_pages,meta.last_page,meta.total_pages,pagination.total_pages,last_page,pages", "general"),
    ("cat_id", "id,slug", "category"),
    ("cat_name", "name,title", "category"),
    ("p_id", "id,slug", "product"),
    ("p_name", "name,title", "product"),
    ("p_price", "price,regular_price,sale_price", "product"),
    ("p_currency", "currency", "product"),
    ("p_image", "image,image_url,thumbnail,images", "product"),
    ("p_url", "url,link,permalink", "product"),
    ("p_desc", "short_description,description,summary", "product"),
    ("p_stock", "in_stock,stock_status,available", "product"),
    ("o_number", "number,order_number,id", "order"),
    ("o_status", "status,state", "order"),
    ("o_total", "total,grand_total", "order"),
    ("o_currency", "currency", "order"),
    ("o_date", "date,created_at,date_created", "order"),
    ("o_phone", "phone,billing.phone,customer.phone,mobile", "order"),
    ("o_tracking", "tracking_code,tracking,tracking_number", "order"),
    ("o_items", "items,line_items,products", "order"),
    ("i_name", "name,title", "order"),
    ("i_qty", "quantity,qty", "order"),
    ("o_name", "customer_name,billing.name,customer.name,customer.first_name,billing.first_name", "order"),   # appended last: keeps panel indices stable
]
DEFAULT_FIELDS = {k: d for k, d, _ in FIELD_META}
LIST_KEYS = ("data", "items", "results", "products", "categories", "orders")
AUTH_STYLES = ("bearer", "header", "query")
DEFAULT_AUTH_NAME = {"bearer": "Authorization", "header": "X-API-Key", "query": "api_key"}


def load_config():
    """Merge saved (admin panel) config with env fallbacks and defaults."""
    c = store.api_config()
    base = (c.get("base_url") or os.environ.get("SHOP_API_BASE_URL", "")).strip()
    key = (c.get("api_key") or os.environ.get("SHOP_API_KEY", "")).strip()
    style = c.get("auth_style") if c.get("auth_style") in AUTH_STYLES else "bearer"
    return {
        "enabled": bool(c.get("enabled", True)),
        "base_url": base, "api_key": key, "auth_style": style,
        "auth_name": (c.get("auth_name") or "").strip() or DEFAULT_AUTH_NAME[style],
        "paths": {**DEFAULT_PATHS, **{k: v for k, v in (c.get("paths") or {}).items() if v}},
        "fields": {**DEFAULT_FIELDS, **{k: v for k, v in (c.get("fields") or {}).items() if v}},
    }


def is_mock_env():
    return os.environ.get("SHOP_USE_MOCK", "0").strip() == "1"


# ----------------------------------------------------------------------------- interface
class ShopAPI(ABC):
    is_mock = False

    @abstractmethod
    def list_categories(self) -> List[Category]: ...

    @abstractmethod
    def list_products(self, category: Optional[str], page: int = 1) -> Page: ...

    @abstractmethod
    def get_product(self, product_id: str) -> Optional[Product]: ...

    @abstractmethod
    def search_products(self, query: str, page: int = 1) -> Page: ...

    @abstractmethod
    def get_order(self, order_number: str, phone_or_user: str) -> Optional[Order]:
        """Return the order only if `phone_or_user` (phone number) is verified as its owner,
        else None.  Raise VerificationUnavailable if verification is impossible."""

    @abstractmethod
    def list_user_orders(self, telegram_user: Optional[int] = None, phone: Optional[str] = None) -> List[Order]: ...

    # ---- admin-only (no phone verification; only ever called from the admin panel)
    def list_orders(self, page: int = 1, status: Optional[str] = None) -> Page:
        raise ShopError("list_orders not supported")

    def find_order(self, order_number: str) -> Optional[Order]:
        raise ShopError("find_order not supported")


class NotConfiguredAPI(ShopAPI):
    def _no(self, *a, **k):
        raise ShopNotConfigured("shop API not configured")
    list_categories = list_products = get_product = search_products = get_order = list_user_orders = _no
    list_orders = find_order = _no


# ----------------------------------------------------------------------------- mock
class MockShopAPI(ShopAPI):
    """Example data — every name is prefixed «[نمونه]» / "[MOCK]". Enabled only with SHOP_USE_MOCK=1."""
    is_mock = True
    MOCK_PHONE = "09120000000"

    CATS = [Category("skin", "[نمونه] مراقبت پوست / [MOCK] Skincare"),
            Category("makeup", "[نمونه] آرایش / [MOCK] Makeup"),
            Category("hair", "[نمونه] مو / [MOCK] Hair")]

    def __init__(self):
        self.products = []
        names = {"skin": ["سرم", "کرم مرطوب‌کننده", "ضدآفتاب", "پاک‌کننده", "تونر", "ماسک", "کرم دور چشم", "لایه‌بردار"],
                 "makeup": ["رژلب", "ریمل", "پنکیک", "خط چشم", "سایه"],
                 "hair": ["شامپو", "ماسک مو", "سرم مو"]}
        n = 0
        for cat, lst in names.items():
            for nm in lst:
                n += 1
                self.products.append(Product(
                    id=f"mock-{n}", name=f"[نمونه] {nm} #{n} / [MOCK]", price=100000 * n, currency="تومان",
                    image="", url="", description="این یک محصول نمونه است و واقعی نیست. / Example item, not real.",
                    in_stock=(n % 4 != 0), is_mock=True))
                self.products[-1].category = cat
        self.orders = [
            Order("MOCK-1001", "processing", "processing", 350000, "تومان", "2026-09-28", "", self.MOCK_PHONE,
                  [OrderItem("[نمونه] سرم", 1)], True),
            Order("MOCK-1002", "shipped", "shipped", 820000, "تومان", "2026-09-25", "MOCK-TRACK-123", self.MOCK_PHONE,
                  [OrderItem("[نمونه] رژلب", 2), OrderItem("[نمونه] ریمل", 1)], True),
            Order("MOCK-1003", "delivered", "delivered", 120000, "تومان", "2026-09-10", "", self.MOCK_PHONE,
                  [OrderItem("[نمونه] شامپو", 1)], True),
        ]

    @staticmethod
    def _page(lst, page):
        page = max(1, page)
        pages = max(1, -(-len(lst) // PER_PAGE))
        return Page(lst[(page - 1) * PER_PAGE: page * PER_PAGE], page, pages)

    def list_categories(self):
        return list(self.CATS)

    def list_products(self, category, page=1):
        lst = [p for p in self.products if not category or getattr(p, "category", None) == category]
        return self._page(lst, page)

    def get_product(self, product_id):
        return next((p for p in self.products if p.id == product_id), None)

    def search_products(self, query, page=1):
        q = (query or "").strip().lower()
        return self._page([p for p in self.products if q and q in p.name.lower()], page)

    def get_order(self, order_number, phone_or_user):
        o = next((o for o in self.orders if o.number.lower() == (order_number or "").strip().lower()), None)
        return o if o and phones_match(o.phone, phone_or_user) else None

    def list_user_orders(self, telegram_user=None, phone=None):
        return [o for o in self.orders if phone and phones_match(o.phone, phone)]

    def list_orders(self, page=1, status=None):
        lst = [o for o in self.orders if not status or o.status == status]
        return self._page(lst, page)

    def find_order(self, order_number):
        return next((o for o in self.orders if o.number.lower() == (order_number or "").strip().lower()), None)


# ----------------------------------------------------------------------------- real adapter
class HttpShopAPI(ShopAPI):
    """Generic JSON-over-HTTP adapter, configured from the admin panel (state.json) with env fallback.

    TODO(when the site's docs arrive) – usually NO code change is needed; in the admin panel
    (🔌 اتصال API) fill in:
      1. base URL + API key + auth style
      2. endpoint paths  (DEFAULT_PATHS above → the site's real routes)
      3. response-field mapping (FIELD_META above → the site's real JSON keys)
    Only if the site's shape can't be expressed with that (e.g. XML, GraphQL, POST-based lookup,
    HMAC signatures) edit `_request()` and the `_parse_*` methods below.
    """

    def __init__(self, cfg=None):
        self.cfg = cfg or load_config()
        tg.add_secret(self.cfg["api_key"])

    # ---- low level
    def _url_and_params(self, kind, **vals):
        """Fill the configured path template. Route values are percent-encoded here; query values are
        left raw because `requests` encodes params itself. Empty query values are dropped."""
        template = self.cfg["paths"][kind]
        vals.setdefault("per_page", PER_PAGE)
        route_t, _, query_t = template.partition("?")

        def val(m, quote):
            v = vals.get(m.group(1))
            v = "" if v is None else str(v)
            return urllib.parse.quote(v, safe="") if quote else v
        route = re.sub(r"\{(\w+)\}", lambda m: val(m, True), route_t)
        params = []
        for part in filter(None, query_t.split("&")):
            k, _, v = part.partition("=")
            v = re.sub(r"\{(\w+)\}", lambda m: val(m, False), v)
            if v != "":
                params.append((k, v))
        if "://" in route or route.startswith("//"):
            raise ShopError("invalid endpoint path")
        return self.cfg["base_url"].rstrip("/") + "/" + route.lstrip("/"), params

    def _request(self, kind, **vals):
        """GET <base><path>; returns (json, headers). Raises ShopError / ShopNotConfigured."""
        cfg = self.cfg
        if not (cfg["enabled"] and cfg["base_url"] and cfg["api_key"]):
            raise ShopNotConfigured("not configured")
        url, params = self._url_and_params(kind, **vals)
        headers = {"Accept": "application/json", "User-Agent": "ShopBot/1.0"}
        key, style, name = cfg["api_key"], cfg["auth_style"], cfg["auth_name"]
        if style == "bearer":
            headers["Authorization"] = "Bearer " + key
        elif style == "header":
            headers[name] = key
        else:
            params.append((name, key))
        try:
            r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT, allow_redirects=False)
        except requests.Timeout:
            raise ShopError("timeout") from None
        except requests.RequestException as e:      # never str(e): it may embed the URL with ?api_key=
            raise ShopError("connection error (" + type(e).__name__ + ")") from None
        sc = r.status_code
        if sc in (401, 403):
            raise ShopError(f"HTTP {sc} (authentication rejected)")
        if sc == 404:
            raise NotFound()
        if 300 <= sc < 400:
            raise ShopError(f"HTTP {sc} (redirect not followed)")
        if sc >= 400:
            raise ShopError(f"HTTP {sc}")
        if len(r.content) > MAX_BODY:
            raise ShopError("response too large")
        try:
            return r.json(), r.headers
        except ValueError:
            raise ShopError("response is not valid JSON") from None

    # ---- parsing (mapping driven)
    def _f(self, key):
        return self.cfg["fields"][key]

    def _list(self, data):
        lk = self._f("list_key")
        if lk:
            v = _walk(data, lk)
            return v if isinstance(v, list) else []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in LIST_KEYS:
                v = data.get(k)
                if isinstance(v, list):
                    return v
                if isinstance(v, dict):
                    for k2 in LIST_KEYS:
                        if isinstance(v.get(k2), list):
                            return v[k2]
        return []

    @staticmethod
    def _unwrap(obj):
        if isinstance(obj, dict):
            for k in ("data", "product", "order", "result"):
                if isinstance(obj.get(k), dict):
                    return obj[k]
        return obj

    def _pages(self, data, headers, page, n_items):
        v = extract(data, self._f("pages_key")) if isinstance(data, dict) else None
        if v is None:
            v = headers.get("X-WP-TotalPages") or headers.get("X-Total-Pages")
        try:
            return max(1, int(v))
        except (TypeError, ValueError):
            return page + 1 if n_items >= PER_PAGE else page

    def _parse_product(self, o):
        if not isinstance(o, dict):
            return None
        pid, name = extract(o, self._f("p_id")), extract(o, self._f("p_name"))
        if pid is None or not name:
            return None
        price = extract(o, self._f("p_price"))
        if isinstance(price, (dict, list)):
            price = None
        return Product(id=str(pid), name=_as_str(name), price=price,
                       currency=_as_str(extract(o, self._f("p_currency"))),
                       image=_as_str(extract(o, self._f("p_image"))), url=_as_str(extract(o, self._f("p_url"))),
                       description=_as_str(extract(o, self._f("p_desc"))),
                       in_stock=_as_bool(extract(o, self._f("p_stock"))))

    def _parse_order(self, o):
        if not isinstance(o, dict):
            return None
        num = extract(o, self._f("o_number"))
        if num is None:
            return None
        raw = _as_str(extract(o, self._f("o_status")))
        items = []
        for it in (extract(o, self._f("o_items")) or []):
            if isinstance(it, dict):
                nm = _as_str(extract(it, self._f("i_name")))
                if nm:
                    items.append(OrderItem(nm, extract(it, self._f("i_qty"))))
        total = extract(o, self._f("o_total"))
        return Order(number=str(num), status_raw=raw, status=normalize_status(raw),
                     total=None if isinstance(total, (dict, list)) else total,
                     currency=_as_str(extract(o, self._f("o_currency"))), date=_as_str(extract(o, self._f("o_date"))),
                     tracking=_as_str(extract(o, self._f("o_tracking"))),
                     phone=_as_str(extract(o, self._f("o_phone"))),
                     customer=_as_str(extract(o, self._f("o_name"))), items=items)

    # ---- interface
    def list_categories(self):
        try:
            data, _ = self._request("categories")
        except NotFound:
            raise ShopError("HTTP 404 (categories endpoint not found)") from None
        out = []
        for c in self._list(data):
            cid, nm = extract(c, self._f("cat_id")), extract(c, self._f("cat_name"))
            if cid is not None and nm:
                out.append(Category(str(cid), _as_str(nm)))
        return out

    def _page_of(self, kind, page, **vals):
        page = max(1, page)
        try:
            data, hdr = self._request(kind, page=page, **vals)
        except NotFound:
            return Page([], page, page)
        items = [p for p in (self._parse_product(x) for x in self._list(data)) if p]
        return Page(items[:PER_PAGE], page, self._pages(data, hdr, page, len(items)))

    def list_products(self, category, page=1):
        return self._page_of("products", page, category=category or "")

    def search_products(self, query, page=1):
        return self._page_of("search", page, q=(query or "").strip())

    def get_product(self, product_id):
        try:
            data, _ = self._request("product", id=product_id)
        except NotFound:
            return None
        return self._parse_product(self._unwrap(data))

    def get_order(self, order_number, phone_or_user):
        phone = phone_or_user or ""
        try:
            data, _ = self._request("order", order=order_number, phone=phone, user=phone)
        except NotFound:
            return None
        data = self._unwrap(data)
        if isinstance(data, list):                      # some sites answer a search with a list
            data = next((x for x in data if str(extract(x, self._f("o_number"))) == str(order_number)), None)
        o = self._parse_order(data)
        if not o:
            return None
        if o.phone:
            return o if phones_match(o.phone, phone) else None
        if "{phone}" in self.cfg["paths"]["order"]:      # the API itself checks the phone (its own rule)
            return o
        raise VerificationUnavailable("order has no phone field to verify against")

    def list_user_orders(self, telegram_user=None, phone=None):
        try:
            data, _ = self._request("user_orders", phone=phone or "", user=telegram_user or "")
        except NotFound:
            return []
        orders = [o for o in (self._parse_order(x) for x in self._list(data)) if o]
        if phone:
            orders = [o for o in orders if not o.phone or phones_match(o.phone, phone)]
        return orders

    # ---- admin-only order access (never reachable from user handlers)
    def list_orders(self, page=1, status=None):
        page = max(1, page)
        template = self.cfg["paths"]["orders_list"]
        try:
            data, hdr = self._request("orders_list", page=page, status=(status or "") if "{status}" in template else "")
        except NotFound:
            raise ShopError("HTTP 404 (orders list endpoint not found)") from None
        raw = self._list(data)
        orders = [o for o in (self._parse_order(x) for x in raw) if o]
        if status and "{status}" not in template:
            orders = [o for o in orders if o.status == status]
        return Page(orders[:50], page, self._pages(data, hdr, page, len(raw)))

    def find_order(self, order_number):
        try:
            data, _ = self._request("order", order=order_number, phone="", user="")
        except NotFound:
            return None
        data = self._unwrap(data)
        if isinstance(data, list):
            data = next((x for x in data if str(extract(x, self._f("o_number"))) == str(order_number)), None)
        return self._parse_order(data)

    # ---- admin "Test connection"
    def test_connection(self):
        """Calls the products endpoint. Returns a dict with only safe, short info (no secrets, no raw body)."""
        t0 = time.time()
        res = {"ok": False, "status": None, "ms": 0, "count": 0, "sample": "", "keys": [], "error": ""}
        try:
            data, _ = self._request("products", page=1, category="")
        except NotFound:
            res.update(status=404, error="HTTP 404 (endpoint not found — check the products path)")
        except ShopError as e:
            m = re.match(r"HTTP (\d+)", str(e))
            res.update(status=int(m.group(1)) if m else None, error=str(e))
        else:
            items = self._list(data)
            parsed = [p for p in (self._parse_product(x) for x in items) if p]
            res.update(ok=True, status=200, count=len(items))
            if parsed:
                res["sample"] = parsed[0].name[:40]
            else:
                res["keys"] = [str(k)[:20] for k in (data if isinstance(data, dict) else
                               (items[0] if items and isinstance(items[0], dict) else {})).keys()][:12]
        res["ms"] = int((time.time() - t0) * 1000)
        return res


# ----------------------------------------------------------------------------- factory
def real_configured():
    c = load_config()
    return bool(c["enabled"] and c["base_url"] and c["api_key"])


def get_api() -> ShopAPI:
    """Real API if configured+enabled, else mock (only if SHOP_USE_MOCK=1), else NotConfiguredAPI."""
    if real_configured():
        return HttpShopAPI()
    if is_mock_env():
        return MockShopAPI()
    return NotConfiguredAPI()


def api_state():
    """'real' | 'mock' | 'none' – for the stats screen."""
    return "real" if real_configured() else ("mock" if is_mock_env() else "none")

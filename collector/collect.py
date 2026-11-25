#!/usr/bin/env python3
"""Printer Prices TR - hourly collector.

Finds the watchlist printers on Turkish store sites (through each store's
sitemap or search page), reads price and stock status from every product
page and writes docs/data/prices.json for the web app.

Standard library only, so it runs on a bare GitHub Actions runner.

    python collector/collect.py                 # full run
    python collector/collect.py --only robo90   # one store, for testing
    python collector/collect.py --probe         # test stores, write nothing
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import gzip
import html
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORES_FILE = Path(__file__).resolve().parent / "stores.json"
OUT_FILE = ROOT / "docs" / "data" / "prices.json"
CACHE_FILE = Path(__file__).resolve().parent / "known_urls.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
TIMEOUT = 25
DELAY = 0.6              # pause between two requests to the same store
MAX_PRODUCTS = 400       # product pages read per store per run
MAX_SITEMAPS = 400       # child sitemaps read per store
REDISCOVER_HOURS = 6     # how often sitemaps are re-read for new listings
KEEP_STALE_DAYS = 14     # unreadable offers are kept (marked old) this long

# --------------------------------------------------------------------------
# Watchlist
# --------------------------------------------------------------------------
# (brand, model label, regex on the folded product name, lowest believable
#  price in TL - anything cheaper is an accessory or spare part)
MODELS = [
    ("Bambu Lab", "X2D", r"\bx2d\b", 30000),
    ("Bambu Lab", "A2L", r"\ba2l\b", 15000),
    ("Bambu Lab", "P1P", r"\bp1p\b", 12000),
    ("Bambu Lab", "P1S", r"\bp1s\b", 15000),
    ("Bambu Lab", "P2S", r"\bp2s\b", 20000),
    ("Bambu Lab", "H2D Pro", r"\bh2d\s*pro\b", 60000),
    ("Bambu Lab", "H2D", r"\bh2d\b", 50000),
    ("Bambu Lab", "H2S", r"\bh2s\b", 40000),
    ("Bambu Lab", "H2C", r"\bh2c\b", 60000),
    ("Creality", "K2 Plus", r"\bk2\s*plus\b", 35000),
    ("Creality", "Ender-5 Max", r"\bender\s*5\s*max\b", 20000),
    ("Elegoo", "Centauri Carbon 2", r"\bcentauri\s*carbon\s*2\b", 12000),
    ("Elegoo", "Saturn 4 Ultra 16K", r"\bsaturn\s*4\s*ultra\s*16k\b", 12000),
]
BRAND_ORDER = ["Bambu Lab", "Creality", "Elegoo"]

# Product URLs worth opening (cheap pre-filter on the URL itself).
URL_RE = re.compile(
    r"(?<![a-z0-9])(h2d|h2s|h2c|p1s|p1p|p2s|x2d|a2l)(?![a-z0-9])"
    r"|k2-?plus|ender-?5-?max|centauri-?carbon-?2|saturn-?4-?ultra")
# Names that are clearly parts made *for* a printer, not the printer.
ACCESSORY_RE = re.compile(
    r"\b(icin|uyumlu|for|compatible|yedek|spare|nozzle|nozul|hotend|"
    r"plaka|plate|tabla|kablo|cable|sensor|ptfe|kayis|belt|termistor|"
    r"isitici|heater|ekstruder|extruder|kapak|cover|fan|fani|"
    r"upgrade|yukseltme|module|modul|modulu|board|touchscreen|ekran|"
    r"screen|karti|motor|filtre|filter)\b")
SKIP_SITEMAP_RE = re.compile(
    r"(image|img|resim|blog|kategori|categor|brand|marka|tag|etiket|"
    r"author|yazar|post|page|sayfa|news|haber|video|collection|static)",
    re.I)


def fold(text: str) -> str:
    """Lower-case, strip Turkish accents and punctuation."""
    text = (text or "").replace("İ", "i").replace("I", "i").replace("ı", "i")
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def classify(name: str, price: float | None):
    """Return (brand, model, variant) for a watchlist printer, else None."""
    f = fold(name)
    for brand, model, rx, floor in MODELS:
        if not re.search(rx, f):
            continue
        if brand == "Bambu Lab" and "bambu" not in f:
            continue
        if ACCESSORY_RE.search(f):
            return None
        if price is not None and price < floor:
            return None
        if model == "H2D" and re.search(
                r"\bpro\b", re.sub(r"ams\s*2\s*pro", " ", f)):
            model = "H2D Pro"            # e.g. "H2D AMS Pro"
        return brand, model, variant_of(brand, model, f)
    return None


def variant_of(brand: str, model: str, f: str) -> str:
    """Version of the printer: alone, combo, laser combo..."""
    if brand == "Bambu Lab":
        laser = re.search(r"\b(10|40)\s*w\b", f)
        if "laser" in f or "lazer" in f or laser:
            watt = f" {laser.group(1)}W" if laser else ""
            if not watt and model == "H2S":
                watt = " 10W"            # the H2S is only sold with 10 W
            return f"Laser Full Combo{watt}"
        if re.search(r"\b(dual|cift|2x|2 adet)\b", f) and "ams" in f:
            return "Combo + Dual AMS 2 Pro"
        if re.search(r"\bams\s*ht\b", f):
            return "Combo + AMS HT"
        if model in ("P1S", "P1P") and re.search(r"\bams\s*2\b", f):
            return "AMS 2 Pro Combo"
        if re.search(r"\b(combo|ams)\b", f):
            return "Combo"
        return ""
    if model == "Saturn 4 Ultra 16K":
        return "Bundle" if re.search(r"\b(bundle|set|seti|paket|mercury)\b", f) else ""
    if model == "K2 Plus":
        return "Combo" if re.search(r"\b(combo|cfs)\b", f) else ""
    if model == "Centauri Carbon 2":
        return "Combo" if re.search(r"\b(combo|canvas)\b", f) else ""
    return ""


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Blocked(Exception):
    pass


def fetch(url: str, accept: str = "text/html,application/xhtml+xml,*/*;q=0.8") -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": accept,
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip",
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:      # asked to slow down
                wait = e.headers.get("Retry-After", "")
                time.sleep(min(int(wait), 60) if wait.isdigit() else 8 * (attempt + 1))
                continue
            if e.code in (401, 403, 429, 503):
                raise Blocked(f"HTTP {e.code}") from None
            raise
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8", errors="replace")
    head = text[:3000].lower()
    if ("attention required" in head or "just a moment" in head
            or "captcha" in head or "access denied" in head
            or "güvenlik doğrulaması" in head):
        raise Blocked("bot check page")
    return text


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
LOC_RE = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", re.S)


def url_matches(url: str) -> bool:
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path).lower()
    return bool(URL_RE.search(fold(path).replace(" ", "-")))


def discover_sitemap(store: dict, log: list) -> list[str]:
    base = store["base"].rstrip("/")
    starts = store.get("sitemaps")
    if not starts:
        starts = []
        try:
            robots = fetch(base + "/robots.txt", "text/plain,*/*")
            starts = [m.strip() for m in
                      re.findall(r"(?im)^sitemap:\s*(\S+)", robots)]
        except Exception:
            pass
        starts = [s if s.startswith("http") else base + "/" + s.lstrip("/")
                  for s in starts] or [base + "/sitemap.xml"]
    seen, queue, found, read_ok = set(), list(dict.fromkeys(starts)), [], 0
    while queue and len(seen) < MAX_SITEMAPS:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            xml = fetch(sm, "application/xml,text/xml,*/*")
        except Exception as e:  # noqa: BLE001
            log.append(f"sitemap {sm}: {e}")
            continue
        read_ok += 1
        time.sleep(DELAY / 2)
        locs = [html.unescape(x) for x in LOC_RE.findall(xml)]
        if "<sitemapindex" in xml[:2000].lower():
            for loc in locs:
                if not SKIP_SITEMAP_RE.search(urllib.parse.urlsplit(loc).path):
                    queue.append(loc)
        else:
            found += [u for u in locs if url_matches(u)]
    if not read_ok:
        raise Blocked("product list (sitemap) could not be read")
    return list(dict.fromkeys(found))


SEARCH_TERMS = ["bambu lab h2d", "bambu lab h2s", "bambu lab h2c",
                "bambu lab p1s", "bambu lab p2s", "bambu lab x2d",
                "bambu lab a2l", "creality k2 plus", "creality ender 5 max",
                "elegoo centauri carbon 2", "elegoo saturn 4 ultra 16k"]


def discover_search(store: dict, log: list) -> list[str]:
    base = store["base"].rstrip("/")
    link_re = re.compile(store.get("link_re", r'href="([^"#?]+)'))
    found = []
    for term in SEARCH_TERMS:
        url = store["search"].replace("{q}", urllib.parse.quote_plus(term))
        try:
            page = fetch(url)
        except Blocked:
            raise
        except Exception as e:  # noqa: BLE001
            log.append(f"search {term}: {e}")
            continue
        time.sleep(DELAY)
        for href in link_re.findall(page):
            href = html.unescape(href)
            full = urllib.parse.urljoin(base + "/", href)
            if full.startswith(base) and url_matches(full):
                found.append(full.split("?")[0])
    return list(dict.fromkeys(found))


# --------------------------------------------------------------------------
# Product page
# --------------------------------------------------------------------------
LD_RE = re.compile(
    r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
PREORDER_RE = re.compile(r"on\s*siparis|pre\s*order|preorder|on\s*satis")


def to_price(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) or None
    s = re.sub(r"[^\d.,]", "", str(v))
    if not s:
        return None
    if "," in s and "." in s:           # 1.234,56 or 1,234.56
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if re.search(r",\d{1,2}$", s) else s.replace(",", "")
    elif s.count(".") > 1 or re.search(r"\.\d{3}$", s):
        s = s.replace(".", "")
    try:
        return float(s) or None
    except ValueError:
        return None


def iter_ld(page: str):
    for block in LD_RE.findall(page):
        try:
            data = json.loads(block.strip(), strict=False)
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                if "@graph" in node:
                    stack.append(node["@graph"])


MICRO = {
    "name": re.compile(r"itemprop=[\"']name[\"'][^>]*>\s*([^<]{3,200})<"),
    "price": re.compile(r"itemprop=[\"']price[\"'][^>]*content=[\"']([\d.,]+)"),
    "avail": re.compile(r"itemprop=[\"']availability[\"'][^>]*(?:href|content)=[\"']([^\"']+)"),
}
TICIMAX_STOCK = re.compile(
    r'"stokKodu":"[^"]*","barkod":"[^"]*"[^{}]{0,120}?"stokAdedi":([\d.]+)')


def status_from(avail: str) -> str:
    avail = (avail or "").lower()
    if "preorder" in avail or "presale" in avail or "backorder" in avail:
        return "pre_order"
    if "outofstock" in avail or "soldout" in avail or "discontinued" in avail:
        return "out_of_stock"
    if "instock" in avail or "limitedavailability" in avail or "onlineonly" in avail:
        return "in_stock"
    return "unknown"


def parse_product(page: str, store: dict) -> dict | None:
    """name / price / status from a product page (schema.org data)."""
    p = parse_ld(page) or parse_micro(page)
    if not p:
        return None
    if store.get("price_re"):            # e.g. store publishes prices ex-VAT
        m = re.search(store["price_re"], page, re.S)
        p["price"] = to_price(m.group(1)) if m else None
    if p["status"] == "unknown":
        m = TICIMAX_STOCK.search(page)
        if m:
            p["status"] = "in_stock" if float(m.group(1)) > 0 else "out_of_stock"
    p["note"] = ""
    if any(re.search(m, page) for m in store.get("instock_markers", [])):
        if p["status"] != "out_of_stock":
            p["status"] = "in_stock"
    elif is_preorder(p["name"], page, store):
        if p["status"] == "out_of_stock":
            p["note"] = "Store page mentions pre-order, but it cannot be ordered now."
        else:
            p["status"] = "pre_order"
    return p


def parse_micro(page: str) -> dict | None:
    name, price = MICRO["name"].search(page), MICRO["price"].search(page)
    if not name or not price:
        return None
    avail = MICRO["avail"].search(page)
    return {"name": html.unescape(name.group(1)).strip(),
            "price": to_price(price.group(1)),
            "status": status_from(avail.group(1) if avail else ""), "seller": ""}


def parse_ld(page: str) -> dict | None:
    for node in iter_ld(page):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if "Product" not in types:
            continue
        offers = node.get("offers")
        offers = offers if isinstance(offers, list) else [offers]
        offers = [o for o in offers if isinstance(o, dict)]
        if not offers:
            continue
        o = offers[0]
        price = to_price(o.get("price") or o.get("lowPrice"))
        if price is None and isinstance(o.get("priceSpecification"), dict):
            price = to_price(o["priceSpecification"].get("price"))
        name = html.unescape(str(node.get("name") or "")).strip()
        seller = o.get("seller") if isinstance(o.get("seller"), dict) else {}
        status = status_from(str(o.get("availability") or ""))
        return {"name": name, "price": price, "status": status,
                "seller": str(seller.get("name") or "")}
    return None


# Sentences stores write on the product itself (not menu or badge text).
PREORDER_PAGE_RE = re.compile(
    r"on siparis urunudur|on siparis ile satisa|on siparis kapsaminda|"
    r"sadece on siparise|on siparis asamasinda|on sipariste")
STRIP_RE = re.compile(r"(?s)<(script|style|template)[^>]*>.*?</\1>")


def is_preorder(name: str, page: str, store: dict) -> bool:
    if PREORDER_RE.search(fold(name)):
        return True
    if any(re.search(m, page) for m in store.get("preorder_markers", [])):
        return True
    return bool(PREORDER_PAGE_RE.search(fold(STRIP_RE.sub(" ", page))))


def run_shopify(store: dict, now: str, log: list) -> list[dict]:
    """Shopify stores publish their whole catalogue as JSON: a few requests."""
    base, offers = store["base"].rstrip("/"), []
    for page_no in range(1, 30):
        data = json.loads(fetch(f"{base}/products.json?limit=250&page={page_no}",
                                "application/json"))
        products = data.get("products", [])
        for prod in products:
            variants = prod.get("variants") or []
            if not variants:
                continue
            live = [v for v in variants if v.get("available")]
            price = min(to_price(v.get("price")) or 9e12 for v in (live or variants))
            cls = classify(prod.get("title", ""), price)
            if not cls:
                continue
            tags = fold(" ".join(prod.get("tags") or []))
            if "yedekparca" in tags.replace(" ", "") or "aksesuar" in tags:
                continue
            status = "in_stock" if live else "out_of_stock"
            if live and PREORDER_RE.search(fold(prod["title"]) + " " + tags):
                status = "pre_order"
            offers.append({
                "brand": cls[0], "model": cls[1], "variant": cls[2],
                "store": store["name"], "store_id": store["id"], "seller": "",
                "title": prod["title"].strip(),
                "url": f"{base}/products/{prod['handle']}",
                "price_try": round(price, 2), "status": status, "note": "",
                "checked_at": now, "stale": False})
        if len(products) < 250:
            break
        time.sleep(2)
    return offers


# --------------------------------------------------------------------------
# One store
# --------------------------------------------------------------------------
def run_store(store: dict, known: dict, now: str, rediscover: bool) -> dict:
    log: list[str] = []
    sid = store["id"]
    urls = list(known.get(sid, {}).get("urls", []))
    skip = set(known.get(sid, {}).get("skip", []))
    result = {"id": sid, "name": store["name"], "offers": [], "ok": False,
              "skip": skip,
              "reason": "", "log": log, "urls": urls, "failed": set(),
              "discovered_at": known.get(sid, {}).get("discovered_at")}
    try:
        if store.get("platform") == "shopify":
            result["offers"] = dedupe(run_shopify(store, now, log))
            result["ok"] = True
            return result
        if rediscover or not urls:
            fresh = (discover_search if store.get("search") else discover_sitemap)(store, log)
            urls = [u for u in dict.fromkeys(fresh + store.get("extra_urls", []))
                    if u not in skip]
            result["urls"], result["discovered_at"] = urls, now
        read = 0
        for url in urls[:MAX_PRODUCTS]:
            try:
                page = fetch(url)
            except Blocked:
                raise
            except Exception as e:  # noqa: BLE001
                log.append(f"{url}: {e}")
                result["failed"].add(url)
                continue
            finally:
                time.sleep(DELAY)
            read += 1
            p = parse_product(page, store)
            if not p or not p["name"]:
                continue
            if not classify(p["name"], None):
                skip.add(url)            # a part or another model: never a printer
                continue
            if p["price"] is None:
                continue
            cls = classify(p["name"], p["price"])
            if not cls:
                continue
            result["offers"].append({
                "brand": cls[0], "model": cls[1], "variant": cls[2],
                "store": store["name"], "store_id": sid,
                "seller": p["seller"] if store.get("marketplace") else "",
                "title": p["name"], "url": url, "price_try": round(p["price"], 2),
                "status": p["status"], "note": p.get("note", ""),
                "checked_at": now, "stale": False,
            })
        result["urls"] = [u for u in urls if u not in skip]
        result["ok"] = read > 0 or not urls
        if not urls:
            result["reason"] = "no watchlist printers listed"
        elif read == 0:
            result["reason"] = "product pages could not be read"
    except Blocked as e:
        result["reason"] = f"refuses automated reading ({e})"
    except Exception as e:  # noqa: BLE001
        result["reason"] = f"error: {e}"
    result["offers"] = dedupe(result["offers"])
    return result


def dedupe(offers: list[dict]) -> list[dict]:
    """Drop language duplicates (same store, same version, same price)."""
    best: dict[tuple, dict] = {}
    for o in offers:
        key = (o["model"], o["variant"], o["price_try"], o["status"])
        cur = best.get(key)
        if cur is None or (re.search(r"-en(-\d+)?$", cur["url"])
                           and not re.search(r"-en(-\d+)?$", o["url"])):
            best[key] = o
    return list(best.values())


# --------------------------------------------------------------------------
# Exchange rate
# --------------------------------------------------------------------------
def usd_try() -> dict | None:
    sources = [
        ("Truncgil Finans", "https://finans.truncgil.com/v4/today.json",
         lambda d: ((float(d["USD"]["Buying"]) + float(d["USD"]["Selling"])) / 2,
                    dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))),
        ("Yahoo Finance",
         "https://query1.finance.yahoo.com/v8/finance/chart/USDTRY=X?interval=1m&range=1d",
         lambda d: (d["chart"]["result"][0]["meta"]["regularMarketPrice"],
                    dt.datetime.fromtimestamp(
                        d["chart"]["result"][0]["meta"]["regularMarketTime"],
                        dt.timezone.utc).isoformat(timespec="seconds"))),
        ("open.er-api.com", "https://open.er-api.com/v6/latest/USD",
         lambda d: (d["rates"]["TRY"],
                    dt.datetime.fromtimestamp(
                        d["time_last_update_unix"],
                        dt.timezone.utc).isoformat(timespec="seconds"))),
    ]
    for name, url, pick in sources:
        try:
            text = fetch(url, "application/json")
            try:
                data = json.loads(text)
            except ValueError:       # feed is sometimes not strictly valid
                usd = re.search(r'"USD"\s*:\s*(\{[^}]*\})', text)
                data = {"USD": json.loads(usd.group(1))}
            rate, at = pick(data)
            if 1 < float(rate) < 1000:
                return {"usd_try": round(float(rate), 4), "source": name, "at": at}
        except Exception as e:  # noqa: BLE001
            print(f"rate source {name} failed: {e}", file=sys.stderr)
    return None


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma separated store ids")
    ap.add_argument("--probe", action="store_true", help="test, write nothing")
    ap.add_argument("--rediscover", action="store_true")
    args = ap.parse_args()

    stores = json.loads(STORES_FILE.read_text(encoding="utf-8"))
    blocked_list = [s for s in stores if s.get("blocked")]
    stores = [s for s in stores if not s.get("blocked")]
    if args.only:
        wanted = set(args.only.split(","))
        stores = [s for s in stores if s["id"] in wanted]

    now_dt = dt.datetime.now(dt.timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    known = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
    previous = json.loads(OUT_FILE.read_text(encoding="utf-8")) if OUT_FILE.exists() else {}

    def needs_rediscovery(store) -> bool:
        if args.rediscover:
            return True
        last = known.get(store["id"], {}).get("discovered_at")
        if not last:
            return True
        age = now_dt - dt.datetime.fromisoformat(last)
        return age > dt.timedelta(hours=REDISCOVER_HOURS)

    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(
            lambda s: run_store(s, known, now, needs_rediscovery(s)), stores))

    for r in results:
        print(f"{r['id']:<18} ok={r['ok']!s:<5} urls={len(r['urls']):<3} "
              f"offers={len(r['offers']):<3} {r['reason']}")
        if args.probe:
            for o in r["offers"]:
                print(f"    {o['model']:<18} {o['variant']:<22} "
                      f"{o['price_try']:>11,.2f} {o['status']:<12} {o['title'][:60]}")
            for line in r["log"][:4]:
                print("    !", line[:150])
    if args.probe:
        return 0

    # Offers read this run, plus last known offers we could not re-read.
    offers = [o for r in results for o in r["offers"]]
    fresh_urls = {o["url"] for o in offers}
    by_id = {r["id"]: r for r in results}
    cutoff = now_dt - dt.timedelta(days=KEEP_STALE_DAYS)
    for m in previous.get("models", []):
        for o in m.get("offers", []):
            if o["url"] in fresh_urls:
                continue
            carried = {"note": "", **o, "brand": m["brand"],
                       "model": m["model"], "variant": m["variant"]}
            r = by_id.get(o["store_id"])
            if r is None:                    # store not part of this run
                offers.append(carried)
                continue
            if r["ok"] and o["url"] not in r["failed"]:
                continue                     # store answered: listing is gone
            if dt.datetime.fromisoformat(o["checked_at"]) >= cutoff:
                offers.append({**carried, "stale": True})

    groups: dict[tuple, dict] = {}
    for o in offers:
        key = (o["brand"], o["model"], o["variant"])
        g = groups.setdefault(key, {
            "brand": o["brand"], "model": o["model"], "variant": o["variant"],
            "name": " ".join(x for x in (o["brand"], o["model"], o["variant"]) if x),
            "offers": []})
        g["offers"].append({k: o[k] for k in (
            "store", "store_id", "seller", "title", "url", "price_try",
            "status", "note", "checked_at", "stale")})
    model_rank = {(b, m): i for i, (b, m, _, _) in enumerate(MODELS)}
    models = sorted(groups.values(), key=lambda g: (
        BRAND_ORDER.index(g["brand"]), model_rank[(g["brand"], g["model"])],
        g["variant"]))
    status_rank = {"in_stock": 0, "pre_order": 0, "unknown": 0, "out_of_stock": 1}
    for g in models:
        g["offers"].sort(key=lambda o: (status_rank[o["status"]], o["price_try"]))

    rate = usd_try() or previous.get("rate")
    covered = [{"name": r["name"], "offers": len(r["offers"])}
               for r in results if r["ok"]]
    not_covered = ([{"name": r["name"], "reason": r["reason"]}
                    for r in results if not r["ok"]]
                   + [{"name": s["name"], "reason": s["blocked"]} for s in blocked_list])
    if args.only:                # test run: keep the other stores' entries
        mine = {r["name"] for r in results}
        prev = previous.get("stores", {})
        covered += [s for s in prev.get("covered", []) if s["name"] not in mine]
        not_covered = [s for s in prev.get("not_covered", [])
                       if s["name"] not in mine] + [
                           s for s in not_covered if s["name"] in mine]
    out = {"collected_at": now, "rate": rate, "models": models,
           "stores": {"covered": sorted(covered, key=lambda s: s["name"].lower()),
                      "not_covered": sorted(not_covered, key=lambda s: s["name"].lower())}}
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in results:
        known[r["id"]] = {"urls": r["urls"], "skip": sorted(r.get("skip", [])),
                          "discovered_at": r["discovered_at"]}
    CACHE_FILE.write_text(json.dumps(known, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {OUT_FILE} - {sum(len(g['offers']) for g in models)} offers, "
          f"{len(models)} versions, rate {rate}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

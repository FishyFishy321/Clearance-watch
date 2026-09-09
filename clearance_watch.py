#!/usr/bin/env python3
"""
clearance_watch.py  (Playwright, page-by-page)
----------------------------------------------
Loads the Sportsman's Warehouse clearance URL page by page (adds &page=0,
&page=1, ... until a page adds nothing new), collects every product, and
reports what CHANGED since the last run: new items + price changes, sorted
by percent off retail. Alerts go to Telegram.
"""

import json
import os
import re
import sys

try:
    import requests  # Telegram only
except ImportError:
    requests = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Playwright isn't installed. The GitHub workflow installs it automatically.")
    sys.exit(1)

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

CLEARANCE_URL = "https://www.sportsmans.com/deals-clearance/fishing-clearance/c/cat101209?q=%3Aprice-desc%3AdefaultParentCategory%3Acat101045%3AdefaultParentCategory%3Acat101039%3AdefaultParentCategory%3Acat101028%3AdefaultParentCategory%3Acat101036%3AdefaultParentCategory%3Acat101038%3AdefaultParentCategory%3Acat112005%3AdefaultParentCategory%3Acat112000%3AdefaultParentCategory%3Acat135701%3AdefaultParentCategory%3Acat135700%3AdefaultParentCategory%3Acat101051%3AdefaultParentCategory%3Acat101037%3AdefaultParentCategory%3Acat101052%3AdefaultParentCategory%3Acat101041%3AdefaultParentCategory%3Acat101034%3AdefaultParentCategory%3Acat101035%3AshipOption%3ASHIPTOYOU&page=0"

KEYWORDS = []  # empty = every product the URL shows; the URL does the filtering

STORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_snapshot.json")
RENDERED_DUMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rendered_page.html")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _to_float(price):
    try:
        return float(str(price).replace("$", "").replace(",", ""))
    except Exception:
        return 0.0


def _pct_off(orig, sale):
    o, s = _to_float(orig), _to_float(sale)
    if o and s and o > s:
        return round((o - s) / o * 100)
    return None


def is_real_price(p):
    return isinstance(p, str) and p.startswith("$")


def _money(raw):
    if not raw:
        return None
    m = re.search(r"[\d,]+(?:\.\d{1,2})?", str(raw))
    if not m:
        return None
    try:
        return f"${float(m.group(0).replace(',', '')):.2f}"
    except Exception:
        return None


def _anchor_fallback(page):
    """Old method: read links whose visible text contains a price."""
    found = {}
    kw = [k.lower() for k in KEYWORDS]
    for a in page.query_selector_all("a"):
        try:
            text = (a.inner_text() or "").strip()
            href = a.get_attribute("href") or ""
        except Exception:
            continue
        if not text:
            continue
        prices = re.findall(r"\$[\d,]+\.\d{2}", text)
        if not prices:
            continue
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        name_lines = [l for l in lines if "$" not in l]
        name = (name_lines[0] if name_lines else lines[0])[:120]
        if len(name) < 5:
            continue
        if kw and not any(k in name.lower() for k in kw):
            continue
        uniq = sorted(set(prices), key=_to_float)
        url = href if href.startswith("http") else ("https://www.sportsmans.com" + href if href.startswith("/") else None)
        found[name] = {"name": name, "price": uniq[0], "orig": (uniq[-1] if len(uniq) > 1 else None), "url": url}
    return found


def extract_from_dom(page):
    """Read product tiles by their labeled data attributes (precise)."""
    found = {}
    kw = [k.lower() for k in KEYWORDS]
    tiles = page.query_selector_all(".product-item")
    if not tiles:
        return _anchor_fallback(page)

    for el in tiles:
        try:
            name = (el.get_attribute("data-cnstrc-item-name") or "").strip()
            if not name:
                a = el.query_selector("a.name")
                name = (a.inner_text().strip() if a else "")
            sale = _money(el.get_attribute("data-cnstrc-item-price"))
            url_raw = el.get_attribute("data-product-url") or ""
        except Exception:
            continue
        if not name:
            continue
        if kw and not any(k in name.lower() for k in kw):
            continue

        if not sale:
            pe = el.query_selector(".smw-sale-price, .price")
            if pe:
                m = re.search(r"\$[\d,]+\.\d{2}", pe.inner_text() or "")
                if m:
                    sale = m.group(0)

        orig = None
        try:
            strike = el.query_selector(".price-strikethrough")
            if strike:
                orig = _money(strike.inner_text())
        except Exception:
            pass

        url = ("https://www.sportsmans.com" + url_raw) if url_raw.startswith("/") else (url_raw or None)
        found[name] = {"name": name, "price": sale or "price n/a", "orig": orig, "url": url}
    return found


# ----------------------------------------------------------------------------
# Browser: load one page, reveal all items, collect everything
# ----------------------------------------------------------------------------

TILE_COUNT_JS = "() => document.querySelectorAll('.product-item').length"

CLICK_MORE_JS = """() => {
    const rx = /(show more|load more|view more|see more|more product|more result)/i;
    let els = Array.from(document.querySelectorAll('button, a'));
    let b = els.find(e => rx.test((e.innerText || '').trim()) && e.offsetParent !== null);
    if (b) { b.scrollIntoView({block: 'center'}); b.click(); return 'text'; }
    const sels = ['.js-show-more', '.js-load-more', '[class*=\"show-more\"]',
                  '[class*=\"load-more\"]', 'a[rel=\"next\"]',
                  '.pagination a.next', '.pagination__next'];
    for (const sel of sels) {
        const el = document.querySelector(sel);
        if (el && el.offsetParent !== null) { el.scrollIntoView({block: 'center'}); el.click(); return sel; }
    }
    return '';
}"""


def render_and_extract():
    products = {}
    first_html, diag = "", {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT, viewport={"width": 1366, "height": 2200})
        page.goto(CLEARANCE_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_function("document.body.innerText.includes('$')", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(2500)

        last, stable = -1, 0
        for _ in range(45):
            # accumulate whatever is currently loaded (robust to append or replace)
            products.update(extract_from_dom(page))
            cur = len(products)
            if cur != last:
                print(f"  {cur} products loaded...")

            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            page.wait_for_timeout(900)
            try:
                clicked = page.evaluate(CLICK_MORE_JS)
            except Exception:
                clicked = ""
            if clicked:
                page.wait_for_timeout(1600)

            if cur == last and not clicked:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable = 0
                last = cur

        products.update(extract_from_dom(page))  # final sweep
        first_html = page.content()
        try:
            diag = {
                "title": page.title(),
                "html_len": len(first_html),
                "product_item_count": page.evaluate(TILE_COUNT_JS),
                "price_count": len(re.findall(r"\$[\d,]+\.\d{2}", first_html)),
                "body_sample": (page.inner_text("body") or "")[:1500],
            }
        except Exception:
            diag = {}

        browser.close()

    result = list(products.values())
    for pr in result:
        pr["pct_off"] = _pct_off(pr.get("orig"), pr.get("price"))
    return result, first_html, diag


# ----------------------------------------------------------------------------
# Snapshot + alerts
# ----------------------------------------------------------------------------

def load_snapshot():
    if not os.path.exists(STORE_FILE):
        return {}
    try:
        with open(STORE_FILE, "r") as f:
            return dict(json.load(f))
    except Exception:
        return {}


def save_snapshot(snapshot):
    with open(STORE_FILE, "w") as f:
        json.dump(snapshot, f, indent=2)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id or requests is None:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=30,
        )
        if not r.ok:
            print(f"Telegram error {r.status_code}: {r.text[:200]}")
        return r.ok
    except Exception as e:
        print(f"Telegram send failed: {e}")
        return False


def notify(text):
    if send_telegram(text):
        print("Alert sent to Telegram.")


def _by_discount(item):
    pct = item.get("pct_off")
    return (pct is None, -(pct or 0))


def _fmt(item):
    if item.get("pct_off") is not None:
        line = f"{item['name']} - {item['price']} ({item['pct_off']}% off, was {item['orig']})"
    else:
        line = f"{item['name']} ({item['price']})"
    if item.get("url"):
        line += f"\n  {item['url']}"
    return line

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    print("Loading clearance pages in a headless browser...")
    try:
        products, html, diag = render_and_extract()
    except Exception as e:
        print(f"Browser/render failed: {e}")
        return

    print(f"Found {len(products)} product items across all pages.")

    if not products:
        try:
            with open(RENDERED_DUMP, "w") as f:
                f.write(html)
        except Exception:
            pass
        print("\n=== DIAGNOSTIC START ===")
        for k in ("title", "html_len", "product_item_count", "price_count"):
            print(f"{k}: {diag.get(k)}")
        print("---- first 1500 chars of visible page text ----")
        print(diag.get("body_sample", ""))
        print("=== DIAGNOSTIC END ===")
        return

    current_items = {p["name"]: p for p in products}
    current = {name: p["price"] for name, p in current_items.items()}
    previous = load_snapshot()

    if not previous:
        print("First run: recording a baseline snapshot.")
        save_snapshot(current)
        return

    new_items = [current_items[n] for n in current if n not in previous]

    # Safety: if most of the page looks 'new' (e.g. after a script update or a
    # site redesign changed how items are identified), don't blast alerts -
    # just quietly re-baseline this once.
    if len(new_items) > 15 and len(new_items) >= 0.5 * max(1, len(current)):
        print(f"{len(new_items)} of {len(current)} look new; treating as a re-baseline (no alerts).")
        save_snapshot(current)
        return

    new_items.sort(key=_by_discount)

    price_changes = []
    for name in current:
        if name in previous and current[name] != previous[name]:
            old, new = previous[name], current[name]
            if is_real_price(old) and is_real_price(new):
                price_changes.append((current_items[name], old, new))
    price_changes.sort(key=lambda t: _by_discount(t[0]))

    report = []
    if new_items:
        print(f"\n*** {len(new_items)} NEW clearance item(s): ***")
        report.append(f"NEW clearance ({len(new_items)}):")
        for item in new_items:
            print("  NEW: " + _fmt(item))
            report.append("- " + _fmt(item))

    if price_changes:
        print(f"\n*** {len(price_changes)} PRICE CHANGE(S): ***")
        report.append(f"\nPrice changes ({len(price_changes)}):")
        for item, old, new in price_changes:
            arrow = "v" if _to_float(new) < _to_float(old) else "^"
            pct = f", now {item['pct_off']}% off" if item.get("pct_off") is not None else ""
            print(f"  {item['name']}: {old} -> {new} ({arrow}{pct})")
            link = f"\n  {item['url']}" if item.get("url") else ""
            report.append(f"- {item['name']}: {old} -> {new}{link}")

    if not new_items and not price_changes:
        print("No new items and no price changes since last check.")
    else:
        notify("\n".join(report))

    save_snapshot(current)
    print(f"\nSaved snapshot ({len(current)} items) for next time.")


if __name__ == "__main__":
    main()

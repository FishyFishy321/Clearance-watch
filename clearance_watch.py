#!/usr/bin/env python3
"""
clearance_watch.py  (Playwright / real-browser version)
--------------------------------------------------------
Loads the Sportsman's Warehouse clearance page in a headless browser so the
page's JavaScript runs and the products actually appear, then reports what
CHANGED since the last run (new items + price changes), sorted by percent off.

Alerts go to Telegram when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set.

If it finds 0 items, it saves the fully-rendered page to 'rendered_page.html'
so the exact layout can be inspected and the parser tuned to match.
"""

import json
import os
import re
import sys

try:
    import requests  # used only for the Telegram alert
except ImportError:
    requests = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Playwright isn't installed. The GitHub workflow installs it automatically.")
    print("Locally you'd run:  pip install playwright && python -m playwright install chromium")
    sys.exit(1)

# ----------------------------------------------------------------------------
# CONFIG  -- edit these
# ----------------------------------------------------------------------------

CLEARANCE_URL = "https://www.sportsmans.com/deals-clearance/fishing-clearance/c/cat101209?q=%3Aprice-desc%3AdefaultParentCategory%3Acat101045%3AdefaultParentCategory%3Acat101039%3AdefaultParentCategory%3Acat101028%3AdefaultParentCategory%3Acat101036%3AdefaultParentCategory%3Acat101038%3AdefaultParentCategory%3Acat112005%3AdefaultParentCategory%3Acat112000%3AdefaultParentCategory%3Acat135701%3AdefaultParentCategory%3Acat135700%3AdefaultParentCategory%3Acat101051%3AdefaultParentCategory%3Acat101037%3AdefaultParentCategory%3Acat101052%3AdefaultParentCategory%3Acat101041%3AdefaultParentCategory%3Acat101034%3AdefaultParentCategory%3Acat101035%3AshipOption%3ASHIPTOYOU"

KEYWORDS = ["rod", "reel", "combo"]

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

# ----------------------------------------------------------------------------
# Browser render + extraction
# ----------------------------------------------------------------------------

def render_and_extract():
    """
    Opens the page in a headless browser, waits for products to render, and
    pulls name/price/link from product tiles using a layout-agnostic method:
    it reads each link's visible text and grabs any $ prices inside it.
    Returns (products_list, rendered_html).
    """
    products = {}
    html = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT, viewport={"width": 1280, "height": 2000})
        page.goto(CLEARANCE_URL, wait_until="domcontentloaded", timeout=60000)

        # Give the JavaScript time to load products; wait until a price shows up.
        try:
            page.wait_for_function("document.body.innerText.includes('$')", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(4000)

        # Scroll down to trigger any lazy-loaded tiles.
        for _ in range(5):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(800)

        html = page.content()

        # Diagnostics so we can see what the browser actually got.
        try:
            body_text = page.inner_text("body")
        except Exception:
            body_text = ""
        try:
            title = page.title()
        except Exception:
            title = ""
        diag = {
            "title": title,
            "body_len": len(body_text),
            "body_sample": body_text[:1500],
            "anchor_count": len(page.query_selector_all("a")),
            "price_count": len(re.findall(r"\$[\d,]+\.\d{2}", html)),
            "html_len": len(html),
        }

        # Layout-agnostic: examine every link's visible text.
        for a in page.query_selector_all("a"):
            try:
                text = (a.inner_text() or "").strip()
                href = a.get_attribute("href") or ""
            except Exception:
                continue
            if not text:
                continue
            low = text.lower()
            if not any(k in low for k in [k.lower() for k in KEYWORDS]):
                continue
            prices = re.findall(r"\$[\d,]+\.\d{2}", text)
            if not prices:
                continue

            lines = [l.strip() for l in text.splitlines() if l.strip()]
            name_lines = [l for l in lines if "$" not in l]
            name = (name_lines[0] if name_lines else lines[0])[:120]

            uniq = sorted(set(prices), key=_to_float)
            sale = uniq[0]
            orig = uniq[-1] if len(uniq) > 1 else None

            if href.startswith("http"):
                url = href
            elif href.startswith("/"):
                url = "https://www.sportsmans.com" + href
            else:
                url = None

            products[name] = {"name": name, "price": sale, "orig": orig, "url": url}

        browser.close()

    result = list(products.values())
    for p in result:
        p["pct_off"] = _pct_off(p.get("orig"), p.get("price"))
    return result, html, diag

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
    print("Loading clearance page in a headless browser...")
    try:
        products, html, diag = render_and_extract()
    except Exception as e:
        print(f"Browser/render failed: {e}")
        return

    print(f"Found {len(products)} matching rod/reel items on the page.")

    if not products:
        try:
            with open(RENDERED_DUMP, "w") as f:
                f.write(html)
        except Exception:
            pass
        print("\n=== DIAGNOSTIC START ===")
        print(f"page title      : {diag['title']}")
        print(f"rendered HTML   : {diag['html_len']} chars")
        print(f"visible text    : {diag['body_len']} chars")
        print(f"links on page   : {diag['anchor_count']}")
        print(f"prices ($) seen : {diag['price_count']}")
        print("---- first 1500 chars of visible page text ----")
        print(diag['body_sample'])
        print("=== DIAGNOSTIC END ===")
        return

    # Clean up any stale debug dump once extraction works.
    if os.path.exists(RENDERED_DUMP):
        try:
            os.remove(RENDERED_DUMP)
        except Exception:
            pass

    current_items = {p["name"]: p for p in products}
    current = {name: p["price"] for name, p in current_items.items()}
    previous = load_snapshot()

    if not previous:
        print("First run: recording a baseline snapshot.")
        save_snapshot(current)
        return

    new_items = [current_items[n] for n in current if n not in previous]
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
    print("\nSaved snapshot for next time.")


if __name__ == "__main__":
    main()

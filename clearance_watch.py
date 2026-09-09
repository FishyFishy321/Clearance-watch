Content is user-generated and unverified.
1
Learn about artifacts
#!/usr/bin/env python3
"""
clearance_watch.py
------------------
Checks Sportsman's Warehouse for fishing RODS / REELS on clearance and reports
what CHANGED since the last run: NEW items and PRICE CHANGES, sorted by percent
off retail (deepest discount first).

Sends alerts to Telegram when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set
(that's how the GitHub Actions cloud version notifies you). If they're not set,
it just prints to the terminal (a-Shell) or fires a Pythonista notification.
"""

import json
import os
import re
import sys
import html as html_lib

try:
    import requests
except ImportError:
    print("The 'requests' library is missing. In a-Shell run:  pip install requests")
    sys.exit(1)

# ----------------------------------------------------------------------------
# CONFIG  -- edit these
# ----------------------------------------------------------------------------

CLEARANCE_URL = "https://www.sportsmans.com/deals-clearance/fishing-clearance/c/cat101209?q=%3Aprice-desc%3AdefaultParentCategory%3Acat101045%3AdefaultParentCategory%3Acat101039%3AdefaultParentCategory%3Acat101028%3AdefaultParentCategory%3Acat101036%3AdefaultParentCategory%3Acat101038%3AdefaultParentCategory%3Acat112005%3AdefaultParentCategory%3Acat112000%3AdefaultParentCategory%3Acat135701%3AdefaultParentCategory%3Acat135700%3AdefaultParentCategory%3Acat101051%3AdefaultParentCategory%3Acat101037%3AdefaultParentCategory%3Acat101052%3AdefaultParentCategory%3Acat101041%3AdefaultParentCategory%3Acat101034%3AdefaultParentCategory%3Acat101035%3AshipOption%3ASHIPTOYOU"

KEYWORDS = ["rod", "reel", "combo"]

STORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_snapshot.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ----------------------------------------------------------------------------
# Price / link helpers
# ----------------------------------------------------------------------------

def _to_float(price):
    try:
        return float(str(price).replace("$", "").replace(",", ""))
    except Exception:
        return 0.0


def _find_price(blob, keys):
    for k in keys:
        m = re.search(r'"' + k + r'"\s*:\s*"?\$?([\d,]+\.\d{2})', blob)
        if m:
            return "$" + m.group(1)
    return None


def _find_url(blob):
    m = re.search(r'"(?:url|link|productUrl|canonicalUrl|@id)"\s*:\s*"(https?:\\?/\\?/[^"]+)"', blob)
    if m:
        return m.group(1).replace("\\/", "/")
    return None


def _pct_off(orig, sale):
    o, s = _to_float(orig), _to_float(sale)
    if o and s and o > s:
        return round((o - s) / o * 100)
    return None


def is_real_price(p):
    return isinstance(p, str) and p.startswith("$")

# ----------------------------------------------------------------------------
# Core logic
# ----------------------------------------------------------------------------

def fetch(url):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def extract_products(page):
    """Returns list of dicts: {name, price, orig, pct_off, url}."""
    products = {}

    for match in re.finditer(r'\{[^{}]*?"name"\s*:\s*"([^"]{3,120})"[^{}]*?\}', page):
        blob = match.group(0)
        name = html_lib.unescape(match.group(1)).strip()
        sale = _find_price(blob, ["salePrice", "currentPrice", "price"])
        orig = _find_price(blob, ["listPrice", "regularPrice", "wasPrice",
                                  "originalPrice", "msrp", "retailPrice"])
        products[name] = {
            "name": name,
            "price": sale or "price n/a",
            "orig": orig,
            "url": _find_url(blob),
        }

    for match in re.finditer(r'"@type"\s*:\s*"Product".*?"name"\s*:\s*"([^"]{3,120})"', page, re.S):
        name = html_lib.unescape(match.group(1)).strip()
        products.setdefault(name, {"name": name, "price": "price n/a", "orig": None, "url": None})

    kw = [k.lower() for k in KEYWORDS]
    result = [p for p in products.values() if any(k in p["name"].lower() for k in kw)]
    for p in result:
        p["pct_off"] = _pct_off(p.get("orig"), p.get("price"))
    return result


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
    """Send a message via Telegram bot. Returns True on success."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
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
        return
    try:
        import notification  # Pythonista only
        notification.schedule(text, delay=1)
    except Exception:
        pass  # a-Shell: the printed output above is the notification


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


def main():
    diagnose = "--diagnose" in sys.argv

    print("Checking Sportsman's Warehouse clearance...")
    try:
        page = fetch(CLEARANCE_URL)
    except Exception as e:
        print(f"Could not reach the site: {e}")
        return

    if diagnose:
        with open("debug_page.html", "w") as f:
            f.write(page)
        print(f"Saved raw page to debug_page.html ({len(page):,} chars).")
        print("Check for product names, original/was prices, and product URLs.")
        return

    products = extract_products(page)
    print(f"Found {len(products)} matching rod/reel items on the page.")
    if not products:
        print("Found 0 - page may be JavaScript-rendered. Try: --diagnose")
        return

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

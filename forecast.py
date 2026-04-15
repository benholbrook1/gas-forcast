"""
Gas Forecast & Daily Briefing
Scrapes today's gas price + change indicator from gaswizard.ca
and sends a concise SMS via the Freedom Mobile email-to-SMS gateway.
"""

import os
import re
import smtplib
import requests
from bs4 import BeautifulSoup
from email.mime.text import MIMEText
from datetime import datetime

# ── Configuration ─────────────────────────────────────────────────────────────
# Each entry: (phone_number, city_slug)
# City slugs match gaswizard.ca URL paths — see full list at:
# https://gaswizard.ca/gas-price-predictions/
#
# Ontario examples:
#   toronto, gta, ottawa, hamilton, london, kitchener, waterloo,
#   windsor, barrie, kingston, mississauga, brampton, markham,
#   oakville, oshawa, niagara, sudbury, thunder-bay, peterborough
RECIPIENTS = [
    (os.environ.get("TARGET_PHONE_NUMBER", ""), os.environ.get("TARGET_CITY", "toronto")),
    # Add more recipients here, e.g.:
    # ("5551234567", "waterloo"),
    # ("5559876543", "ottawa"),
]

GMAIL_USER     = os.environ.get("GMAIL_USER", "")
GMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
SMS_GATEWAY    = "txt.freedommobile.ca"
DEBUG          = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")

BASE_URL = "https://gaswizard.ca/gas-prices/{city}/"
HEADERS  = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


# ── Scraping ───────────────────────────────────────────────────────────────────
def fetch_gas_forecast(city: str) -> dict:
    slug = city.lower().replace(" ", "-")
    url  = BASE_URL.format(city=slug)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Network error fetching {url}: {e}") from e

    soup = BeautifulSoup(resp.text, "html.parser")

    if DEBUG:
        # Dump first 3000 chars of visible text to help diagnose layout changes
        preview = soup.get_text(" ", strip=True)[:3000]
        print(f"[DEBUG] Page text preview:\n{preview}\n")

    price, change, trend = _parse_price_block(soup, resp.text)

    h1 = soup.find("h1")
    display_name = city.title()
    if h1:
        text = h1.get_text(strip=True)
        m = re.search(r"for (.+)$", text, re.IGNORECASE)
        if m:
            display_name = m.group(1).strip()

    return {"city": display_name, "price": price, "change": change, "trend": trend}


def _parse_price_block(soup: BeautifulSoup, raw_html: str):
    """
    Try multiple strategies to extract the Regular gas price and change.
    Returns (price_float, change_str, trend_str).
    """

    # Strategy 1 — original pattern: "174.9 (n/c)" or "168.9 (+3.0)"
    PRICE_RE = re.compile(
        r"(\d{2,3}(?:\.\d)?)\s*\((n/c|[+-]\d+(?:\.\d)?)\)",
        re.IGNORECASE,
    )

    # Search within <li> tags containing "Regular" first
    for li in soup.find_all("li"):
        text = li.get_text(" ", strip=True)
        if "regular" in text.lower():
            m = PRICE_RE.search(text)
            if m:
                return float(m.group(1)), m.group(2), _trend(m.group(2))

    # Strategy 2 — scan all visible text (catches tables, divs, spans)
    full_text = soup.get_text(" ", strip=True)
    m = PRICE_RE.search(full_text)
    if m:
        return float(m.group(1)), m.group(2), _trend(m.group(2))

    # Strategy 3 — price only, no change indicator (e.g. "Regular 174.9")
    PRICE_ONLY_RE = re.compile(
        r"[Rr]egular[^\d]{0,20}(\d{2,3}(?:\.\d)?)",
    )
    m = PRICE_ONLY_RE.search(full_text)
    if m:
        price = float(m.group(1))
        return price, "n/c", "→"

    # Strategy 4 — scan raw HTML (catches JSON-in-script blocks, data attributes)
    m = PRICE_RE.search(raw_html)
    if m:
        return float(m.group(1)), m.group(2), _trend(m.group(2))

    # Strategy 5 — any standalone 3-digit price in the 100–199 range
    STANDALONE_RE = re.compile(r"\b(1\d{2}(?:\.\d)?)\b")
    candidates = STANDALONE_RE.findall(full_text)
    if candidates:
        price = float(candidates[0])
        return price, "n/c", "→"

    raise RuntimeError(
        "Could not parse a gas price from gaswizard.ca — "
        "the page layout may have changed, or this city has no data. "
        "Set DEBUG=true in your env to see raw page content."
    )


def _trend(change_str: str) -> str:
    s = change_str.strip().lower()
    if s in ("n/c", "0"):
        return "→"
    return "▲" if s.startswith("+") else "▼" if s.startswith("-") else "→"


# ── Message Formatting ─────────────────────────────────────────────────────────
def build_sms(data: dict) -> str:
    price_str  = f"{data['price']:.1f}¢" if data["price"] is not None else "N/A"
    change_str = data["change"] or ""
    date_str   = datetime.now().strftime("%b %-d")
    msg = f"⛽ {data['city']} | {price_str} ({change_str}) {data['trend']} | {date_str}"
    return msg[:137] + "..." if len(msg) > 140 else msg


# ── SMS Sending ────────────────────────────────────────────────────────────────
def send_sms(phone: str, message: str) -> None:
    if not phone:
        raise ValueError("TARGET_PHONE_NUMBER is not set.")
    if not GMAIL_USER or not GMAIL_PASSWORD:
        raise ValueError("GMAIL_USER or GMAIL_APP_PASSWORD is not set.")

    to_address = f"{phone}@{SMS_GATEWAY}"
    mime = MIMEText(message)
    mime["From"]    = GMAIL_USER
    mime["To"]      = to_address
    mime["Subject"] = ""

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_USER, to_address, mime.as_string())

    print(f"✓ SMS sent to {to_address}: {message}")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    errors = []
    for phone, city in RECIPIENTS:
        try:
            print(f"→ Fetching forecast for {city}...")
            data    = fetch_gas_forecast(city)
            message = build_sms(data)
            print(f"  Message ({len(message)} chars): {message}")
            send_sms(phone, message)
        except Exception as e:
            err = f"[ERROR] {city} / {phone}: {e}"
            print(err)
            errors.append(err)

    if errors:
        print("\nCompleted with errors:")
        for e in errors: print(" ", e)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
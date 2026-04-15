"""
Gas Forecast & Daily Briefing
Scrapes today's gas price + change indicator from gaswizard.ca
and sends a concise SMS via the Freedom Mobile email-to-SMS gateway.

Source: https://gaswizard.ca/gas-prices/{city}/
Covers all major Ontario (and Canadian) cities — no API key required.
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

BASE_URL = "https://gaswizard.ca/gas-prices/{city}/"
HEADERS  = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


# ── Scraping ──────────────────────────────────────────────────────────────────
def fetch_gas_forecast(city: str) -> dict:
    """
    Scrape today's regular gas price and change from gaswizard.ca.

    Returns a dict with keys:
        city      (str)         — display name
        price     (float|None)  — price in cents/litre, e.g. 174.9
        change    (str)         — raw change string e.g. "+3.0", "-2.0", "n/c"
        trend     (str)         — "▲", "▼", or "→"
    Raises RuntimeError on network or parse failure.
    """
    slug = city.lower().replace(" ", "-")
    url  = BASE_URL.format(city=slug)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Network error fetching {url}: {e}") from e

    soup = BeautifulSoup(resp.text, "html.parser")

    price, change, trend = _parse_price_block(soup)

    # Derive a clean display name from the page <h1>
    h1 = soup.find("h1")
    display_name = city.title()
    if h1:
        text = h1.get_text(strip=True)
        # "Tomorrow's Gas Price for Toronto" → "Toronto"
        m = re.search(r"for (.+)$", text, re.IGNORECASE)
        if m:
            display_name = m.group(1).strip()

    return {
        "city":   display_name,
        "price":  price,
        "change": change,
        "trend":  trend,
    }


def _parse_price_block(soup: BeautifulSoup):
    """
    Find the regular gas price and change indicator for today.
    Returns (price_float_or_None, change_str, trend_str).
    """
    # Pattern: 2-3 digit price, optional decimal, then parenthesised change
    # e.g. "174.9 (n/c)"  "168.9 (+3.0)"  "171.9 (-2.0)"
    PRICE_RE = re.compile(
        r"(\d{2,3}(?:\.\d)?)\s*\((n/c|[+-]\d+(?:\.\d)?)\)",
        re.IGNORECASE,
    )

    # Grab all list items; the first one that contains "Regular" is today's block
    for li in soup.find_all("li"):
        text = li.get_text(" ", strip=True)
        if "Regular" not in text:
            continue
        m = PRICE_RE.search(text)
        if m:
            price_str  = m.group(1)
            change_str = m.group(2)
            price      = float(price_str)
            trend      = _trend(change_str)
            return price, change_str, trend

    # Fallback: scan the whole page for the pattern
    full_text = soup.get_text(" ")
    m = PRICE_RE.search(full_text)
    if m:
        price_str  = m.group(1)
        change_str = m.group(2)
        return float(price_str), change_str, _trend(change_str)

    raise RuntimeError(
        "Could not parse a gas price from gaswizard.ca. "
        "The page layout may have changed."
    )


def _trend(change_str: str) -> str:
    s = change_str.strip().lower()
    if s == "n/c" or s == "0":
        return "→"
    if s.startswith("+"):
        return "▲"
    if s.startswith("-"):
        return "▼"
    return "→"


# ── Message Formatting ────────────────────────────────────────────────────────
def build_sms(data: dict) -> str:
    """
    Build a message under 140 characters.
    Example outputs:
      ⛽ Toronto | 174.9¢ (n/c) → | Apr 14
      ⛽ Waterloo | 171.9¢ (-3.0) ▼ | Apr 14
    """
    price_str  = f"{data['price']:.1f}¢" if data["price"] is not None else "N/A"
    change_str = data["change"] if data["change"] else ""
    date_str   = datetime.now().strftime("%b %-d")

    msg = (
        f"⛽ {data['city']} | "
        f"{price_str} ({change_str}) {data['trend']} | "
        f"{date_str}"
    )

    if len(msg) > 140:
        msg = msg[:137] + "..."

    return msg


# ── SMS Sending ───────────────────────────────────────────────────────────────
def send_sms(phone: str, message: str) -> None:
    """Send message via Freedom Mobile email-to-SMS gateway using Gmail SMTP."""
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


# ── Main ──────────────────────────────────────────────────────────────────────
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
        for e in errors:
            print(" ", e)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
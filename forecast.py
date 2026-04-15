"""
Gas Forecast & Daily Briefing
Scrapes tomorrow's gas price forecast for a configured Ontario city
and sends a concise SMS via Freedom Mobile email-to-SMS gateway.
"""

import os
import smtplib
import requests
from bs4 import BeautifulSoup
from email.mime.text import MIMEText
from datetime import datetime

# ── Configuration ────────────────────────────────────────────────────────────
# Each entry: (phone_number, city_slug)
# City slugs come from GasBuddy URLs, e.g. "Toronto" → "toronto"
# Full list: https://www.gasbuddy.com/gasprices/ontario
RECIPIENTS = [
    (os.environ.get("TARGET_PHONE_NUMBER", ""), os.environ.get("TARGET_CITY", "toronto")),
    # Add more recipients here, e.g.:
    # ("5551234567", "ottawa"),
    # ("5559876543", "hamilton"),
]

GMAIL_USER     = os.environ.get("GMAIL_USER", "")
GMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
SMS_GATEWAY    = "txt.freedommobile.ca"

GASBUDDY_URL   = "https://www.gasbuddy.com/gasprices/ontario/{city}"
HEADERS        = {"User-Agent": "Mozilla/5.0 (compatible; GasForecastBot/1.0)"}


# ── Scraping ─────────────────────────────────────────────────────────────────
def fetch_gas_forecast(city: str) -> dict:
    """
    Scrape today's average and tomorrow's forecast price from GasBuddy.
    Returns a dict with keys: city, today, tomorrow, trend
    Raises RuntimeError if data cannot be found.
    """
    url = GASBUDDY_URL.format(city=city.lower().replace(" ", "-"))
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Network error fetching {url}: {e}") from e

    soup = BeautifulSoup(resp.text, "html.parser")

    # GasBuddy displays prices in elements with data-testid attributes.
    # We look for the primary price display block; fall back gracefully.
    today_price    = _extract_price(soup, "today")
    tomorrow_price = _extract_price(soup, "tomorrow")

    if not today_price and not tomorrow_price:
        raise RuntimeError(
            f"Could not parse gas prices for '{city}'. "
            "GasBuddy may have changed its page layout."
        )

    # Determine trend arrow
    trend = ""
    if today_price and tomorrow_price:
        if tomorrow_price > today_price:
            trend = "▲"
        elif tomorrow_price < today_price:
            trend = "▼"
        else:
            trend = "→"

    return {
        "city":     city.title(),
        "today":    today_price,
        "tomorrow": tomorrow_price,
        "trend":    trend,
    }


def _extract_price(soup: BeautifulSoup, label: str) -> float | None:
    """
    Attempt multiple CSS selector strategies to pull a price value.
    Returns float (cents/litre) or None.
    """
    strategies = [
        # Strategy 1: data-testid attributes (GasBuddy 2024+ layout)
        lambda s: s.find(attrs={"data-testid": lambda v: v and label in v.lower()}),
        # Strategy 2: look for a <span> near text containing "today"/"tomorrow"
        lambda s: _find_near_text(s, label),
    ]

    for strategy in strategies:
        try:
            el = strategy(soup)
            if el:
                text = el.get_text(strip=True).replace("¢", "").replace("$", "").strip()
                # GasBuddy shows prices like "163.9" (cents/L) or "1.639"
                price = float(text.split()[0])
                # Normalise: if < 5, it's in dollars — convert to cents
                if price < 5:
                    price = round(price * 100, 1)
                return price
        except (ValueError, AttributeError, TypeError):
            continue

    return None


def _find_near_text(soup: BeautifulSoup, keyword: str):
    """Find a price element that lives near a heading/label containing keyword."""
    for tag in soup.find_all(string=lambda t: t and keyword.lower() in t.lower()):
        parent = tag.parent
        for _ in range(4):          # walk up max 4 levels
            if parent is None:
                break
            candidate = parent.find(class_=lambda c: c and "price" in c.lower())
            if candidate:
                return candidate
            parent = parent.parent
    return None


# ── Message Formatting ────────────────────────────────────────────────────────
def build_sms(data: dict) -> str:
    """
    Build a message under 140 characters.
    Example: "⛽ Toronto | Today: 162.9¢ | Tmrw: 159.4¢ ▼ | 2025-07-14"
    """
    today_str    = f"{data['today']:.1f}¢"    if data["today"]    else "N/A"
    tomorrow_str = f"{data['tomorrow']:.1f}¢" if data["tomorrow"] else "N/A"
    date_str     = datetime.now().strftime("%Y-%m-%d")

    msg = (
        f"⛽ {data['city']} | "
        f"Today: {today_str} | "
        f"Tmrw: {tomorrow_str} {data['trend']} | "
        f"{date_str}"
    )

    # Safety truncation — SMS hard limit
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
    mime["Subject"] = ""          # Subject counts toward character limit on some gateways

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
            # Log but don't crash — other recipients should still get their SMS
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
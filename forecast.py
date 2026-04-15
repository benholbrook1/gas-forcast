"""
Gas Forecast & Daily Briefing
Scrapes today's gas price + change indicator from gaswizard.ca
and sends a message via Discord webhook.
"""

import os
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime

# ── Configuration ──────────────────────────────────────────────────────────────
CITIES = [
    os.environ.get("TARGET_CITY", "waterloo"),
    # Add more cities here, e.g.: "ottawa", "toronto"
]

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DEBUG               = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")

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
    PRICE_RE = re.compile(
        r"(\d{2,3}(?:\.\d)?)\s*\((n/c|[+-]\d+(?:\.\d)?)\)",
        re.IGNORECASE,
    )

    for li in soup.find_all("li"):
        text = li.get_text(" ", strip=True)
        if "regular" in text.lower():
            m = PRICE_RE.search(text)
            if m:
                return float(m.group(1)), m.group(2), _trend(m.group(2))

    full_text = soup.get_text(" ", strip=True)
    m = PRICE_RE.search(full_text)
    if m:
        return float(m.group(1)), m.group(2), _trend(m.group(2))

    PRICE_ONLY_RE = re.compile(r"[Rr]egular[^\d]{0,20}(\d{2,3}(?:\.\d)?)")
    m = PRICE_ONLY_RE.search(full_text)
    if m:
        return float(m.group(1)), "n/c", "→"

    m = PRICE_RE.search(raw_html)
    if m:
        return float(m.group(1)), m.group(2), _trend(m.group(2))

    STANDALONE_RE = re.compile(r"\b(1\d{2}(?:\.\d)?)\b")
    candidates = STANDALONE_RE.findall(full_text)
    if candidates:
        return float(candidates[0]), "n/c", "→"

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
def build_message(data: dict) -> str:
    price_str  = f"{data['price']:.1f}¢" if data["price"] is not None else "N/A"
    change_str = data["change"] or ""
    date_str   = datetime.now().strftime("%b %-d")
    return f"⛽ **{data['city']}** | {price_str} ({change_str}) {data['trend']} | {date_str}"


# ── Discord Sending ────────────────────────────────────────────────────────────
def send_discord(message: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise ValueError("DISCORD_WEBHOOK_URL is not set.")

    resp = requests.post(
        DISCORD_WEBHOOK_URL,
        json={"content": message},
        timeout=10,
    )

    if resp.status_code not in (200, 204):
        raise RuntimeError(
            f"Discord webhook failed: {resp.status_code} {resp.text}"
        )

    print(f"✓ Discord message sent: {message}")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    errors  = []
    lines   = []

    for city in CITIES:
        try:
            print(f"→ Fetching forecast for {city}...")
            data = fetch_gas_forecast(city)
            lines.append(build_message(data))
        except Exception as e:
            err = f"[ERROR] {city}: {e}"
            print(err)
            errors.append(err)

    if lines:
        try:
            send_discord("\n".join(lines))
        except Exception as e:
            errors.append(f"[ERROR] Discord send: {e}")

    if errors:
        print("\nCompleted with errors:")
        for e in errors:
            print(" ", e)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
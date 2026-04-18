"""
Gas forecast SMS briefings.

Uses Gemini with Google Search grounding to research near-term conditions and
produce a structured tomorrow prediction, then sends the result via Twilio SMS.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
import re

from dotenv import load_dotenv
from google import genai
from google.genai import types
from twilio.rest import Client as TwilioClient


@dataclass(frozen=True)
class Forecast:
    city: str
    tomorrow_expected_price_cents_per_liter: float
    influences: list[str]
    notable_information: list[str]
    recommendation: str


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_required(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise ValueError(f"{name} is required but not set.")
    return v


def _parse_cities() -> list[str]:
    raw = (os.environ.get("TARGET_CITIES") or os.environ.get("TARGET_CITY") or "waterloo").strip()
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def _tomorrow_local_date_str(tz_name: str) -> str:
    # If zoneinfo isn't available/valid, fall back to UTC.
    try:
        from zoneinfo import ZoneInfo  # py3.9+

        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    now = datetime.now(tz)
    return (now + timedelta(days=1)).strftime("%Y-%m-%d")


def _safe_json_loads(text: str) -> Any:
    """
    Extract the first JSON object from text and parse it.

    With tool use (Google Search grounding), Gemini cannot always return a strict
    JSON mime type response; this keeps us resilient while still enforcing a
    structured shape.
    """
    t = (text or "").strip()

    # Strip common fenced code blocks.
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
        t = t.strip()

    # If it's already JSON, parse directly.
    if t.startswith("{") and t.endswith("}"):
        return json.loads(t)

    # Otherwise, extract the first {...} block.
    start = t.find("{")
    if start == -1:
        raise ValueError("No JSON object found in Gemini response.")

    depth = 0
    for i in range(start, len(t)):
        ch = t[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = t[start : i + 1]
                return json.loads(candidate)

    raise ValueError("Unterminated JSON object in Gemini response.")


def _get_grounding_metadata(resp: types.GenerateContentResponse) -> Any | None:
    """
    Grounding metadata is attached to each Candidate, not the top-level response.
    (GenerateContentResponse has no grounding_metadata field in the Python SDK.)
    """
    md = getattr(resp, "grounding_metadata", None)
    if md is not None:
        return md
    for cand in getattr(resp, "candidates", None) or []:
        gmd = getattr(cand, "grounding_metadata", None)
        if gmd is not None:
            return gmd
    return None


def _extract_grounding_sources(resp: types.GenerateContentResponse) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    md = _get_grounding_metadata(resp)
    if not md:
        return sources

    chunks = getattr(md, "grounding_chunks", None) or []
    for ch in chunks:
        web = getattr(ch, "web", None)
        if web is None and isinstance(ch, dict):
            web = ch.get("web")
        if not web:
            continue
        uri = getattr(web, "uri", None)
        if uri is None and isinstance(web, dict):
            uri = web.get("uri")
        title = getattr(web, "title", None)
        if title is None and isinstance(web, dict):
            title = web.get("title")
        if uri:
            sources.append({"title": str(title or ""), "uri": str(uri)})
    return sources


def _extract_grounding_web_search_queries(resp: types.GenerateContentResponse) -> list[str]:
    md = _get_grounding_metadata(resp)
    if not md:
        return []
    q = getattr(md, "web_search_queries", None) or []
    return [str(x) for x in q if x is not None]


def generate_forecast_with_gemini(
    *, city: str, region: str, model: str, api_key: str, tz_name: str
) -> tuple[Forecast, list[dict[str, str]], list[str]]:
    client = genai.Client(api_key=api_key)

    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    config = types.GenerateContentConfig(
        tools=[grounding_tool],
        temperature=0.4,
    )

    target_date = _tomorrow_local_date_str(tz_name)
    prompt = f"""
You are generating a concise gas price briefing for SMS.

Location: {city}, {region}
Target date (tomorrow): {target_date}

Task:
- Use web search to find the most relevant and recent information that could influence regular gasoline prices in this location between now and tomorrow.
- Produce a best-estimate prediction for tomorrow's expected pump price in cents per liter (e.g. 152.9).
- Provide a short, practical recommendation. Do not include anything about checking local gas price apps. Justify your recommendation.

Output requirements (STRICT JSON ONLY):
{{
  "city": string,
  "tomorrow_expected_price_cents_per_liter": number,   // numeric pump-style price (e.g. 152.9)
  "influences": [string, ...],                 // 2-5 bullets, each < 110 chars
  "notable_information": [string, ...],        // 1-3 bullets, each < 140 chars
  "recommendation": string                     // <= 200 chars, actionable
}}

Guidance:
- If sources disagree, choose the most credible and recent; reflect uncertainty in wording of influences/notables.
- Keep it grounded in facts from sources you find; do not invent specific events.
""".strip()

    resp = client.models.generate_content(model=model, contents=prompt, config=config)
    data = _safe_json_loads(resp.text or "")

    # Backward/robustness: if the model returns dollars/L by mistake, convert.
    cents = data.get("tomorrow_expected_price_cents_per_liter", None)
    if cents is None and "tomorrow_expected_price_per_liter" in data:
        cents = float(data["tomorrow_expected_price_per_liter"]) * 100.0
    if cents is None:
        raise ValueError("Gemini response missing tomorrow_expected_price_cents_per_liter")

    forecast = Forecast(
        city=str(data.get("city") or city),
        tomorrow_expected_price_cents_per_liter=float(cents),
        influences=[str(x) for x in (data.get("influences") or [])][:5],
        notable_information=[str(x) for x in (data.get("notable_information") or [])][:3],
        recommendation=str(data.get("recommendation") or "").strip(),
    )
    sources = _extract_grounding_sources(resp)
    return forecast, sources, _extract_grounding_web_search_queries(resp)


def format_sms(forecast: Forecast) -> str:
    raise RuntimeError("Use format_sms_with_template()")


def _squeeze(s: str) -> str:
    return " ".join((s or "").strip().split())


def _truncate(s: str, max_len: int) -> str:
    s = _squeeze(s)
    if len(s) <= max_len:
        return s
    if max_len <= 1:
        return s[:max_len]
    return s[: max_len - 1].rstrip() + "…"


def format_sms_with_template(
    forecast: Forecast,
    *,
    template: str,
    max_len: int,
) -> str:
    """
    Keep messages short when needed (e.g., carrier segmentation limits).

    Supported templates:
    - "detailed": multi-line detailed briefing (the full template you liked)
    - "compact": tomorrow price (¢/L) then recommendation

    Backward compatible aliases:
    - "recommendation_only" -> "compact"
    """
    template = (template or "compact").strip().lower()
    if template == "recommendation_only":
        template = "compact"
    if template == "full":
        template = "detailed"

    if template == "detailed":
        lines: list[str] = []
        lines.append("Gas Forecast")
        lines.append("")
        lines.append(f"Tomorrow's Expected Price: {forecast.tomorrow_expected_price_cents_per_liter:.1f}¢/L")
        lines.append("")
        lines.append("Current Influences:")
        for item in (forecast.influences or [])[:5]:
            item = _squeeze(item)
            if item:
                lines.append(f"- {item}")
        if forecast.notable_information:
            lines.append("")
            lines.append("Notable Information:")
            for item in (forecast.notable_information or [])[:3]:
                item = _squeeze(item)
                if item:
                    lines.append(f"- {item}")
        lines.append("")
        lines.append(f"Recommendation: {_squeeze(forecast.recommendation)}".strip())

        body = "\n".join(lines).strip()
        return _truncate(body, max_len) if max_len > 0 else body

    if template == "compact":
        price_line = f"Tomorrow's Expected Price: {forecast.tomorrow_expected_price_cents_per_liter:.1f}¢/L"
        rec = _squeeze(forecast.recommendation)
        sep = "\n\n"
        rec_prefix = "Recommendation: "
        body = f"{price_line}{sep}{rec_prefix}{rec}"

        if max_len <= 0 or len(body) <= max_len:
            return body

        # Keep the price line; truncate recommendation only.
        header = f"{price_line}{sep}{rec_prefix}"
        if len(header) >= max_len:
            return _truncate(body, max_len)
        room = max_len - len(header)
        rec2 = _truncate(rec, max(0, room))
        return f"{header}{rec2}"

    raise ValueError(f"Unknown SMS_TEMPLATE: {template!r}")


def send_sms_via_twilio(*, body: str) -> str:
    account_sid = _env_required("TWILIO_ACCOUNT_SID")
    auth_token = _env_required("TWILIO_AUTH_TOKEN")
    from_number = _env_required("TWILIO_FROM_NUMBER")

    raw_to = (os.environ.get("TWILIO_TO_NUMBERS") or os.environ.get("TWILIO_TO_NUMBER") or "").strip()
    if not raw_to:
        raise ValueError("TWILIO_TO_NUMBERS (or TWILIO_TO_NUMBER) is required but not set.")

    to_numbers = [x.strip() for x in raw_to.split(",") if x.strip()]
    if not to_numbers:
        raise ValueError("No destination numbers found in TWILIO_TO_NUMBERS.")

    client = TwilioClient(account_sid, auth_token)
    sids: list[str] = []
    for to in to_numbers:
        msg = client.messages.create(from_=from_number, to=to, body=body)
        sids.append(str(msg.sid))
    return ", ".join(sids)


def send_sms_via_twilio_many(*, bodies: Iterable[str]) -> list[str]:
    # Send each body as its own SMS (avoids concatenation/segment surprises).
    all_sids: list[str] = []
    for body in bodies:
        sids = send_sms_via_twilio(body=body)
        all_sids.extend([x.strip() for x in sids.split(",") if x.strip()])
    return all_sids


def log_sources(
    *,
    city: str,
    sources: Iterable[dict[str, str]],
    model: str,
    web_search_queries: list[str] | None = None,
) -> None:
    log_dir = Path(os.environ.get("LOG_DIR", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "sources.jsonl"
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "city": city,
        "model": model,
        "sources": list(sources),
        "web_search_queries": list(web_search_queries or []),
    }
    path.write_text("", encoding="utf-8") if not path.exists() else None
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> None:
    # Load variables from a local .env file if present.
    load_dotenv()

    debug = _env_bool("DEBUG", False)
    dry_run = _env_bool("DRY_RUN", False)

    api_key = _env_required("GEMINI_API_KEY")
    model = (os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash").strip()
    region = (os.environ.get("REGION") or "Ontario, Canada").strip()
    tz_name = (os.environ.get("TIMEZONE") or "America/Toronto").strip()
    sms_template = (os.environ.get("SMS_TEMPLATE") or "compact").strip()
    sms_max_len = int((os.environ.get("SMS_MAX_LEN") or "122").strip())

    cities = _parse_cities()
    errors: list[str] = []
    sms_bodies: list[str] = []

    for city in cities:
        try:
            if debug:
                print(f"→ Generating Gemini forecast for {city}...")
            forecast, sources, web_queries = generate_forecast_with_gemini(
                city=city,
                region=region,
                model=model,
                api_key=api_key,
                tz_name=tz_name,
            )
            log_sources(
                city=city,
                sources=sources,
                model=model,
                web_search_queries=web_queries,
            )
            sms_bodies.append(
                format_sms_with_template(
                    forecast,
                    template=sms_template,
                    max_len=sms_max_len,
                )
            )
        except Exception as e:
            errors.append(f"[ERROR] {city}: {e}")

    if errors:
        for e in errors:
            print(e, file=sys.stderr)

    if not sms_bodies:
        raise SystemExit(1)

    if dry_run:
        for b in sms_bodies:
            print(b)
            print()
        return

    sids = send_sms_via_twilio_many(bodies=sms_bodies)
    if debug:
        print(f"✓ Sent SMS via Twilio. Message SID(s): {', '.join(sids)}")


if __name__ == "__main__":
    main()
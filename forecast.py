"""
Gas forecast SMS briefings.

Uses Gemini with Google Search grounding to research near-term conditions and
produce a structured tomorrow prediction, then sends the result via Twilio SMS.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
import re
from collections import OrderedDict

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from twilio.rest import Client as TwilioClient


@dataclass(frozen=True)
class Forecast:
    city: str
    today_average_price_cents_per_liter: float | None
    tomorrow_expected_price_cents_per_liter: float
    influences: list[str]
    notable_information: list[str]
    recommendation: str


@dataclass(frozen=True)
class Recipient:
    """One SMS destination and the city forecast they should receive."""

    to_e164: str
    city_label: str  # human/Gemini location name (e.g. "Waterloo")
    city_key: str  # normalized key for grouping (lowercase, single spaces)
    sms_template: str  # "compact" | "detailed"


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


def _city_key(name: str) -> str:
    return " ".join(name.strip().lower().split())


_SMS_TEMPLATE_ALIASES = {"detail": "detailed", "short": "compact"}


def _is_sms_template_token(s: str) -> bool:
    t = _SMS_TEMPLATE_ALIASES.get(s.strip().lower(), s.strip().lower())
    return t in ("compact", "detailed")


def _validated_sms_template(name: str) -> str:
    t = (name or "").strip().lower()
    t = _SMS_TEMPLATE_ALIASES.get(t, t)
    if t not in ("compact", "detailed"):
        raise ValueError(f"SMS template must be 'compact' or 'detailed', not {name!r}")
    return t


def _sms_max_len_for_template(template: str) -> int:
    """Length cap for SMS body. 0 means no cap. ``compact`` uses SMS_MAX_LEN; ``detailed`` uses SMS_MAX_LEN_DETAILED (default 0 = full message, often multi-segment)."""
    t = _validated_sms_template(template)
    if t == "detailed":
        raw = (os.environ.get("SMS_MAX_LEN_DETAILED") or "0").strip()
        return int(raw or "0")
    raw = (os.environ.get("SMS_MAX_LEN") or "122").strip()
    return int(raw or "0")


def _default_phone_country_prefix() -> str:
    return (os.environ.get("DEFAULT_PHONE_COUNTRY_PREFIX") or "1").strip().lstrip("+") or "1"


def _normalize_phone_e164(raw_phone: str) -> str:
    p = re.sub(r"[\s\-\(\)]", "", (raw_phone or "").strip())
    if not p:
        raise ValueError("Empty phone number.")
    if p.startswith("+"):
        return p
    cc = _default_phone_country_prefix()
    if re.fullmatch(r"\d{10}", p):
        return f"+{cc}{p}"
    if re.fullmatch(rf"{cc}\d{{10}}", p):
        return f"+{p}"
    if re.fullmatch(r"\d{11,15}", p):
        return f"+{p}"
    raise ValueError(f"Unrecognized phone format: {raw_phone!r} (use 10 digits or +E.164)")


def _recipients_config_raw() -> str:
    path = (os.environ.get("RECIPIENTS_FILE") or "").strip()
    if path:
        p = Path(path).expanduser()
        try:
            return p.read_text(encoding="utf-8")
        except OSError as e:
            raise RuntimeError(f"Could not read RECIPIENTS_FILE {p}: {e}") from e
    return os.environ.get("RECIPIENTS") or ""


def _parse_recipients_from_env(*, default_template: str) -> list[Recipient]:
    """
    Multiline config: one row per recipient, ``phone, city`` or ``phone, city, template``.

    ``template`` is optional; when omitted, ``default_template`` (from ``SMS_TEMPLATE``) is used.
    If the line has three or more comma segments and the last segment is ``compact`` or
    ``detailed``, it is treated as the template and the middle segments form the city (so
    ``city, province`` style names can include commas without a template).

    Set ``RECIPIENTS`` (use double-quoted multiline in ``.env``) or ``RECIPIENTS_FILE``.

    Example::

        226-792-8781, waterloo, compact
        324-543-2356, burlington, detailed
        324-352-2356, waterloo
    """
    block = _recipients_config_raw().strip()
    if not block:
        return []

    default_t = _validated_sms_template(default_template)
    out: list[Recipient] = []
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "," not in line:
            raise ValueError(
                f"Invalid RECIPIENTS line (expected 'phone, city' or 'phone, city, template'): {line!r}"
            )
        parts = [p.strip() for p in line.split(",")]
        if not parts or not parts[0]:
            raise ValueError(f"Invalid RECIPIENTS line: {line!r}")

        phone_part = parts[0]
        tmpl = default_t
        if len(parts) >= 3 and _is_sms_template_token(parts[-1]):
            tmpl = _validated_sms_template(parts[-1])
            city_raw = ", ".join(parts[1:-1]).strip()
        else:
            city_raw = ", ".join(parts[1:]).strip()

        if not city_raw:
            raise ValueError(f"Missing city in RECIPIENTS line: {line!r}")

        to_e164 = _normalize_phone_e164(phone_part)
        ck = _city_key(city_raw)
        label = " ".join(city_raw.split())
        out.append(
            Recipient(
                to_e164=to_e164,
                city_label=label,
                city_key=ck,
                sms_template=tmpl,
            )
        )
    return out


def _group_recipients_by_city_and_template(
    recipients: list[Recipient],
) -> list[tuple[str, str, str, list[str]]]:
    """
    One Gemini call per distinct (city, template). Returns list of
    (city_key, city_label, template, [e164, ...]) in first-seen order.
    """
    order: list[tuple[str, str]] = []
    labels: dict[tuple[str, str], str] = {}
    phones: dict[tuple[str, str], OrderedDict[str, None]] = {}

    for r in recipients:
        key = (r.city_key, r.sms_template)
        if key not in phones:
            order.append(key)
            labels[key] = r.city_label
            phones[key] = OrderedDict()
        if r.to_e164 not in phones[key]:
            phones[key][r.to_e164] = None

    return [
        (city_key, labels[(city_key, tmpl)], tmpl, list(phones[(city_key, tmpl)].keys()))
        for city_key, tmpl in order
    ]


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


def _retryable_genai_api_error(exc: BaseException) -> bool:
    """Transient overload / capacity errors from the Gemini API."""
    if isinstance(exc, genai_errors.ServerError):
        return True
    if isinstance(exc, genai_errors.ClientError):
        code = getattr(exc, "code", None)
        status = (getattr(exc, "status", None) or "").upper()
        if code == 429:
            return True
        if status in ("UNAVAILABLE", "RESOURCE_EXHAUSTED"):
            return True
    return False


def _generate_content_with_retries(
    *,
    client: genai.Client,
    model: str,
    prompt: str,
    config: types.GenerateContentConfig,
    debug: bool,
) -> types.GenerateContentResponse:
    raw_max = (os.environ.get("GEMINI_MAX_RETRIES") or "6").strip()
    max_attempts = max(1, int(raw_max or "6"))
    base = float((os.environ.get("GEMINI_RETRY_BASE_SECONDS") or "4.0").strip() or "4.0")
    cap = float((os.environ.get("GEMINI_RETRY_MAX_SECONDS") or "90.0").strip() or "90.0")

    for attempt in range(max_attempts):
        try:
            return client.models.generate_content(
                model=model, contents=prompt, config=config
            )
        except Exception as e:
            if not _retryable_genai_api_error(e) or attempt >= max_attempts - 1:
                raise
            delay = min(cap, base * (2**attempt))
            delay += random.uniform(0.0, min(3.0, max(0.5, delay * 0.15)))
            if debug:
                print(
                    f"Gemini call failed ({e!r}); sleeping {delay:.1f}s "
                    f"before retry {attempt + 2}/{max_attempts}...",
                    file=sys.stderr,
                )
            time.sleep(delay)


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
- Estimate today's area-average regular gas price (pump-style cents per liter) from recent local sources when possible.
- Produce a best-estimate prediction for tomorrow's expected pump price in cents per liter (e.g. 152.9).
- Provide a short, practical recommendation. Do not include anything about checking local gas price apps. Justify your recommendation.

Output requirements (STRICT JSON ONLY):
{{
  "city": string,
  "today_average_price_cents_per_liter": number | null,   // best estimate for today; null only if no usable data
  "tomorrow_expected_price_cents_per_liter": number,   // numeric pump-style price (e.g. 152.9)
  "influences": [string, ...],                 // 2-5 bullets, each < 110 chars
  "notable_information": [string, ...],        // 1-3 bullets, each < 140 chars
  "recommendation": string                     // <= 200 chars, actionable
}}

Guidance:
- If sources disagree, choose the most credible and recent; reflect uncertainty in wording of influences/notables.
- Keep it grounded in facts from sources you find; do not invent specific events.
""".strip()

    resp = _generate_content_with_retries(
        client=client,
        model=model,
        prompt=prompt,
        config=config,
        debug=_env_bool("DEBUG", False),
    )
    data = _safe_json_loads(resp.text or "")

    # Backward/robustness: if the model returns dollars/L by mistake, convert.
    cents = data.get("tomorrow_expected_price_cents_per_liter", None)
    if cents is None and "tomorrow_expected_price_per_liter" in data:
        cents = float(data["tomorrow_expected_price_per_liter"]) * 100.0
    if cents is None:
        raise ValueError("Gemini response missing tomorrow_expected_price_cents_per_liter")

    today_cents: float | None = None
    raw_today = data.get("today_average_price_cents_per_liter", None)
    if raw_today is None and "today_average_price_per_liter" in data:
        raw_today = float(data["today_average_price_per_liter"]) * 100.0
    if raw_today is not None:
        today_cents = float(raw_today)

    forecast = Forecast(
        city=str(data.get("city") or city),
        today_average_price_cents_per_liter=today_cents,
        tomorrow_expected_price_cents_per_liter=float(cents),
        influences=[str(x) for x in (data.get("influences") or [])][:5],
        notable_information=[str(x) for x in (data.get("notable_information") or [])][:3],
        recommendation=str(data.get("recommendation") or "").strip(),
    )
    sources = _extract_grounding_sources(resp)
    return forecast, sources, _extract_grounding_web_search_queries(resp)


def _squeeze(s: str) -> str:
    return " ".join((s or "").strip().split())


def _truncate(s: str, max_len: int) -> str:
    s = _squeeze(s)
    if len(s) <= max_len:
        return s
    if max_len <= 1:
        return s[:max_len]
    return s[: max_len - 1].rstrip() + "…"


def _fmt_cents_per_liter(x: float | None) -> str:
    if x is None:
        return "N/A"
    return f"{x:.1f}¢/L"


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
    """
    template = (template or "compact").strip().lower()

    if template == "detailed":
        lines: list[str] = []
        lines.append("Gas Forecast")
        lines.append("")
        lines.append(f"Today's Average: {_fmt_cents_per_liter(forecast.today_average_price_cents_per_liter)}")
        lines.append("")
        lines.append(
            f"Tomorrow's Expected Price: {forecast.tomorrow_expected_price_cents_per_liter:.1f}¢/L"
        )
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
        # Do not run detailed text through _truncate/_squeeze: that collapses newlines and
        # makes long briefings look like a single short "compact" blurb when max_len is small.
        if max_len <= 0 or len(body) <= max_len:
            return body
        return body[: max_len - 1].rstrip() + "…"

    if template == "compact":
        today_line = f"Today's Average: {_fmt_cents_per_liter(forecast.today_average_price_cents_per_liter)}"
        price_line = f"Tomorrow's Expected Price: {forecast.tomorrow_expected_price_cents_per_liter:.1f}¢/L"
        rec = _squeeze(forecast.recommendation)
        sep = "\n\n"
        rec_prefix = "Recommendation: "
        body = f"{today_line}{sep}{price_line}{sep}{rec_prefix}{rec}"

        if max_len <= 0 or len(body) <= max_len:
            return body

        # Keep both price lines; truncate recommendation only.
        header = f"{today_line}{sep}{price_line}{sep}{rec_prefix}"
        if len(header) >= max_len:
            return _truncate(body, max_len)
        room = max_len - len(header)
        rec2 = _truncate(rec, max(0, room))
        return f"{header}{rec2}"

    raise ValueError(f"Unknown SMS_TEMPLATE: {template!r}")


def send_sms_via_twilio(*, body: str, to_numbers: list[str]) -> list[str]:
    account_sid = _env_required("TWILIO_ACCOUNT_SID")
    auth_token = _env_required("TWILIO_AUTH_TOKEN")
    from_number = _env_required("TWILIO_FROM_NUMBER")

    if not to_numbers:
        raise ValueError("No destination phone numbers provided.")

    client = TwilioClient(account_sid, auth_token)
    sids: list[str] = []
    for to in to_numbers:
        msg = client.messages.create(from_=from_number, to=to, body=body)
        sids.append(str(msg.sid))
    return sids


def log_sources(
    *,
    city: str,
    sources: Iterable[dict[str, str]],
    model: str,
    web_search_queries: list[str] | None = None,
    recipient_count: int | None = None,
    sms_template: str | None = None,
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
        "recipient_count": recipient_count,
        "sms_template": sms_template,
    }
    path.write_text("", encoding="utf-8") if not path.exists() else None
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> None:
    # Load variables from a local .env file if present.
    load_dotenv()

    debug = _env_bool("DEBUG", False)
    dry_run = _env_bool("DRY_RUN", False)
    tz_name = (os.environ.get("TIMEZONE") or "America/Toronto").strip()

    api_key = _env_required("GEMINI_API_KEY")
    model = (os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash").strip()
    region = (os.environ.get("REGION") or "Ontario, Canada").strip()
    sms_template_default = _validated_sms_template(os.environ.get("SMS_TEMPLATE") or "compact")

    recipients = _parse_recipients_from_env(default_template=sms_template_default)
    if not recipients:
        raise SystemExit(
            "Set RECIPIENTS (multiline 'phone, city[, template]' in .env) or RECIPIENTS_FILE. "
            "See .env.example."
        )
    city_groups = _group_recipients_by_city_and_template(recipients)

    errors: list[str] = []
    all_sids: list[str] = []
    any_success = False

    for city_key, city_label, tmpl, phones in city_groups:
        try:
            if debug:
                print(
                    f"→ Generating Gemini forecast for {city_label!r} [{tmpl}] ({len(phones)} recipient(s))..."
                )
            forecast, sources, web_queries = generate_forecast_with_gemini(
                city=city_label,
                region=region,
                model=model,
                api_key=api_key,
                tz_name=tz_name,
            )
            log_sources(
                city=city_label,
                sources=sources,
                model=model,
                web_search_queries=web_queries,
                recipient_count=len(phones),
                sms_template=tmpl,
            )
            body = format_sms_with_template(
                forecast,
                template=tmpl,
                max_len=_sms_max_len_for_template(tmpl),
            )

            if dry_run:
                print(f"--- {city_label} ({city_key}) [{tmpl}] — {len(phones)} number(s) ---")
                for p in phones:
                    print(f"  → {p}")
                print(body)
                print()
                any_success = True
                continue

            sids = send_sms_via_twilio(body=body, to_numbers=phones)
            all_sids.extend(sids)
            any_success = True
        except Exception as e:
            errors.append(f"[ERROR] {city_label} [{tmpl}]: {e}")

    if errors:
        for e in errors:
            print(e, file=sys.stderr)

    if not any_success:
        raise SystemExit(1)

    if dry_run:
        return

    if debug:
        print(f"✓ Sent SMS via Twilio. Message SID(s): {', '.join(all_sids)}")


if __name__ == "__main__":
    main()
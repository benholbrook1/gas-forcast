# Gas Forecast (Gemini → Twilio SMS)

Generates a short **tomorrow gas price prediction** with:
- a **Current Influences** section
- a **Notable Information** section
- an actionable **Recommendation**

It uses **Gemini with Google Search grounding** for web research, logs the sources locally, then sends the briefing via **Twilio SMS**.

## Setup

Create your env file:

- Copy `.env.example` to `.env`
- Fill in `GEMINI_API_KEY`, Twilio credentials, and **`RECIPIENTS`** (or **`RECIPIENTS_FILE`**)

### Recipients (phone + city [+ template])

Use **`RECIPIENTS`** (multiline: `phone, city` or `phone, city, template`) or **`RECIPIENTS_FILE`**. **`template`** is `compact` or `detailed` and is optional; if omitted, **`SMS_TEMPLATE`** applies to that line.

Groups are **city + template** (city match is case-insensitive): **one Gemini request per group**; every number in the group gets the same SMS. Same city with different templates means separate Gemini calls.

In `.env`, multiline values must be **double-quoted**:

```env
RECIPIENTS="123-456-7890, waterloo, compact
324-543-2356, burlington, detailed
324-352-2356, waterloo"
```

10-digit numbers are normalized to **+1** by default (`DEFAULT_PHONE_COUNTRY_PREFIX=1`).

Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

Dry run (prints the SMS instead of sending):

```bash
DRY_RUN=true python forecast.py
```

Send SMS:

```bash
DRY_RUN=false python forecast.py
```

## SMS templates

This project defaults to **122 characters** max per SMS body (configurable).

Configure with:
- `SMS_TEMPLATE=compact` or `detailed` — default for recipient lines that omit the third field
- Per row: `phone, city, compact` or `phone, city, detailed` in **`RECIPIENTS`**
- `SMS_MAX_LEN` — applies to **`compact`** only (e.g. `122`)
- `SMS_MAX_LEN_DETAILED` — applies to **`detailed`** only; default **`0`** = send the full briefing (often multiple SMS segments). Optional aliases: `detail`, `short` for templates in `RECIPIENTS`

## Source logging

Grounding sources (URLs/titles returned by Gemini) are appended to:

- `logs/sources.jsonl`

No links are included in the SMS message body.


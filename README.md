# Gas Forecast (Gemini → Twilio SMS)

Generates a short **tomorrow gas price prediction** with:
- a **Current Influences** section
- a **Notable Information** section
- an actionable **Recommendation**

It uses **Gemini with Google Search grounding** for web research, logs the sources locally, then sends the briefing via **Twilio SMS**.

## Setup

Create your env file:

- Copy `.env.example` to `.env`
- Fill in `GEMINI_API_KEY` and Twilio credentials/numbers

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
- `SMS_TEMPLATE=compact` (default; tomorrow’s expected ¢/L, then recommendation)
- `SMS_TEMPLATE=detailed` (full multi-line briefing)
- `SMS_MAX_LEN=122`

## Source logging

Grounding sources (URLs/titles returned by Gemini) are appended to:

- `logs/sources.jsonl`

No links are included in the SMS message body.


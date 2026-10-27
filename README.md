# Sisi — the spaza sister who never forgets

Sisi is a WhatsApp-first assistant for South African spaza shop owners. The owner sends a voice note in her own words ("Sipho took two bread and a 2L Coke on the book"), a photo of a shelf or wholesaler slip, or a short text, and Sisi keeps the credit book (ibhuku), stock and sales, low-stock alerts, a restock list grouped by wholesaler, expiry checks, the daily cash-up, and a compliance helper for registration and the Spaza Shop Support Fund. Every write is shown as a short preview with Yes/No buttons and nothing is saved until the owner confirms; every saved action can be undone.

**The problem.** After the 2024 foodborne-illness deaths linked to spaza shops, every spaza had to register with its municipality (registration closed 28 Feb 2025); of 44,696 verified shops only about 15,000 hold trading permits. The R500 million Spaza Shop Support Fund offers up to R100,000 per shop but is gated by paperwork, and government warns of fake paid "application assistants" — applications are free. Sources: [gov.za media statement, 29 May 2026](https://www.gov.za), [the dtic, 6 Jun 2025](https://www.thedtic.gov.za), [SAnews, 17 Apr 2025](https://www.sanews.gov.za), [spazashopfund.co.za](https://spazashopfund.co.za).
Video stats: ±150,000 spaza shops (UCT Case Writing Centre; Accenture Africa 2023 via Daily Investor), 5.2% of GDP and 2.6 million jobs (UCT via supermarket.co.za).

**Built during the hackathon.** Everything in this repo was built during the Meta Global AI Developer Hackathon 2026 (26 Oct – 4 Nov 2026). No earlier code was imported. The git history shows when each feature was built.

## Architecture

```mermaid
flowchart LR
    Owner -- voice/photo/text --> WA[WhatsApp Cloud API]
    WA -- webhook --> API[FastAPI]
    API --> Agent[agent.py: Yes/No/Undo deterministic]
    Agent --> Voice[Muse Voice Transcribe]
    Agent --> Vision[Muse Spark vision + SAM 3.1]
    Agent --> LLM[Muse Spark tool loop]
    LLM --> Tools[tools.py: dry-run preview]
    Tools --> DB[(SQLite per-shop data)]
    Agent -- reply + Yes/No buttons --> WA
```

## Meta models

| Job | Model | How |
|---|---|---|
| Agent + tool calling + replies | `muse-spark-1.3` (`-contributor` for dev/tests) | Chat Completions, tools, `tool_choice: auto` |
| Vision (expiry, slips, shelves) | `muse-spark-1.3` | base64 data URL + `response_format: json_schema` |
| Voice notes | `muse-voice-transcribe-1.0` | `POST /v1/asr/transcribe`, 16 kHz mono wav via ffmpeg |
| Shelf counting (stretch) | `sam-3.1` | `POST /v1/responses` (streamed) |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in MODEL_API_KEY, WhatsApp tokens
python -m seed.seed_demo
pytest
```

Run the API: `.venv/bin/python -m uvicorn app.main:app --port 8000`.

## Try it (judges)

- Terminal chat (real model, no WhatsApp): `python -m seed.seed_demo && python -m sisi.chat`, shop `27710000001` (MaZulu's Spaza, fictional).
- Simulated WhatsApp (no Meta account, same webhook + buttons path as production):

  ```bash
  SISI_DB=/tmp/sisi_sim.db .venv/bin/python -m uvicorn app.main:app --port 8000 &
  python scripts/simulate.py "Sipho took 2 Sasko white bread and a 2L Coke on the book"
  python scripts/simulate.py "Yes"
  ```

  Add `APP_SECRET=dev-secret` in front of both commands to also exercise signature verification.
- Voice notes without WhatsApp: `python scripts/simulate.py --audio samples/voice_sipho_credit.ogg`
  sends a local recording through the full voice pipeline (ffmpeg → Muse Voice Transcribe →
  Sisi's reply with the transcript shown first). Needs `ffmpeg` installed and `MODEL_API_KEY` set.
  More samples in [samples/](samples/): cash sale, restock, restock list, cash-up.
- Expiry photo without WhatsApp: `python scripts/simulate.py --photo <shelf.jpg>`
  reads best-before stamps via vision + strict structured output and proposes an
  expiry note. Set `SISI_TODAY=YYYY-MM-DD` on the server to freeze "today" for
  dated props.
- Compliance: `python scripts/simulate.py "Can I get the spaza fund?"` answers
  with the checklist, the official link, and the applications-are-free warning.
- Live WhatsApp: message the test number (see hackathon submission) — try "Sipho took two bread and a 2L Coke on the book", tap Yes, then Undo.
- Screenshots: [screenshots/](screenshots/).

## Reply style guide (standing rule)

Every WhatsApp reply must be visually polished:

- Emoji section headings, WhatsApp *bold* labels, one item per line, a blank
  line between sections. Short sentences, no walls of text.
- Amounts as R0.00. ⚠️ for warnings, ❌ for out of stock, ✅ for confirmations.
- No line over 120 characters; anything over 300 characters has line breaks;
  anything over about 1,500 characters is split at a section boundary
  (`split_message` in `app/whatsapp.py`, applied by both live senders).
- The privacy PS appears only on the first message or in "demo help".
- The model relays tool results exactly as written (see `REPLY STYLE` in
  `app/prompts.py`). `tests/test_format.py` fails the suite if any
  deterministic reply breaks these rules.

## Go live (no code changes)

Sisi auto-selects the fake sender until both of these are set; add exactly 2 env vars to send via the Cloud API:

```bash
WHATSAPP_TOKEN=...
WHATSAPP_PHONE_NUMBER_ID=...
```

Also set `APP_SECRET` (webhook signature check) and `VERIFY_TOKEN` (webhook verification) before pointing Meta at `/webhook`.

## Deploy (Modal)

```bash
modal setup                                     # once: browser login
modal secret create sisi-env --from-dotenv .env   # once per secret change
modal deploy modal_app.py                       # prints the public URL
```

Paste the printed `https://...modal.run/webhook` URL into the WhatsApp app's
webhook settings (with your `VERIFY_TOKEN`). The app ([modal_app.py](modal_app.py))
runs one warm container with ffmpeg in the image and SQLite on a persistent
`sisi-data` volume. Seed the demo shop remotely after deploy:

```bash
curl -X POST 'https://...modal.run/_admin/seed?token=$VERIFY_TOKEN&shop=27710000001'
```

Rollback: check out the last good commit and `modal deploy modal_app.py` again.

Local alternative: `ngrok http 8000`, then point the webhook at the ngrok URL.

## Vonage sandbox channel (alternative to Meta)

Set `VONAGE_API_KEY`, `VONAGE_API_SECRET` (and optionally `VONAGE_WHATSAPP_FROM`,
default `14157386102`), redeploy, and point the Vonage Messages sandbox at
`https://...modal.run/webhook/vonage` (inbound) and
`https://...modal.run/webhook/vonage/status` (receipts). Vonage messages flow
through the same agent, voice, and vision handlers; replies go back via
Vonage. The Meta `/webhook` path is unchanged.

Hardening in place: signature verification (fails closed when `APP_SECRET` is
set), 256 KB webhook body cap, 30/min per-shop rate limit, retries with backoff
on outbound calls, consent line on first contact, `delete my data` erasure, and
no secrets in code or logs (`pip-audit` clean).

## Honest notes

- Muse Voice Transcribe does not officially support isiZulu/Afrikaans. Sisi uses an English language bias plus a keywords list of local brands and slang (Sasko, Albany, Iwisa, ibhuku, skoloto, …) and **always shows the owner what she understood before saving**.
- Token usage is logged per request to `token_usage.log` for spend tracking.

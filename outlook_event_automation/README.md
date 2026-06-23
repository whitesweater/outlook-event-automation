# Outlook Event Automation

An always-on mail-to-calendar agent for event-heavy inboxes.

The service reads recent Outlook or Gmail messages, extracts calendar-worthy
events with an OpenAI-compatible Responses API, writes eligible events to
Outlook Calendar or Google Calendar, and records local dedupe/review state.

## What It Does

- Reads Outlook Mail through Microsoft Graph delegated or app-only auth.
- Optionally reads Gmail messages through the Gmail API.
- Extracts structured event data with JSON-schema Responses API output.
- Writes to Outlook Calendar or Google Calendar.
- Stores dedupe and processing state in SQLite.
- Runs continuously as a systemd service.
- Uses batch extraction and retry/split fallback for larger inbox scans.

## Safety Rules

The agent is conservative by default:

- `ignored`: clearly not a calendar event.
- `needs_review`: event-like but ambiguous, missing required times, multi-event,
  or cancellation/recall related.
- `dry_run`: eligible event in a non-writing test run.
- `created`: event written to the configured calendar.

Known noisy summary mail such as `Daily Event Alert` can be ignored before
calling the model. Cancellation and recall messages are forced to review and
will not be auto-created.

## Quick Start

Create a local config from the example:

```bash
cp config.example.json config.local.json
cp .env.example .env
```

Set the relevant OAuth client IDs, mailbox settings, and API keys in:

```text
config.local.json
.env
```

Authorize Microsoft Graph with device code:

```bash
python3 event_agent.py --config config.local.json auth-microsoft
```

Run a non-writing scan:

```bash
python3 event_agent.py --config config.local.json run --source outlook --sink none --limit 20 --force
```

Write eligible events to Outlook Calendar:

```bash
python3 event_agent.py --config config.local.json run --source outlook --sink outlook --limit 20 --write
```

Run permanently:

```bash
python3 event_agent.py --config config.local.json serve --write
```

## Configuration Notes

Important settings live in `config.example.json`:

- `source`: `outlook` or `gmail`.
- `calendar.sink`: `outlook`, `google`, or `none`.
- `extraction.openai_model`: model name for the Responses API.
- `extraction.batch_size`: messages per model request.
- `calendar.include_source_email_body`: attach source mail text to calendar
  event bodies.

Environment variables live in `.env`:

```text
OPENAI_API_KEY=...
OPENAI_BASE_URL=http://127.0.0.1:8317/v1
OPENAI_RESPONSES_URL=http://127.0.0.1:8317/v1/responses
MICROSOFT_CLIENT_SECRET=...
MICROSOFT_USER_ID=...
```

## Deploy With systemd

Install the service:

```bash
sudo APP_DIR=/opt/outlook-event-agent bash scripts/install-systemd.sh
```

Then edit `/opt/outlook-event-agent/config.local.json` and
`/opt/outlook-event-agent/.env`, and start:

```bash
sudo systemctl start outlook-event-agent
sudo systemctl status outlook-event-agent
journalctl -u outlook-event-agent -f
```

## Project Site

The GitHub Pages homepage is in `../docs/index.html`.

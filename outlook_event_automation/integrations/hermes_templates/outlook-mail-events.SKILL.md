---
name: outlook-mail-events
description: Query the Outlook Event Automation service for Outlook Calendar agenda, multi-day agenda ranges, mail-derived calendar events, pending reviews, digests, health, recent run status, and create confirmed manual Outlook Calendar events.
---

# Outlook Mail And Calendar Queries

Use this skill when the user asks about today's schedule, tomorrow's schedule, the next few days, the next week, recent mail-derived events, pending review emails, calendar sync health, whether the mail automation is working, or adding a clearly confirmed Outlook Calendar event.

The local helper is:
`/opt/hermes-agent/home/bin/outlook-mail-events`

Commands:
- `/opt/hermes-agent/home/bin/outlook-mail-events agenda today 50` for today's Outlook Calendar agenda.
- `/opt/hermes-agent/home/bin/outlook-mail-events agenda tomorrow 50` for tomorrow's agenda.
- `/opt/hermes-agent/home/bin/outlook-mail-events agenda YYYY-MM-DD 50` for a specific day.
- `/opt/hermes-agent/home/bin/outlook-mail-events agenda-range 3 today 100` for the next 3 days.
- `/opt/hermes-agent/home/bin/outlook-mail-events agenda-range 7 today 100` for the next week.
- `/opt/hermes-agent/home/bin/outlook-mail-events digest 24 20` for recent mail activity summary.
- `/opt/hermes-agent/home/bin/outlook-mail-events review 24 20` for pending review emails.
- `/opt/hermes-agent/home/bin/outlook-mail-events events created 24 20` for created calendar events.
- `/opt/hermes-agent/home/bin/outlook-mail-events health` for service health.
- `/opt/hermes-agent/home/bin/outlook-mail-events last-run` for last scan details.
- `cat event.json | /opt/hermes-agent/home/bin/outlook-mail-events create-event-json` to create a confirmed Outlook Calendar event.

Read the JSON, then answer in Chinese by default. Prefer the `markdown` field when it exists. For questions like “最近三天有什么日程” or “最近一周有哪些安排”, call `agenda-range` with `3` or `7` days. Do not expose `OUTLOOK_AGENT_API_TOKEN`, webhook secrets, raw `.env` contents, or full email bodies unless the user explicitly asks for source detail.

For adding a calendar event:

1. Resolve relative dates in `Asia/Shanghai` and state the exact date and time back to the user.
2. Ask for confirmation unless the user's latest message already explicitly confirms the exact title, date, time, timezone, and Outlook Calendar target.
3. After confirmation, call `create-event-json` with JSON like:

```json
{
  "confirmed": true,
  "title": "字节跳动面试",
  "start_time": "2026-06-25T11:00:00+08:00",
  "end_time": "2026-06-25T12:00:00+08:00",
  "timezone": "Asia/Shanghai",
  "location": "",
  "description": "通过 Hermes 手动添加。"
}
```

For validation without writing, include `"dry_run": true`. Do not create events if the date, time, or title is ambiguous. This skill can add confirmed events, but it does not update or delete existing events.

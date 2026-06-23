#!/usr/bin/env python3
"""Forwarded mail -> structured event -> calendar automation.

This CLI intentionally uses only the Python standard library so the first
setup pass is easy to inspect and run on Windows, WSL, or a small server.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import http.server
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.local.json"
EXAMPLE_CONFIG = BASE_DIR / "config.example.json"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "events.sqlite3"
NOTIFICATION_STATE_PATH = DATA_DIR / "notification_state.json"
UTC = timezone.utc

MICROSOFT_AUTH_BASE = "https://login.microsoftonline.com"
MICROSOFT_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
GOOGLE_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"

EVENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "is_event": {"type": "boolean"},
        "confidence": {"type": "number"},
        "title": {"type": "string"},
        "start_time": {"type": "string"},
        "end_time": {"type": "string"},
        "timezone": {"type": "string"},
        "location": {"type": "string"},
        "description": {"type": "string"},
        "organizer": {"type": "string"},
        "requires_review": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": [
        "is_event",
        "confidence",
        "title",
        "start_time",
        "end_time",
        "timezone",
        "location",
        "description",
        "organizer",
        "requires_review",
        "reason",
    ],
}

BATCH_EVENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "events": {
            "type": "array",
            "items": EVENT_SCHEMA,
        }
    },
    "required": ["events"],
}

WINDOWS_TIMEZONE_BY_IANA = {
    "UTC": "UTC",
    "Etc/UTC": "UTC",
    "Asia/Shanghai": "China Standard Time",
    "Asia/Hong_Kong": "China Standard Time",
    "Asia/Taipei": "Taipei Standard Time",
    "America/New_York": "Eastern Standard Time",
    "America/Chicago": "Central Standard Time",
    "America/Denver": "Mountain Standard Time",
    "America/Los_Angeles": "Pacific Standard Time",
    "Europe/London": "GMT Standard Time",
}


class AgentError(RuntimeError):
    """Expected operational error with a user-facing message."""


@dataclass
class SourceMessage:
    source: str
    message_id: str
    subject: str
    sender: str
    received_at: str
    body_text: str
    web_link: str = ""


def utc_now() -> datetime:
    return datetime.now(UTC)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AgentError(
            f"Missing config: {path}. Run `python event_agent.py init` first."
        )
    return load_json(path)


def write_local_config(path: Path) -> None:
    if path.exists():
        print(f"Config already exists: {path}")
        return
    config = load_json(EXAMPLE_CONFIG)
    save_json(path, config)
    print(f"Created {path}")
    print("Edit client IDs and calendar settings before authenticating.")


def form_post_json(url: str, form: dict[str, str], timeout: int = 60) -> dict[str, Any]:
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    return read_json_response(req, timeout=timeout)


def http_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    payload: Any | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    body = None
    merged_headers = dict(headers or {})
    if token:
        merged_headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        merged_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=merged_headers, method=method)
    return read_json_response(req, timeout=timeout)


def http_post_raw(
    url: str,
    *,
    payload: Any,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> tuple[int, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    merged_headers = {"Content-Type": "application/json; charset=utf-8"}
    merged_headers.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=merged_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise AgentError(f"HTTP {exc.code} from {url}: {details}") from exc
    except urllib.error.URLError as exc:
        raise AgentError(f"Network error from {url}: {exc}") from exc


def http_post_json_bytes(
    url: str,
    *,
    body: bytes,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> tuple[int, str]:
    merged_headers = {"Content-Type": "application/json; charset=utf-8"}
    merged_headers.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=merged_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise AgentError(f"HTTP {exc.code} from {url}: {details}") from exc
    except urllib.error.URLError as exc:
        raise AgentError(f"Network error from {url}: {exc}") from exc


def read_json_response(req: urllib.request.Request, timeout: int = 60) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise AgentError(f"HTTP {exc.code} from {req.full_url}: {details}") from exc
    except urllib.error.URLError as exc:
        raise AgentError(f"Network error from {req.full_url}: {exc}") from exc


def config_path_value(config: dict[str, Any], section: str, key: str, default: Path) -> Path:
    value = config.get(section, {}).get(key)
    return BASE_DIR / value if value else default


def ensure_client_id(config: dict[str, Any], section: str) -> str:
    client_id = config.get(section, {}).get("client_id", "").strip()
    if not client_id or client_id.startswith("PASTE_"):
        raise AgentError(f"Set `{section}.client_id` in config.local.json first.")
    return client_id


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def microsoft_token_path(config: dict[str, Any]) -> Path:
    return config_path_value(config, "microsoft", "token_file", DATA_DIR / "microsoft_token.json")


def google_token_path(config: dict[str, Any]) -> Path:
    return config_path_value(config, "google", "token_file", DATA_DIR / "google_token.json")


def format_oauth_error(fields: dict[str, str]) -> str:
    keys = [
        "error",
        "error_description",
        "suberror",
        "error_codes",
        "timestamp",
        "trace_id",
        "correlation_id",
    ]
    lines = []
    for key in keys:
        value = fields.get(key)
        if value:
            lines.append(f"{key}: {value}")
    extras = sorted(k for k in fields if k not in keys and k not in {"state"})
    for key in extras:
        lines.append(f"{key}: {fields[key]}")
    return "\n".join(lines) if lines else "Unknown OAuth error"


def auth_microsoft(config: dict[str, Any]) -> None:
    ms = config.get("microsoft", {})
    client_id = ensure_client_id(config, "microsoft")
    tenant = ms.get("tenant", "organizations")
    scopes = " ".join(ms.get("scopes", ["offline_access", "Mail.Read"]))
    device_url = f"{MICROSOFT_AUTH_BASE}/{tenant}/oauth2/v2.0/devicecode"
    token_url = f"{MICROSOFT_AUTH_BASE}/{tenant}/oauth2/v2.0/token"

    device = form_post_json(device_url, {"client_id": client_id, "scope": scopes})
    print("\nMicrosoft login required:")
    print(f"  Open: {device.get('verification_uri')}")
    print(f"  Code: {device.get('user_code')}")
    print(device.get("message", "Approve the device code in your browser."))

    interval = int(device.get("interval", 5))
    expires_at = time.time() + int(device.get("expires_in", 900))
    while time.time() < expires_at:
        time.sleep(interval)
        try:
            token = form_post_json(
                token_url,
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": client_id,
                    "device_code": device["device_code"],
                },
            )
        except AgentError as exc:
            text = str(exc)
            if "authorization_pending" in text:
                continue
            if "slow_down" in text:
                interval += 5
                continue
            raise
        store_token(microsoft_token_path(config), token)
        print(f"Stored Microsoft token: {microsoft_token_path(config)}")
        return
    raise AgentError("Microsoft device-code login expired. Run auth-microsoft again.")


def auth_microsoft_web(config: dict[str, Any]) -> None:
    ms = config.get("microsoft", {})
    client_id = ensure_client_id(config, "microsoft")
    client_secret = os.environ.get("MICROSOFT_CLIENT_SECRET", "").strip()
    if not client_secret:
        raise AgentError("Set MICROSOFT_CLIENT_SECRET in .env before auth-microsoft-web.")
    tenant = ms.get("tenant", "organizations")
    scopes = " ".join(ms.get("scopes", ["offline_access", "Mail.Read"]))
    port = int(ms.get("redirect_port", 5000))
    redirect_path = ms.get("redirect_path", "/getAToken")
    redirect_uri = ms.get("redirect_uri") or f"http://localhost:{port}{redirect_path}"
    auth_url_base = f"{MICROSOFT_AUTH_BASE}/{tenant}/oauth2/v2.0/authorize"
    token_url = f"{MICROSOFT_AUTH_BASE}/{tenant}/oauth2/v2.0/token"
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": scopes,
        "state": state,
        "prompt": "select_account",
    }
    auth_url = f"{auth_url_base}?{urllib.parse.urlencode(params)}"
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            parsed = urllib.parse.urlparse(self.path)
            values = urllib.parse.parse_qs(parsed.query)
            incoming = {k: v[0] for k, v in values.items() if v}
            if "code" in incoming or "error" in incoming:
                result.update(incoming)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            if "code" in incoming:
                text = "Microsoft authorization complete. You can close this tab.\n"
            elif "error" in incoming:
                text = f"Microsoft authorization failed:\n{format_oauth_error(incoming)}\n"
            else:
                text = "Microsoft authorization listener is running. Continue the login flow in the browser.\n"
            self.wfile.write(text.encode("utf-8"))

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    print("\nMicrosoft browser login required:")
    print(f"  Keep SSH tunnel open: ssh -L {port}:127.0.0.1:{port} your-server")
    print(f"  Open: {auth_url}")
    deadline = time.time() + int(ms.get("auth_timeout_seconds", 600))
    while not result and time.time() < deadline:
        server.handle_request()
    if not result:
        raise AgentError("Microsoft OAuth timed out waiting for the browser callback.")
    if result.get("state") != state:
        print("Warning: Microsoft OAuth state mismatch; continuing for local one-shot setup.")
    if "error" in result:
        raise AgentError(f"Microsoft OAuth error:\n{format_oauth_error(result)}")
    code = result.get("code")
    if not code:
        raise AgentError("Microsoft OAuth did not return an authorization code.")
    token = form_post_json(
        token_url,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "scope": scopes,
        },
    )
    token["auth_mode"] = "auth_code"
    store_token(microsoft_token_path(config), token)
    print(f"Stored Microsoft delegated token: {microsoft_token_path(config)}")


def refresh_microsoft_token(config: dict[str, Any]) -> str:
    auth_mode = config.get("microsoft", {}).get("auth_mode", "device_code")
    if auth_mode == "client_credentials":
        return refresh_microsoft_client_credentials_token(config)
    path = microsoft_token_path(config)
    token = load_token(path)
    if token_is_valid(token):
        return token["access_token"]
    refresh_token = token.get("refresh_token")
    if not refresh_token:
        raise AgentError("Microsoft token has no refresh_token. Run auth-microsoft.")
    ms = config.get("microsoft", {})
    client_id = ensure_client_id(config, "microsoft")
    tenant = ms.get("tenant", "organizations")
    scopes = " ".join(ms.get("scopes", ["offline_access", "Mail.Read"]))
    refreshed = form_post_json(
        f"{MICROSOFT_AUTH_BASE}/{tenant}/oauth2/v2.0/token",
        {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": scopes,
        },
    )
    if "refresh_token" not in refreshed:
        refreshed["refresh_token"] = refresh_token
    store_token(path, refreshed)
    return refreshed["access_token"]


def refresh_microsoft_client_credentials_token(config: dict[str, Any]) -> str:
    path = microsoft_token_path(config)
    if path.exists():
        token = load_token(path)
        if token.get("auth_mode") == "client_credentials" and token_is_valid(token):
            return token["access_token"]
    ms = config.get("microsoft", {})
    client_id = ensure_client_id(config, "microsoft")
    client_secret = os.environ.get("MICROSOFT_CLIENT_SECRET", "").strip()
    if not client_secret:
        raise AgentError("Set MICROSOFT_CLIENT_SECRET in .env for client_credentials mode.")
    tenant = ms.get("tenant", "organizations")
    if tenant in {"common", "consumers"}:
        raise AgentError(
            "Microsoft client_credentials mode needs a tenant ID or tenant domain, not common/consumers."
        )
    token = form_post_json(
        f"{MICROSOFT_AUTH_BASE}/{tenant}/oauth2/v2.0/token",
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
        },
    )
    token["auth_mode"] = "client_credentials"
    store_token(path, token)
    return token["access_token"]


def auth_google(config: dict[str, Any]) -> None:
    google = config.get("google", {})
    client_id = ensure_client_id(config, "google")
    client_secret = google.get("client_secret", "").strip()
    scopes = " ".join(google.get("scopes", ["https://www.googleapis.com/auth/calendar.events"]))
    port = int(google.get("redirect_port", 8765))
    redirect_uri = f"http://127.0.0.1:{port}/oauth2callback"

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    state = secrets.token_urlsafe(24)

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scopes,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"

    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            parsed = urllib.parse.urlparse(self.path)
            values = urllib.parse.parse_qs(parsed.query)
            result.update({k: v[0] for k, v in values.items() if v})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "Google authorization complete. You can close this tab.\n".encode("utf-8")
            )

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    print("\nGoogle login required:")
    print(f"  Open: {auth_url}")
    webbrowser.open(auth_url)
    server.handle_request()

    if result.get("state") != state:
        raise AgentError("Google OAuth state mismatch. Retry auth-google.")
    if "error" in result:
        raise AgentError(f"Google OAuth error: {result['error']}")
    code = result.get("code")
    if not code:
        raise AgentError("Google OAuth did not return an authorization code.")

    form = {
        "client_id": client_id,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    if client_secret:
        form["client_secret"] = client_secret
    token = form_post_json(GOOGLE_TOKEN_URL, form)
    store_token(google_token_path(config), token)
    print(f"Stored Google token: {google_token_path(config)}")


def refresh_google_token(config: dict[str, Any]) -> str:
    path = google_token_path(config)
    token = load_token(path)
    if token_is_valid(token):
        return token["access_token"]
    refresh_token = token.get("refresh_token")
    if not refresh_token:
        raise AgentError("Google token has no refresh_token. Run auth-google.")
    google = config.get("google", {})
    form = {
        "client_id": ensure_client_id(config, "google"),
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if google.get("client_secret"):
        form["client_secret"] = google["client_secret"]
    refreshed = form_post_json(GOOGLE_TOKEN_URL, form)
    refreshed["refresh_token"] = refresh_token
    store_token(path, refreshed)
    return refreshed["access_token"]


def require_google_scopes(config: dict[str, Any]) -> None:
    scopes = set(config.get("google", {}).get("scopes", []))
    needed = {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.events",
    }
    missing = sorted(needed - scopes)
    if missing:
        raise AgentError(
            "Google config is missing required scopes: "
            + ", ".join(missing)
            + ". Update config.local.json and rerun auth-google."
        )


def load_token(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AgentError(f"Missing token file: {path}")
    return load_json(path)


def store_token(path: Path, token: dict[str, Any]) -> None:
    token = dict(token)
    token["obtained_at"] = int(time.time())
    token["expires_at"] = int(time.time()) + int(token.get("expires_in", 3600))
    save_json(path, token)


def token_is_valid(token: dict[str, Any]) -> bool:
    return bool(token.get("access_token")) and int(token.get("expires_at", 0)) > int(time.time()) + 120


def fetch_outlook_messages(config: dict[str, Any], limit: int | None = None) -> list[SourceMessage]:
    token = refresh_microsoft_token(config)
    ms = config.get("microsoft", {})
    folder = urllib.parse.quote(ms.get("mail_folder", "inbox"))
    top = limit or int(ms.get("message_limit", 25))
    select = "id,subject,receivedDateTime,from,bodyPreview,body,webLink,hasAttachments"
    query = urllib.parse.urlencode(
        {"$select": select, "$top": str(top), "$orderby": "receivedDateTime desc"}
    )
    if ms.get("auth_mode") == "client_credentials":
        user_id = ms.get("user_id", "").strip() or os.environ.get("MICROSOFT_USER_ID", "").strip()
        if not user_id:
            raise AgentError("Set microsoft.user_id or MICROSOFT_USER_ID for client_credentials mode.")
        encoded_user = urllib.parse.quote(user_id, safe="")
        url = f"{MICROSOFT_GRAPH_BASE}/users/{encoded_user}/mailFolders/{folder}/messages?{query}"
    else:
        url = f"{MICROSOFT_GRAPH_BASE}/me/mailFolders/{folder}/messages?{query}"
    data = http_json("GET", url, token=token)
    messages = [graph_message_to_source(item) for item in data.get("value", [])]
    lookback_days = int(ms.get("lookback_days", 30))
    cutoff = utc_now() - timedelta(days=lookback_days)
    return [msg for msg in messages if not msg.received_at or parse_datetime(msg.received_at) >= cutoff]


def fetch_gmail_messages(config: dict[str, Any], limit: int | None = None) -> list[SourceMessage]:
    require_google_scopes(config)
    token = refresh_google_token(config)
    gmail = config.get("gmail", {})
    top = limit or int(gmail.get("message_limit", 25))
    query_value = gmail.get("query", "newer_than:7d -in:spam -in:trash")
    params: dict[str, Any] = {
        "maxResults": str(top),
        "q": query_value,
        "includeSpamTrash": "false",
    }
    label_ids = gmail.get("label_ids", ["INBOX"])
    if label_ids:
        params["labelIds"] = label_ids
    url = f"{GMAIL_API_BASE}/users/me/messages?{urlencode_query(params)}"
    data = http_json("GET", url, token=token)
    messages: list[SourceMessage] = []
    for item in data.get("messages", []):
        message_id = item.get("id")
        if not message_id:
            continue
        messages.append(fetch_gmail_message(config, token, message_id))
    return messages


def urlencode_query(params: dict[str, Any]) -> str:
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        if isinstance(value, list):
            pairs.extend((key, str(item)) for item in value)
        else:
            pairs.append((key, str(value)))
    return urllib.parse.urlencode(pairs)


def fetch_gmail_message(config: dict[str, Any], token: str, message_id: str) -> SourceMessage:
    fields = (
        "id,threadId,internalDate,labelIds,payload(headers,name,value,body/data,"
        "parts(mimeType,filename,body/data,parts(mimeType,filename,body/data))),snippet"
    )
    url = (
        f"{GMAIL_API_BASE}/users/me/messages/{urllib.parse.quote(message_id, safe='')}"
        f"?format=full&fields={urllib.parse.quote(fields, safe='(),/')}"
    )
    item = http_json("GET", url, token=token)
    headers = gmail_headers(item.get("payload", {}).get("headers", []))
    internal_date = item.get("internalDate")
    received_at = ""
    if internal_date:
        received_at = datetime.fromtimestamp(int(internal_date) / 1000, tz=UTC).isoformat()
    return SourceMessage(
        source="gmail",
        message_id=item.get("id", message_id),
        subject=headers.get("subject") or "(no subject)",
        sender=headers.get("from", ""),
        received_at=received_at,
        body_text=extract_gmail_body_text(item.get("payload", {})) or item.get("snippet", ""),
        web_link=gmail_web_link(item.get("threadId", "")),
    )


def gmail_headers(headers: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for header in headers:
        name = str(header.get("name", "")).lower()
        if name in {"subject", "from", "to", "date", "reply-to"}:
            result[name] = str(header.get("value", ""))
    return result


def gmail_web_link(thread_id: str) -> str:
    if not thread_id:
        return ""
    return f"https://mail.google.com/mail/u/0/#inbox/{thread_id}"


def extract_gmail_body_text(payload: dict[str, Any]) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    collect_gmail_parts(payload, plain_parts, html_parts)
    if plain_parts:
        return "\n\n".join(part.strip() for part in plain_parts if part.strip())
    if html_parts:
        return html_to_text("\n\n".join(html_parts))
    return ""


def collect_gmail_parts(payload: dict[str, Any], plain_parts: list[str], html_parts: list[str]) -> None:
    mime_type = payload.get("mimeType", "")
    data = (payload.get("body") or {}).get("data", "")
    if data:
        decoded = decode_gmail_data(data)
        if mime_type == "text/plain":
            plain_parts.append(decoded)
        elif mime_type == "text/html":
            html_parts.append(decoded)
    for part in payload.get("parts", []) or []:
        collect_gmail_parts(part, plain_parts, html_parts)


def decode_gmail_data(data: str) -> str:
    padding = "=" * (-len(data) % 4)
    raw = base64.urlsafe_b64decode((data + padding).encode("ascii"))
    return raw.decode("utf-8", errors="replace")


def graph_message_to_source(item: dict[str, Any]) -> SourceMessage:
    body = item.get("body") or {}
    content = body.get("content") or item.get("bodyPreview") or ""
    sender = (
        ((item.get("from") or {}).get("emailAddress") or {}).get("address")
        or ((item.get("from") or {}).get("emailAddress") or {}).get("name")
        or ""
    )
    return SourceMessage(
        source="outlook",
        message_id=item.get("id", ""),
        subject=item.get("subject") or "(no subject)",
        sender=sender,
        received_at=item.get("receivedDateTime") or "",
        body_text=html_to_text(content),
        web_link=item.get("webLink") or "",
    )


def load_fixture_messages(path: Path) -> list[SourceMessage]:
    data = load_json(path)
    raw_messages = data if isinstance(data, list) else data.get("messages", [data])
    messages: list[SourceMessage] = []
    for idx, item in enumerate(raw_messages):
        messages.append(
            SourceMessage(
                source=item.get("source", "fixture"),
                message_id=item.get("message_id", f"fixture-{idx}"),
                subject=item.get("subject", "(no subject)"),
                sender=item.get("sender", ""),
                received_at=item.get("received_at", utc_now().isoformat()),
                body_text=html_to_text(item.get("body_text", "")),
                web_link=item.get("web_link", ""),
            )
        )
    return messages


def html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</p\s*>", "\n", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n\s+", "\n", value)
    return value.strip()


def extract_event(message: SourceMessage, config: dict[str, Any]) -> dict[str, Any]:
    return extract_events([message], config)[0]


def extract_events(messages: list[SourceMessage], config: dict[str, Any]) -> list[dict[str, Any]]:
    extractor = config.get("extraction", {}).get("mode", "openai").lower()
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if extractor == "heuristic" or not api_key:
        return [heuristic_extract(message, config, reason="OPENAI_API_KEY not set") for message in messages]
    batch_size = max(1, int(config.get("extraction", {}).get("batch_size", 20)))
    events: list[dict[str, Any]] = []
    for batch in chunks(messages, batch_size):
        events.extend(openai_extract_batch_with_fallback(batch, config, api_key))
    return events


def chunks(values: list[SourceMessage], size: int) -> list[list[SourceMessage]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def openai_extract(message: SourceMessage, config: dict[str, Any], api_key: str) -> dict[str, Any]:
    return openai_extract_batch([message], config, api_key)[0]


def openai_extract_batch_with_fallback(
    messages: list[SourceMessage], config: dict[str, Any], api_key: str
) -> list[dict[str, Any]]:
    try:
        return openai_extract_batch(messages, config, api_key)
    except AgentError as exc:
        if len(messages) == 1:
            return [extraction_error_event(messages[0], config, str(exc))]
        midpoint = max(1, len(messages) // 2)
        print(
            f"Batch extraction failed for {len(messages)} messages; retrying as "
            f"{midpoint}+{len(messages) - midpoint}. Reason: {exc}",
            file=sys.stderr,
        )
        return (
            openai_extract_batch_with_fallback(messages[:midpoint], config, api_key)
            + openai_extract_batch_with_fallback(messages[midpoint:], config, api_key)
        )


def openai_extract_batch(
    messages: list[SourceMessage], config: dict[str, Any], api_key: str
) -> list[dict[str, Any]]:
    extraction = config.get("extraction", {})
    default_timezone = extraction.get("default_timezone", "Asia/Shanghai")
    body_limit = int(extraction.get("batch_body_limit", 6000))
    user_prompt = {
        "default_timezone": default_timezone,
        "messages": [
            {
                "batch_index": index,
                **source_message_for_model(message, body_limit=body_limit),
            }
            for index, message in enumerate(messages)
        ],
    }
    payload = {
        "model": extraction.get("openai_model", "gpt-5.5"),
        "input": [
            {"role": "system", "content": event_extraction_system_prompt()},
            {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "event_extraction_batch",
                "strict": True,
                "schema": BATCH_EVENT_SCHEMA,
            }
        },
    }
    result = openai_request_json(
        "POST",
        openai_responses_url(config),
        config,
        token=api_key,
        payload=payload,
        timeout=int(extraction.get("openai_timeout_seconds", 90)),
    )
    text = collect_openai_text(result)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentError(f"OpenAI did not return valid JSON: {text}") from exc
    events = parsed.get("events") if isinstance(parsed, dict) else None
    if not isinstance(events, list):
        raise AgentError(f"OpenAI batch response did not contain events array: {text}")
    if len(events) != len(messages):
        raise AgentError(f"OpenAI returned {len(events)} events for {len(messages)} messages.")
    return [normalize_event(event, message, config) for event, message in zip(events, messages)]


def event_extraction_system_prompt() -> str:
    return (
        "You extract calendar events from university and school emails. "
        "Return only the requested JSON schema. Prefer explicit facts from the email. "
        "For batch input, return exactly one event object per input message in the same "
        "order as the input messages. Use Chinese for all user-facing fields whenever "
        "possible, including title, description, location, organizer, and reason. "
        "Translate English event titles into concise natural Chinese while preserving "
        "proper nouns, organization names, room names, and official program names when "
        "translation would be lossy. The title should be short and calendar-friendly in "
        "Chinese. The description should be a concise Chinese summary; do not copy the "
        "entire original email there because the system attaches the original email "
        "separately. If the email is not an event invitation, seminar, workshop, "
        "deadline, or activity, set is_event=false. Use ISO 8601 datetime strings. If a "
        "date or time is ambiguous, set requires_review=true and explain the ambiguity "
        "in Chinese. Do not invent missing locations, organizers, or times."
    )


def openai_request_json(
    method: str,
    url: str,
    config: dict[str, Any],
    *,
    token: str | None = None,
    payload: Any | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    extraction = config.get("extraction", {})
    attempts = max(1, int(extraction.get("openai_max_retries", 3)))
    delay = float(extraction.get("openai_retry_seconds", 2))
    last_error: AgentError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return http_json(method, url, token=token, payload=payload, timeout=timeout)
        except AgentError as exc:
            last_error = exc
            if attempt >= attempts or not is_retryable_openai_error(str(exc)):
                break
            print(
                f"OpenAI request failed on attempt {attempt}/{attempts}; retrying in {delay:.1f}s: {exc}",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise last_error or AgentError("OpenAI request failed.")


def is_retryable_openai_error(message: str) -> bool:
    retryable_fragments = [
        "HTTP 408",
        "HTTP 409",
        "HTTP 429",
        "HTTP 500",
        "HTTP 502",
        "HTTP 503",
        "HTTP 504",
        "Network error",
        "connection reset",
        "timed out",
    ]
    lowered = message.lower()
    return any(fragment.lower() in lowered for fragment in retryable_fragments)


def extraction_error_event(message: SourceMessage, config: dict[str, Any], error: str) -> dict[str, Any]:
    event = {
        "is_event": False,
        "confidence": 0.0,
        "title": clean_title(message.subject),
        "start_time": "",
        "end_time": "",
        "timezone": config.get("extraction", {}).get("default_timezone", "Asia/Shanghai"),
        "location": "",
        "description": summarize_description(message),
        "organizer": message.sender,
        "requires_review": True,
        "reason": f"AI 提取失败，稍后会自动重试：{error}",
        "extraction_error": True,
    }
    return normalize_event(event, message, config)


def openai_responses_url(config: dict[str, Any]) -> str:
    extraction = config.get("extraction", {})
    explicit_url = (
        os.environ.get("OPENAI_RESPONSES_URL")
        or extraction.get("openai_responses_url")
    )
    if explicit_url:
        return explicit_url
    base_url = os.environ.get("OPENAI_BASE_URL") or extraction.get("openai_base_url")
    if base_url:
        return f"{base_url.rstrip('/')}/responses"
    return OPENAI_RESPONSES_URL


def collect_openai_text(result: dict[str, Any]) -> str:
    if isinstance(result.get("output_text"), str):
        return result["output_text"]
    chunks: list[str] = []
    for item in result.get("output", []):
        for content in item.get("content", []):
            if isinstance(content.get("text"), str):
                chunks.append(content["text"])
    if chunks:
        return "".join(chunks)
    raise AgentError(f"Could not find text in OpenAI response keys: {list(result.keys())}")


def heuristic_extract(message: SourceMessage, config: dict[str, Any], reason: str = "heuristic") -> dict[str, Any]:
    text = f"{message.subject}\n{message.body_text}"
    keywords = config.get("extraction", {}).get("event_keywords", [])
    if not keywords:
        keywords = [
            "event",
            "seminar",
            "talk",
            "lecture",
            "workshop",
            "conference",
            "webinar",
            "rsvp",
            "registration",
            "activity",
            "活动",
            "讲座",
            "研讨会",
            "报名",
            "会议",
            "工作坊",
            "论坛",
        ]
    lower = text.lower()
    has_keyword = any(keyword.lower() in lower for keyword in keywords)
    start_time = find_labeled_value(text, ["Start", "Starts", "开始", "时间"])
    end_time = find_labeled_value(text, ["End", "Ends", "结束"])
    location = find_labeled_value(text, ["Location", "Venue", "地点", "地址"])
    organizer = find_labeled_value(text, ["Organizer", "Host", "主办", "组织者"]) or message.sender
    date_guess = first_iso_datetime(text)
    if not start_time and date_guess:
        start_time = date_guess
    if start_time and not end_time:
        end_time = default_end_time(start_time)
    is_event = has_keyword or bool(start_time and location)
    confidence = 0.84 if is_event and start_time and end_time and location else 0.72 if is_event and start_time else 0.4 if is_event else 0.15
    event = {
        "is_event": is_event,
        "confidence": confidence,
        "title": clean_title(message.subject),
        "start_time": start_time,
        "end_time": end_time,
        "timezone": config.get("extraction", {}).get("default_timezone", "Asia/Shanghai"),
        "location": location,
        "description": summarize_description(message),
        "organizer": organizer,
        "requires_review": not (is_event and start_time and end_time and confidence >= 0.7),
        "reason": reason,
    }
    return normalize_event(event, message, config)


def find_labeled_value(text: str, labels: list[str]) -> str:
    for label in labels:
        pattern = rf"(?im)^\s*{re.escape(label)}\s*[:：]\s*(.+?)\s*$"
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return ""


def first_iso_datetime(text: str) -> str:
    match = re.search(
        r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:?\d{2}|Z)?\b",
        text,
    )
    if not match:
        return ""
    return match.group(0).replace("/", "-").replace("Z", "+00:00")


def default_end_time(start_time: str) -> str:
    try:
        return (parse_datetime(start_time) + timedelta(hours=1)).isoformat()
    except AgentError:
        return ""


def clean_title(subject: str) -> str:
    subject = re.sub(r"(?i)^\s*(re|fw|fwd)\s*:\s*", "", subject).strip()
    return subject or "未命名事件"


def summarize_description(message: SourceMessage, limit: int = 1200) -> str:
    parts = [
        f"来源：{message.source}",
        f"原始邮件 ID：{message.message_id}",
        f"发件人：{message.sender}",
    ]
    if message.web_link:
        parts.append(f"原始邮件链接：{message.web_link}")
    parts.append("")
    parts.append(message.body_text[:limit])
    return "\n".join(parts).strip()


def source_message_for_model(message: SourceMessage, body_limit: int = 12000) -> dict[str, str]:
    return {
        "source": message.source,
        "message_id": message.message_id,
        "subject": message.subject,
        "sender": message.sender,
        "received_at": message.received_at,
        "web_link": message.web_link,
        "body_text": message.body_text[:body_limit],
    }


def normalize_event(
    event: dict[str, Any], message: SourceMessage, config: dict[str, Any]
) -> dict[str, Any]:
    default_timezone = config.get("extraction", {}).get("default_timezone", "Asia/Shanghai")
    normalized = dict(event)
    normalized["confidence"] = max(0.0, min(1.0, float(normalized.get("confidence") or 0)))
    normalized["title"] = (normalized.get("title") or clean_title(message.subject)).strip()
    normalized["timezone"] = (normalized.get("timezone") or default_timezone).strip()
    normalized["description"] = (normalized.get("description") or summarize_description(message)).strip()
    normalized["source"] = message.source
    normalized["source_email_id"] = message.message_id
    normalized["source_subject"] = message.subject
    normalized["source_sender"] = message.sender
    normalized["source_received_at"] = message.received_at
    normalized["source_web_link"] = message.web_link
    normalized["source_body_text"] = message.body_text
    normalized["dedupe_key"] = event_dedupe_key(normalized)
    if is_cancellation_or_recall(message):
        normalized["requires_review"] = True
        normalized["reason"] = "原始邮件看起来是取消或撤回通知，不自动创建日历事件。"
    if normalized.get("is_event"):
        normalized["requires_review"] = bool(normalized.get("requires_review")) or not event_has_required_times(normalized)
    return normalized


def is_cancellation_or_recall(message: SourceMessage) -> bool:
    text = f"{message.subject}\n{message.body_text[:1000]}".lower()
    keywords = [
        "canceled event:",
        "cancelled event:",
        "event canceled",
        "event cancelled",
        "recall:",
        "已取消",
        "取消通知",
        "撤回",
    ]
    return any(keyword in text for keyword in keywords)


def event_dedupe_key(event: dict[str, Any]) -> str:
    raw = "|".join(
        [
            str(event.get("source", "")),
            str(event.get("source_email_id", "")),
            str(event.get("title", "")),
            str(event.get("start_time", "")),
            str(event.get("end_time", "")),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def event_has_required_times(event: dict[str, Any]) -> bool:
    return bool(event.get("title") and event.get("start_time") and event.get("end_time"))


def parse_datetime(value: str, default_timezone: str = "UTC") -> datetime:
    if not value:
        raise AgentError("Missing datetime value.")
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise AgentError(f"Expected ISO 8601 datetime, got: {value}") from exc
    if parsed.tzinfo is None:
        try:
            zone = ZoneInfo(default_timezone)
        except ZoneInfoNotFoundError:
            zone = UTC
        parsed = parsed.replace(tzinfo=zone)
    return parsed


def calendar_body_html(event: dict[str, Any], config: dict[str, Any]) -> str:
    calendar = config.get("calendar", {})
    include_source = bool(calendar.get("include_source_email_body", True))
    body_limit = int(calendar.get("source_body_limit", 12000))
    parts: list[str] = []

    description = (event.get("description") or "").strip()
    if description:
        parts.append("<h3>活动摘要</h3>")
        parts.append(f"<p>{html_lines(description)}</p>")

    metadata = [
        ("原始邮件主题", event.get("source_subject", "")),
        ("发件人", event.get("source_sender", "")),
        ("收信时间", event.get("source_received_at", "")),
    ]
    parts.append("<h3>来源邮件</h3>")
    parts.append("<ul>")
    for label, value in metadata:
        if value:
            parts.append(f"<li><strong>{html.escape(label)}：</strong>{html_lines(str(value))}</li>")
    source_link = event.get("source_web_link", "")
    if source_link:
        safe_link = html.escape(str(source_link), quote=True)
        parts.append(f'<li><strong>原始邮件链接：</strong><a href="{safe_link}">{safe_link}</a></li>')
    parts.append("</ul>")

    if include_source:
        source_body = (event.get("source_body_text") or "").strip()
        if source_body:
            if len(source_body) > body_limit:
                source_body = source_body[:body_limit].rstrip() + "\n\n[原始邮件内容过长，已截断]"
            parts.append("<h3>原始邮件内容</h3>")
            parts.append(f"<pre>{html.escape(source_body)}</pre>")

    return "\n".join(parts).strip()


def html_lines(value: str) -> str:
    return "<br>".join(html.escape(value).splitlines())


def calendar_body_text(event: dict[str, Any], config: dict[str, Any]) -> str:
    calendar = config.get("calendar", {})
    include_source = bool(calendar.get("include_source_email_body", True))
    body_limit = int(calendar.get("source_body_limit", 12000))
    parts: list[str] = []
    if event.get("description"):
        parts.extend(["活动摘要", str(event["description"]).strip(), ""])
    parts.extend(
        [
            "来源邮件",
            f"原始邮件主题：{event.get('source_subject', '')}",
            f"发件人：{event.get('source_sender', '')}",
            f"收信时间：{event.get('source_received_at', '')}",
        ]
    )
    if event.get("source_web_link"):
        parts.append(f"原始邮件链接：{event['source_web_link']}")
    if include_source and event.get("source_body_text"):
        source_body = str(event["source_body_text"]).strip()
        if len(source_body) > body_limit:
            source_body = source_body[:body_limit].rstrip() + "\n\n[原始邮件内容过长，已截断]"
        parts.extend(["", "原始邮件内容", source_body])
    return "\n".join(part for part in parts if part is not None).strip()


def calendar_payload_google(event: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    timezone = event.get("timezone") or config.get("extraction", {}).get("default_timezone", "Asia/Shanghai")
    payload = {
        "summary": event["title"],
        "description": calendar_body_text(event, config),
        "location": event.get("location", ""),
        "start": google_time_object(event["start_time"], timezone),
        "end": google_time_object(event["end_time"], timezone),
        "extendedProperties": {
            "private": {
                "source": event.get("source", ""),
                "sourceEmailId": event.get("source_email_id", ""),
                "dedupeKey": event.get("dedupe_key", ""),
            }
        },
    }
    if event.get("source_web_link"):
        payload["source"] = {
            "title": event.get("source_subject", event["title"]),
            "url": event["source_web_link"],
        }
    return payload


def google_time_object(value: str, timezone: str) -> dict[str, str]:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return {"date": value.strip()}
    return {"dateTime": ensure_rfc3339(value, timezone), "timeZone": timezone}


def ensure_rfc3339(value: str, timezone: str) -> str:
    parsed = parse_datetime(value, timezone)
    try:
        parsed = parsed.astimezone(ZoneInfo(timezone))
    except ZoneInfoNotFoundError:
        pass
    return parsed.isoformat()


def calendar_payload_outlook(event: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    timezone = event.get("timezone") or config.get("extraction", {}).get("default_timezone", "Asia/Shanghai")
    windows_timezone = config.get("microsoft", {}).get("calendar_timezone") or WINDOWS_TIMEZONE_BY_IANA.get(
        timezone, "UTC"
    )
    start = parse_datetime(event["start_time"], timezone)
    end = parse_datetime(event["end_time"], timezone)
    try:
        zone = ZoneInfo(timezone)
        start = start.astimezone(zone)
        end = end.astimezone(zone)
    except ZoneInfoNotFoundError:
        pass
    return {
        "subject": event["title"],
        "body": {"contentType": "HTML", "content": calendar_body_html(event, config)},
        "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": windows_timezone},
        "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": windows_timezone},
        "location": {"displayName": event.get("location", "")},
        "transactionId": deterministic_uuid(event.get("dedupe_key", "")),
        "isReminderOn": True,
        "reminderMinutesBeforeStart": int(config.get("calendar", {}).get("reminder_minutes", 30)),
    }


def deterministic_uuid(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return str(uuid.UUID(bytes=digest[:16]))


def write_google_event(event: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    token = refresh_google_token(config)
    calendar_id = config.get("google", {}).get("calendar_id", "primary")
    encoded_calendar = urllib.parse.quote(calendar_id, safe="")
    send_updates = config.get("google", {}).get("send_updates", "none")
    payload = calendar_payload_google(event, config)
    url = f"{GOOGLE_CALENDAR_BASE}/calendars/{encoded_calendar}/events?sendUpdates={urllib.parse.quote(send_updates)}"
    return http_json("POST", url, token=token, payload=payload)


def write_outlook_event(event: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    token = refresh_microsoft_token(config)
    calendar_id = config.get("microsoft", {}).get("calendar_id", "").strip()
    payload = calendar_payload_outlook(event, config)
    if calendar_id:
        encoded_calendar = urllib.parse.quote(calendar_id, safe="")
        url = f"{MICROSOFT_GRAPH_BASE}/me/calendars/{encoded_calendar}/events"
    else:
        url = f"{MICROSOFT_GRAPH_BASE}/me/calendar/events"
    return http_json("POST", url, token=token, payload=payload)


def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_events (
          source TEXT NOT NULL,
          source_email_id TEXT NOT NULL,
          dedupe_key TEXT NOT NULL,
          status TEXT NOT NULL,
          sink TEXT NOT NULL,
          remote_event_id TEXT,
          title TEXT,
          start_time TEXT,
          created_at TEXT NOT NULL,
          extraction_json TEXT NOT NULL,
          PRIMARY KEY (source, source_email_id, sink)
        )
        """
    )
    return conn


def was_seen(conn: sqlite3.Connection, source: str, source_email_id: str, sink: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM processed_events
        WHERE source = ? AND source_email_id = ? AND sink = ?
        """,
        (source, source_email_id, sink),
    ).fetchone()
    return row is not None


def was_created(conn: sqlite3.Connection, event: dict[str, Any], sink: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM processed_events
        WHERE source = ? AND source_email_id = ? AND sink = ? AND status = 'created'
        """,
        (event.get("source", ""), event.get("source_email_id", ""), sink),
    ).fetchone()
    return row is not None


def message_placeholder_event(message: SourceMessage, config: dict[str, Any]) -> dict[str, Any]:
    event = {
        "is_event": False,
        "confidence": 0.0,
        "title": clean_title(message.subject),
        "start_time": "",
        "end_time": "",
        "timezone": config.get("extraction", {}).get("default_timezone", "Asia/Shanghai"),
        "location": "",
        "description": summarize_description(message),
        "organizer": message.sender,
        "requires_review": True,
        "reason": "not processed yet",
    }
    return normalize_event(event, message, config)


def ignored_event(message: SourceMessage, config: dict[str, Any], reason: str) -> dict[str, Any]:
    event = {
        "is_event": False,
        "confidence": 1.0,
        "title": clean_title(message.subject),
        "start_time": "",
        "end_time": "",
        "timezone": config.get("extraction", {}).get("default_timezone", "Asia/Shanghai"),
        "location": "",
        "description": summarize_description(message),
        "organizer": message.sender,
        "requires_review": False,
        "reason": reason,
    }
    return normalize_event(event, message, config)


def should_ignore_without_ai(message: SourceMessage) -> tuple[bool, str]:
    subject = message.subject.strip().lower()
    if "daily event alert" in subject:
        return True, "Daily Event Alert 是活动汇总邮件，按规则直接忽略。"
    return False, ""


def record_event(
    conn: sqlite3.Connection,
    event: dict[str, Any],
    sink: str,
    status: str,
    remote_event_id: str = "",
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO processed_events (
          source, source_email_id, dedupe_key, status, sink, remote_event_id,
          title, start_time, created_at, extraction_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.get("source", ""),
            event.get("source_email_id", ""),
            event.get("dedupe_key", ""),
            status,
            sink,
            remote_event_id,
            event.get("title", ""),
            event.get("start_time", ""),
            utc_now().isoformat(),
            json.dumps(event, ensure_ascii=False),
        ),
    )
    conn.commit()


def notification_config(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("notifications", {}) or {}


def notifications_enabled(config: dict[str, Any]) -> bool:
    return bool(notification_config(config).get("enabled", False))


def notification_target(config: dict[str, Any], override: str | None = None) -> str:
    value = (override or notification_config(config).get("notify_target") or "webhook").strip()
    return value or "webhook"


def notification_webhook_url(config: dict[str, Any]) -> str:
    notify = notification_config(config)
    env_name = notify.get("webhook_url_env", "NOTIFY_WEBHOOK_URL")
    return (os.environ.get(env_name, "") or notify.get("webhook_url", "")).strip()


def hermes_webhook_url(config: dict[str, Any]) -> str:
    notify = notification_config(config)
    env_name = notify.get("hermes_webhook_url_env", "HERMES_WEBHOOK_URL")
    return (os.environ.get(env_name, "") or notify.get("hermes_webhook_url", "")).strip()


def hermes_webhook_secret(config: dict[str, Any]) -> str:
    notify = notification_config(config)
    env_name = notify.get("hermes_webhook_secret_env", "HERMES_WEBHOOK_SECRET")
    return (os.environ.get(env_name, "") or notify.get("hermes_webhook_secret", "")).strip()


def notification_headers(config: dict[str, Any]) -> dict[str, str]:
    notify = notification_config(config)
    headers: dict[str, str] = {}
    token_env = notify.get("webhook_token_env", "NOTIFY_WEBHOOK_TOKEN")
    token = (os.environ.get(token_env, "") or notify.get("webhook_token", "")).strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def notification_envelope(
    *,
    event_type: str,
    title: str,
    markdown: str,
    severity: str,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "source": "outlook_event_automation",
        "event_type": event_type,
        "type": event_type,
        "severity": severity,
        "title": title,
        "markdown": markdown,
        "text": strip_markdown(markdown),
        "created_at": utc_now().isoformat(),
        "payload": payload or {},
    }


def send_notification(
    config: dict[str, Any],
    *,
    event_type: str,
    title: str,
    markdown: str,
    severity: str = "info",
    payload: dict[str, Any] | None = None,
    force: bool = False,
    target: str | None = None,
) -> bool:
    if not force and not notifications_enabled(config):
        return False
    notify = notification_config(config)
    selected = notification_target(config, target)
    envelope = notification_envelope(
        event_type=event_type,
        title=title,
        markdown=markdown,
        severity=severity,
        payload=payload,
    )
    if selected == "webhook":
        provider = notify.get("provider", "webhook")
        if provider != "webhook":
            raise AgentError(f"Unsupported notification provider: {provider}")
        url = notification_webhook_url(config)
        if not url:
            raise AgentError(
                "Notification webhook URL is missing. Set NOTIFY_WEBHOOK_URL or "
                "notifications.webhook_url."
            )
        status, _ = http_post_raw(
            url,
            payload=envelope,
            headers=notification_headers(config),
            timeout=int(notify.get("webhook_timeout_seconds", 30)),
        )
    elif selected == "hermes-webhook":
        status, _ = send_hermes_webhook(config, envelope)
    else:
        raise AgentError(f"Unsupported notify target: {selected}")
    print(f"Notification sent: target={selected} type={event_type} status={status}")
    return True


def send_hermes_webhook(config: dict[str, Any], envelope: dict[str, Any]) -> tuple[int, str]:
    notify = notification_config(config)
    url = hermes_webhook_url(config)
    if not url:
        raise AgentError(
            "Hermes webhook URL is missing. Set HERMES_WEBHOOK_URL or "
            "notifications.hermes_webhook_url."
        )
    secret = hermes_webhook_secret(config)
    if not secret:
        raise AgentError(
            "Hermes webhook secret is missing. Set HERMES_WEBHOOK_SECRET or "
            "notifications.hermes_webhook_secret."
        )
    body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    request_id = deterministic_uuid(
        "|".join(
            [
                str(envelope.get("source", "")),
                str(envelope.get("event_type", "")),
                str(envelope.get("created_at", "")),
                str(envelope.get("title", "")),
            ]
        )
    )
    headers = {
        "X-Webhook-Signature": signature,
        "X-Request-ID": request_id,
    }
    return http_post_json_bytes(
        url,
        body=body,
        headers=headers,
        timeout=int(notify.get("webhook_timeout_seconds", 30)),
    )


def strip_markdown(value: str) -> str:
    text = re.sub(r"`([^`]+)`", r"\1", value)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    return text.strip()


def load_notification_state() -> dict[str, Any]:
    if not NOTIFICATION_STATE_PATH.exists():
        return {}
    try:
        return load_json(NOTIFICATION_STATE_PATH)
    except Exception:
        return {}


def save_notification_state(state: dict[str, Any]) -> None:
    save_json(NOTIFICATION_STATE_PATH, state)


def notification_cooldown_elapsed(event_type: str, cooldown_minutes: int) -> bool:
    if cooldown_minutes <= 0:
        return True
    state = load_notification_state()
    raw = state.get("last_sent", {}).get(event_type, "")
    if not raw:
        return True
    try:
        last_sent = parse_datetime(raw)
    except AgentError:
        return True
    return utc_now() - last_sent >= timedelta(minutes=cooldown_minutes)


def mark_notification_sent(event_type: str) -> None:
    state = load_notification_state()
    last_sent = dict(state.get("last_sent", {}))
    last_sent[event_type] = utc_now().isoformat()
    state["last_sent"] = last_sent
    save_notification_state(state)


def notify_fault(config: dict[str, Any], message: str, context: dict[str, Any]) -> None:
    if not notifications_enabled(config):
        return
    notify = notification_config(config)
    cooldown = int(notify.get("fault_cooldown_minutes", 30))
    if not notification_cooldown_elapsed("fault", cooldown):
        return
    markdown = "\n".join(
        [
            "## 邮件日历服务故障",
            "",
            f"- 时间：`{utc_now().isoformat()}`",
            f"- 组件：`outlook_event_automation`",
            f"- 错误：{message}",
            "",
            "服务循环会继续保活并在下一轮自动重试。",
        ]
    )
    try:
        sent = send_notification(
            config,
            event_type="fault",
            title="邮件日历服务故障",
            markdown=markdown,
            severity="error",
            payload={"context": context, "error": message},
        )
        if sent:
            mark_notification_sent("fault")
    except Exception as exc:
        print(f"[{utc_now().isoformat()}] ERROR: failed to send fault notification: {exc}", file=sys.stderr)


def query_processed_events(
    hours: int,
    limit: int,
    status: str | None = None,
) -> list[dict[str, Any]]:
    since = utc_now() - timedelta(hours=hours)
    conn = db()
    where = "WHERE created_at >= ?"
    params: list[Any] = [since.isoformat()]
    if status:
        where += " AND status = ?"
        params.append(status)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT source, source_email_id, status, sink, remote_event_id, title,
               start_time, created_at, extraction_json
        FROM processed_events
        {where}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            extraction = json.loads(row["extraction_json"])
        except Exception:
            extraction = {}
        events.append(
            {
                "source": row["source"],
                "source_email_id": row["source_email_id"],
                "status": row["status"],
                "sink": row["sink"],
                "remote_event_id": row["remote_event_id"],
                "title": row["title"] or extraction.get("title", ""),
                "start_time": row["start_time"] or extraction.get("start_time", ""),
                "location": extraction.get("location", ""),
                "reason": extraction.get("reason", ""),
                "source_subject": extraction.get("source_subject", ""),
                "created_at": row["created_at"],
            }
        )
    return events


def build_daily_digest(config: dict[str, Any], hours: int, limit: int) -> dict[str, Any]:
    all_events = query_processed_events(hours, max(limit, 1000))
    counts = Counter(event["status"] for event in all_events)
    created = [event for event in all_events if event["status"] == "created"]
    review = [event for event in all_events if event["status"] == "needs_review"]
    ignored = [event for event in all_events if event["status"] == "ignored"]
    lines = [
        "## 活动邮件同步日报",
        "",
        f"- 时间窗：过去 {hours} 小时",
        f"- 新增日历事件：{len(created)}",
        f"- 待复核邮件：{len(review)}",
        f"- 已忽略邮件：{len(ignored)}",
    ]
    if created:
        lines.extend(["", "### 新增活动"])
        for item in created[:limit]:
            when = compact_datetime(item.get("start_time", ""))
            location = item.get("location") or "地点待确认"
            lines.append(f"- `{when}` {item.get('title', '未命名事件')}｜{location}")
    else:
        lines.extend(["", "过去这段时间没有新增可写入日历的活动。"])
    if review:
        lines.extend(["", "### 待复核"])
        for item in review[:5]:
            subject = item.get("source_subject") or item.get("title") or "未命名邮件"
            reason = item.get("reason") or "需要人工判断"
            lines.append(f"- {subject}：{reason}")
    return {
        "title": "活动邮件同步日报",
        "markdown": "\n".join(lines),
        "payload": {
            "hours": hours,
            "counts": dict(counts),
            "events": all_events[:limit],
            "total_events": len(all_events),
        },
    }


def compact_datetime(value: str) -> str:
    if not value:
        return "时间待确认"
    try:
        parsed = parse_datetime(value)
    except AgentError:
        return value
    return parsed.strftime("%m-%d %H:%M")


def notify_digest(args: argparse.Namespace, config: dict[str, Any]) -> None:
    notify = notification_config(config)
    hours = args.hours or int(notify.get("daily_digest_hours", 24))
    limit = args.limit or int(notify.get("daily_digest_limit", 20))
    digest = build_daily_digest(config, hours, limit)
    print(digest["markdown"])
    if args.dry_run:
        return
    sent = send_notification(
        config,
        event_type="daily_digest",
        title=digest["title"],
        markdown=digest["markdown"],
        severity="info",
        payload=digest["payload"],
        target=getattr(args, "notify_target", None),
    )
    if not sent:
        print("Notifications are disabled. Set notifications.enabled=true to send.")


def build_health_report(config: dict[str, Any], max_age_minutes: int) -> dict[str, Any]:
    issues: list[str] = []
    last_run: dict[str, Any] = {}
    path = DATA_DIR / "last_run.json"
    if not path.exists():
        issues.append("没有找到 last_run.json，服务可能还没有成功跑过。")
    else:
        try:
            last_run = load_json(path)
            ran_at = parse_datetime(str(last_run.get("ran_at", "")))
            age_minutes = int((utc_now() - ran_at).total_seconds() // 60)
            if age_minutes > max_age_minutes:
                issues.append(f"最近一次成功运行距今 {age_minutes} 分钟，超过阈值 {max_age_minutes} 分钟。")
        except Exception as exc:
            issues.append(f"无法读取最近运行状态：{exc}")
    results = last_run.get("results", []) if isinstance(last_run, dict) else []
    actions = Counter(str(item.get("action", "unknown")) for item in results if isinstance(item, dict))
    if actions.get("extraction_failed", 0):
        issues.append(f"最近一次运行有 {actions['extraction_failed']} 封邮件 AI 抽取失败。")
    severity = "error" if issues else "ok"
    lines = [
        "## 邮件日历服务健康报告",
        "",
        f"- 时间：`{utc_now().isoformat()}`",
        f"- 状态：{'异常' if issues else '正常'}",
        f"- 最近动作统计：{dict(actions)}",
    ]
    if issues:
        lines.extend(["", "### 问题"])
        lines.extend(f"- {issue}" for issue in issues)
    else:
        lines.extend(["", "服务最近一次运行状态正常。"])
    return {
        "severity": severity,
        "title": "邮件日历服务健康报告",
        "markdown": "\n".join(lines),
        "payload": {"issues": issues, "actions": dict(actions), "last_run": last_run},
    }


def health_report(args: argparse.Namespace, config: dict[str, Any]) -> None:
    notify = notification_config(config)
    max_age = args.max_last_run_age_minutes or int(
        notify.get("health_max_last_run_age_minutes", 120)
    )
    report = build_health_report(config, max_age)
    print(report["markdown"])
    should_send = args.always or report["severity"] != "ok"
    if args.dry_run or not should_send:
        return
    sent = send_notification(
        config,
        event_type="health_report",
        title=report["title"],
        markdown=report["markdown"],
        severity=report["severity"],
        payload=report["payload"],
        target=getattr(args, "notify_target", None),
    )
    if not sent:
        print("Notifications are disabled. Set notifications.enabled=true to send.")


def api_config(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("api", {}) or {}


def api_token(config: dict[str, Any]) -> str:
    api = api_config(config)
    env_name = api.get("token_env", "OUTLOOK_AGENT_API_TOKEN")
    return (os.environ.get(env_name, "") or api.get("token", "")).strip()


def is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def parse_query(path: str) -> tuple[str, dict[str, str]]:
    parsed = urllib.parse.urlparse(path)
    values = urllib.parse.parse_qs(parsed.query)
    query = {key: items[-1] for key, items in values.items() if items}
    return parsed.path, query


def query_int(
    values: dict[str, str],
    key: str,
    default: int,
    minimum: int = 1,
    maximum: int = 1000,
) -> int:
    raw = values.get(key, "")
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise AgentError(f"Expected integer query parameter `{key}`, got {raw!r}.") from exc
    return max(minimum, min(maximum, parsed))


def query_bool(values: dict[str, str], key: str, default: bool = False) -> bool:
    raw = values.get(key, "")
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def json_response(handler: http.server.BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def check_api_auth(handler: http.server.BaseHTTPRequestHandler, config: dict[str, Any]) -> bool:
    expected = api_token(config)
    if not expected:
        return True
    auth = handler.headers.get("Authorization", "")
    header_token = handler.headers.get("X-Outlook-Agent-Token", "")
    bearer = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    return hmac.compare_digest(expected, bearer or header_token)


def last_run_summary() -> dict[str, Any]:
    path = DATA_DIR / "last_run.json"
    if not path.exists():
        return {"exists": False, "path": str(path)}
    value = load_json(path)
    return {"exists": True, "path": str(path), **value}


def api_routes() -> dict[str, Any]:
    return {
        "service": "outlook_event_automation",
        "routes": {
            "GET /": "List routes",
            "GET /health": "Service health report JSON",
            "GET /digest?hours=24&limit=20": "Activity digest JSON and markdown",
            "GET /events?status=created&hours=24&limit=20": "Processed events from SQLite",
            "GET /review?hours=24&limit=20": "needs_review events",
            "GET /last-run": "Last pipeline run result",
            "POST /scan?source=outlook&limit=20": "Run a dry scan; write=true requires api.allow_write_actions=true",
        },
    }


def handle_api_get(path: str, query: dict[str, str], config: dict[str, Any]) -> tuple[int, Any]:
    api = api_config(config)
    default_hours = int(api.get("default_hours", 24))
    default_limit = int(api.get("default_limit", 20))
    if path == "/":
        return 200, api_routes()
    if path == "/health":
        max_age = query_int(
            query,
            "max_last_run_age_minutes",
            int(api.get("health_max_last_run_age_minutes", 120)),
            maximum=10080,
        )
        return 200, build_health_report(config, max_age)
    if path == "/digest":
        hours = query_int(query, "hours", default_hours, maximum=24 * 365)
        limit = query_int(query, "limit", default_limit, maximum=200)
        return 200, build_daily_digest(config, hours, limit)
    if path == "/events":
        hours = query_int(query, "hours", default_hours, maximum=24 * 365)
        limit = query_int(query, "limit", default_limit, maximum=500)
        status = query.get("status") or None
        return 200, {
            "events": query_processed_events(hours, limit, status),
            "hours": hours,
            "status": status,
        }
    if path == "/review":
        hours = query_int(query, "hours", default_hours, maximum=24 * 365)
        limit = query_int(query, "limit", default_limit, maximum=200)
        return 200, {
            "events": query_processed_events(hours, limit, "needs_review"),
            "hours": hours,
            "status": "needs_review",
        }
    if path == "/last-run":
        return 200, last_run_summary()
    return 404, {"error": "not_found", "routes": api_routes()["routes"]}


def handle_api_post(path: str, query: dict[str, str], config: dict[str, Any]) -> tuple[int, Any]:
    if path != "/scan":
        return 404, {"error": "not_found", "routes": api_routes()["routes"]}
    api = api_config(config)
    write = query_bool(query, "write", False)
    if write and not bool(api.get("allow_write_actions", False)):
        return 403, {
            "error": "write_not_allowed",
            "message": "Set api.allow_write_actions=true before using POST /scan?write=true.",
        }
    source = query.get("source") or config.get("source", "outlook")
    sink = query.get("sink") or ("none" if not write else config.get("calendar", {}).get("sink", "outlook"))
    if source not in {"gmail", "outlook", "fixture"}:
        return 400, {"error": "invalid_source", "allowed": ["gmail", "outlook", "fixture"]}
    if sink not in {"google", "outlook", "none"}:
        return 400, {"error": "invalid_sink", "allowed": ["google", "outlook", "none"]}
    limit = query_int(query, "limit", int(api.get("default_limit", 20)), maximum=200)
    force = query_bool(query, "force", False)
    batch_size = query_int(query, "batch_size", 0, minimum=0, maximum=100) or None
    run_args = argparse.Namespace(
        source=source,
        fixture=query.get("fixture"),
        sink=sink,
        limit=limit,
        write=write,
        force=force,
        batch_size=batch_size,
    )
    run_pipeline(run_args, config)
    return 200, {
        "ok": True,
        "source": source,
        "sink": sink,
        "write": write,
        "last_run": last_run_summary(),
    }


def api_server(args: argparse.Namespace, config: dict[str, Any]) -> None:
    api = api_config(config)
    host = args.host or api.get("host", "127.0.0.1")
    port = int(args.port or api.get("port", 8791))
    token = api_token(config)
    if not is_loopback_host(host) and not token:
        raise AgentError(
            "Refusing to bind API server on a non-loopback host without OUTLOOK_AGENT_API_TOKEN."
        )

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            self.handle_request("GET")

        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
            self.handle_request("POST")

        def handle_request(self, method: str) -> None:
            try:
                if not check_api_auth(self, config):
                    json_response(self, 401, {"error": "unauthorized"})
                    return
                path, query = parse_query(self.path)
                if method == "GET":
                    status, payload = handle_api_get(path, query, config)
                else:
                    status, payload = handle_api_post(path, query, config)
                json_response(self, status, payload)
            except Exception as exc:
                json_response(self, 500, {"error": "internal_error", "message": str(exc)})

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[{utc_now().isoformat()}] API {self.address_string()} {fmt % args}")

    print(f"Starting API server on http://{host}:{port}")
    if token:
        print("API token authentication is enabled.")
    else:
        print("API token authentication is disabled; loopback binding only.")
    http.server.ThreadingHTTPServer((host, port), Handler).serve_forever()


def run_pipeline(args: argparse.Namespace, config: dict[str, Any]) -> None:
    if getattr(args, "batch_size", None):
        config.setdefault("extraction", {})["batch_size"] = args.batch_size
    source = args.source or config.get("source", "fixture")
    if source == "outlook":
        messages = fetch_outlook_messages(config, args.limit)
    elif source == "gmail":
        messages = fetch_gmail_messages(config, args.limit)
    elif source == "fixture":
        fixture = Path(args.fixture or BASE_DIR / "fixtures" / "sample_event_email.json")
        messages = load_fixture_messages(fixture)
    else:
        raise AgentError(f"Unknown source: {source}")

    sink = args.sink or config.get("calendar", {}).get("sink", "google")
    threshold = float(config.get("extraction", {}).get("confidence_threshold", 0.75))
    write = bool(args.write)
    conn = db()
    results: list[dict[str, Any] | None] = []
    pending: list[tuple[int, SourceMessage]] = []

    for message in messages:
        if not args.force and was_seen(conn, message.source, message.message_id, sink):
            placeholder = message_placeholder_event(message, config)
            result = {
                "message_id": message.message_id,
                "subject": message.subject,
                "event": placeholder,
                "eligible": False,
                "action": "skipped_seen",
            }
            results.append(result)
            print_result(result)
            continue
        ignore, ignore_reason = should_ignore_without_ai(message)
        if ignore:
            event = ignored_event(message, config, ignore_reason)
            result = {
                "message_id": message.message_id,
                "subject": message.subject,
                "event": event,
                "eligible": False,
                "action": "ignored",
            }
            if write:
                record_event(conn, event, sink, "ignored")
            results.append(result)
            print_result(result)
            continue
        pending.append((len(results), message))
        results.append(None)

    if pending:
        events = extract_events([message for _, message in pending], config)
    else:
        events = []

    for (result_index, message), event in zip(pending, events):
        eligible = bool(
            event.get("is_event")
            and float(event.get("confidence", 0)) >= threshold
            and event_has_required_times(event)
            and not event.get("requires_review")
        )
        result = {
            "message_id": message.message_id,
            "subject": message.subject,
            "event": event,
            "eligible": eligible,
            "action": "review",
        }
        if event.get("extraction_error"):
            result["action"] = "extraction_failed"
        elif eligible and was_created(conn, event, sink) and not args.force:
            result["action"] = "skipped_duplicate"
        elif eligible and write and sink != "none":
            remote = write_event(event, config, sink)
            remote_id = remote.get("id") or remote.get("iCalUId") or ""
            record_event(conn, event, sink, "created", remote_id)
            result["action"] = "created"
            result["remote_event_id"] = remote_id
            result["remote_event_link"] = remote.get("htmlLink") or remote.get("webLink") or ""
        elif eligible:
            result["action"] = "dry_run"
        elif not event.get("is_event"):
            result["action"] = "ignored"
            if write:
                record_event(conn, event, sink, "ignored")
        else:
            result["action"] = "needs_review"
            if write:
                record_event(conn, event, sink, "needs_review")
        results[result_index] = result
        print_result(result)

    save_json(DATA_DIR / "last_run.json", {"ran_at": utc_now().isoformat(), "results": [r for r in results if r]})
    print(f"\nSaved run details: {DATA_DIR / 'last_run.json'}")


def service_loop(args: argparse.Namespace, config: dict[str, Any]) -> None:
    interval = args.interval or int(config.get("service", {}).get("poll_seconds", 30))
    source = args.source or config.get("source", "gmail")
    sink = args.sink or config.get("calendar", {}).get("sink", "google")
    limit = args.limit or int(config.get("service", {}).get("max_messages_per_poll", 10))
    if not args.write:
        raise AgentError("Service mode is intended for production writes. Pass --write explicitly.")
    print(
        f"Starting service loop: source={source} sink={sink} "
        f"interval={interval}s limit={limit}"
    )
    while True:
        started = utc_now()
        try:
            run_args = argparse.Namespace(
                source=source,
                fixture=args.fixture,
                sink=sink,
                limit=limit,
                write=True,
                force=False,
                batch_size=getattr(args, "batch_size", None),
            )
            run_pipeline(run_args, config)
        except Exception as exc:  # Keep the daemon alive across transient API failures.
            print(f"[{utc_now().isoformat()}] ERROR: {exc}", file=sys.stderr)
            notify_fault(
                config,
                str(exc),
                {"source": source, "sink": sink, "limit": limit, "interval": interval},
            )
        elapsed = (utc_now() - started).total_seconds()
        sleep_for = max(1, interval - int(elapsed))
        time.sleep(sleep_for)


def write_event(event: dict[str, Any], config: dict[str, Any], sink: str) -> dict[str, Any]:
    if sink == "google":
        return write_google_event(event, config)
    if sink == "outlook":
        return write_outlook_event(event, config)
    raise AgentError(f"Unknown sink: {sink}")


def print_result(result: dict[str, Any]) -> None:
    event = result["event"]
    print("\n---")
    print(f"Email: {result['subject']}")
    print(f"Action: {result['action']}")
    print(f"Event: {event.get('title')}")
    print(f"Confidence: {event.get('confidence')}")
    print(f"Start: {event.get('start_time')}")
    print(f"End: {event.get('end_time')}")
    print(f"Location: {event.get('location')}")
    if event.get("requires_review"):
        print(f"Review reason: {event.get('reason')}")
    if result.get("remote_event_link"):
        print(f"Calendar link: {result['remote_event_link']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Forwarded mail event automation")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create config.local.json from config.example.json")
    sub.add_parser("auth-microsoft", help="Authorize Microsoft Graph by device code")
    sub.add_parser("auth-microsoft-web", help="Authorize Microsoft Graph by browser callback")
    sub.add_parser("auth-google", help="Authorize Google Calendar by local browser OAuth")

    run = sub.add_parser("run", help="Process messages and optionally write calendar events")
    run.add_argument("--source", choices=["fixture", "gmail", "outlook"], help="Message source")
    run.add_argument("--fixture", help="Fixture JSON path for source=fixture")
    run.add_argument("--sink", choices=["google", "outlook", "none"], help="Calendar sink")
    run.add_argument("--limit", type=int, help="Maximum messages to inspect")
    run.add_argument("--write", action="store_true", help="Actually create calendar events")
    run.add_argument("--force", action="store_true", help="Ignore local duplicate records")
    run.add_argument("--batch-size", type=int, help="Messages per AI extraction batch")

    serve = sub.add_parser("serve", help="Run a permanent polling worker")
    serve.add_argument("--source", choices=["gmail", "outlook"], help="Message source")
    serve.add_argument("--fixture", help=argparse.SUPPRESS)
    serve.add_argument("--sink", choices=["google", "outlook"], help="Calendar sink")
    serve.add_argument("--limit", type=int, help="Maximum messages per poll")
    serve.add_argument("--interval", type=int, help="Polling interval in seconds")
    serve.add_argument("--write", action="store_true", help="Required: create calendar events")
    serve.add_argument("--batch-size", type=int, help="Messages per AI extraction batch")

    digest = sub.add_parser("notify-digest", help="Send or preview a daily activity digest")
    digest.add_argument("--hours", type=int, help="Lookback window in hours")
    digest.add_argument("--limit", type=int, help="Maximum processed rows to include")
    digest.add_argument("--dry-run", action="store_true", help="Print without sending webhook")
    digest.add_argument(
        "--notify-target",
        choices=["webhook", "hermes-webhook"],
        help="Notification target override",
    )

    health = sub.add_parser("health-report", help="Send or preview a service health report")
    health.add_argument("--max-last-run-age-minutes", type=int, help="Staleness threshold")
    health.add_argument("--always", action="store_true", help="Send even when healthy")
    health.add_argument("--dry-run", action="store_true", help="Print without sending webhook")
    health.add_argument(
        "--notify-target",
        choices=["webhook", "hermes-webhook"],
        help="Notification target override",
    )

    api = sub.add_parser("api-server", help="Run a lightweight HTTP API for agents")
    api.add_argument("--host", help="Bind host; defaults to api.host or 127.0.0.1")
    api.add_argument("--port", type=int, help="Bind port; defaults to api.port or 8791")

    sub.add_parser("show-config", help="Print the effective config")
    return parser


def main() -> int:
    load_env_file(BASE_DIR / ".env")
    parser = build_parser()
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    try:
        if args.command == "init":
            write_local_config(config_path)
            return 0
        config = load_config(config_path)
        if args.command == "auth-microsoft":
            auth_microsoft(config)
        elif args.command == "auth-microsoft-web":
            auth_microsoft_web(config)
        elif args.command == "auth-google":
            auth_google(config)
        elif args.command == "run":
            run_pipeline(args, config)
        elif args.command == "serve":
            service_loop(args, config)
        elif args.command == "notify-digest":
            notify_digest(args, config)
        elif args.command == "health-report":
            health_report(args, config)
        elif args.command == "api-server":
            api_server(args, config)
        elif args.command == "show-config":
            print(json.dumps(config, ensure_ascii=False, indent=2))
        else:
            parser.error(f"Unknown command: {args.command}")
        return 0
    except AgentError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

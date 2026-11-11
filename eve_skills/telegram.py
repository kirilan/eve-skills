"""A Telegram bot as an outbound channel: the bot's token and chat, and sending a message.

The token lives in `telegram.json` in the config directory, written owner-only (it is a password
for the bot). Nothing here ever puts the token in an error message or a log line: the API takes it
inside the URL, so every failure is reported from Telegram's own `description` instead of the
request. Standard library only, like the rest of the tool.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import paths, storage

API = "https://api.telegram.org"
CONFIG_NAME = "telegram.json"
# Telegram refuses a message over 4096 characters; lines are packed below that.
MAX_MESSAGE = 4000
TIMEOUT_SECONDS = 20


class TelegramError(RuntimeError):
    """Telegram refused or could not be reached; the text never contains the token."""


def config_path() -> str:
    return os.path.join(paths.config_dir(), CONFIG_NAME)


def load_config() -> dict:
    """`{"token": ..., "chat_id": ...}`, or an empty dict before `alerts setup`."""
    try:
        with open(config_path(), encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def save_config(doc: dict) -> None:
    storage.atomic_write_json(config_path(), doc, private=True)


def _call(token: str, method: str, payload: dict | None = None, opener=None) -> object:
    """One Bot API call: its `result`, or TelegramError with Telegram's own description."""
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(f"{API}/bot{token}/{method}", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with (opener or urllib.request.urlopen)(req, timeout=TIMEOUT_SECONDS) as res:
            doc = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        try:
            doc = json.loads(err.read().decode("utf-8"))
        except (ValueError, OSError):
            raise TelegramError(f"Telegram {method}: HTTP {err.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as err:
        reason = getattr(err, "reason", None) or type(err).__name__
        raise TelegramError(f"Telegram {method}: could not reach the API ({reason})") from None
    if not isinstance(doc, dict) or not doc.get("ok"):
        description = doc.get("description") if isinstance(doc, dict) else None
        raise TelegramError(f"Telegram {method}: {description or 'refused'}")
    return doc.get("result")


def bot_name(token: str, opener=None) -> str:
    """The bot's @username: proves the token is valid."""
    me = _call(token, "getMe", opener=opener) or {}
    return str(me.get("username") or me.get("first_name") or "bot")


def private_chats(token: str, opener=None) -> list[dict]:
    """Chats that messaged the bot recently, newest first: `{"id", "name"}`.

    A bot cannot open a conversation; the user sends it any message first, and that message is the
    only place the chat id appears."""
    updates = _call(token, "getUpdates", {"allowed_updates": ["message"]}, opener=opener) or []
    out: dict[int, dict] = {}
    for update in reversed(updates):
        chat = ((update or {}).get("message") or {}).get("chat") or {}
        if isinstance(chat.get("id"), int) and chat["id"] not in out:
            name = chat.get("username") or " ".join(
                p for p in (chat.get("first_name"), chat.get("last_name")) if p) or chat.get("title")
            out[chat["id"]] = {"id": chat["id"], "name": name, "type": chat.get("type")}
    return list(out.values())


def chunks(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Split on line boundaries so each part fits one message; a single huge line is cut."""
    parts, current = [], ""
    for line in text.splitlines():
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def send(token: str, chat_id: int, text: str, *, silent: bool = False, opener=None) -> int:
    """Send `text` (split when long) as plain text; returns the number of messages sent."""
    sent = 0
    for part in chunks(text):
        _call(token, "sendMessage", {"chat_id": chat_id, "text": part, "disable_notification": silent,
                                     "link_preview_options": {"is_disabled": True}}, opener=opener)
        sent += 1
    return sent

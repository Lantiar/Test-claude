"""Read a one-time verification code out of a just-arrived email.

Deliberately the narrowest thing that closes the loop. An ATS sends a 6-digit
code, the run needs that code, and nothing else about the mailbox is any of its
business. So:

  * the mailbox is opened **read-only** -- nothing is marked read, moved or deleted
  * a high-water mark is taken when the wait starts, and only messages arriving
    **after** it are ever examined; existing mail is invisible to this module
  * a message is only looked at if it matches a caller-supplied filter, so a wait
    for a Workday code cannot read a message from anyone else
  * only the extracted code is returned. Bodies, senders and subjects are never
    returned to the caller and never logged

Credentials come from the environment (an app password, not the account
password). Without them this raises MailUnavailable and the caller queues the
application for a human, which is the same thing it does for any other step it
cannot complete.
"""
from __future__ import annotations

import email
import imaplib
import os
import re
import time
from email.header import decode_header, make_header

# 4-8 digits, the shape every ATS uses. Bounded on both sides so a phone number
# or a year inside the mail body is not mistaken for the code.
CODE_RE = re.compile(r"\b(\d{4,8})\b")

# Phrases that appear next to the real code. A message often carries other
# numbers (a req id, a phone number, a year), so prefer a number that a code
# word points at before falling back to any number at all.
CODE_HINTS = (
    "verification code", "security code", "one-time", "one time passcode",
    "otp", "passcode", "confirmation code", "access code", "your code",
)


class MailUnavailable(RuntimeError):
    """No mailbox configured, or it would not let us in."""


def _connect() -> tuple[imaplib.IMAP4_SSL, str]:
    user = os.getenv("MAIL_USER") or os.getenv("IMAP_USER")
    password = os.getenv("MAIL_APP_PASSWORD") or os.getenv("IMAP_PASSWORD")
    host = os.getenv("IMAP_HOST", "imap.gmail.com")
    port = int(os.getenv("IMAP_PORT", "993"))
    if not user or not password:
        raise MailUnavailable(
            "no mailbox configured: set MAIL_USER and MAIL_APP_PASSWORD "
            "(an app password, never the account password)"
        )
    try:
        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(user, password)
    except Exception as exc:                      # noqa: BLE001 - surfaced as one reason
        raise MailUnavailable(f"{host}: {exc}") from exc
    return conn, os.getenv("IMAP_MAILBOX", "INBOX")


def _latest_uid(conn, mailbox: str) -> int:
    """Highest UID currently in the mailbox: the line we will not look behind."""
    conn.select(mailbox, readonly=True)
    ok, data = conn.uid("search", None, "ALL")
    if ok != "OK" or not data or not data[0]:
        return 0
    return max(int(u) for u in data[0].split())


def _header(msg, name: str) -> str:
    raw = msg.get(name, "")
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw or ""


def _body_text(msg) -> str:
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    parts.append(part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace"))
                except Exception:
                    continue
    else:
        try:
            parts.append(msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", "replace"))
        except Exception:
            pass
    return re.sub(r"<[^>]+>", " ", " ".join(parts))


def extract_code(text: str) -> str | None:
    """The code a code-word points at, else any lone 4-8 digit number."""
    low = text.lower()
    for hint in CODE_HINTS:
        at = low.find(hint)
        if at == -1:
            continue
        window = text[at:at + 200]
        if m := CODE_RE.search(window):
            return m.group(1)
    found = CODE_RE.findall(text)
    return found[0] if len(set(found)) == 1 else None


def wait_for_code(contains: list[str], timeout: int = 180,
                  poll: int = 10) -> str | None:
    """Wait for a NEW message matching `contains`, return its code.

    `contains` is matched case-insensitively against the sender and subject.
    It is required and must be non-empty: an unfiltered wait would hand back a
    code out of whatever unrelated mail happened to land first.
    """
    if not contains:
        raise ValueError("wait_for_code needs a filter; refusing to read any new mail")
    needles = [c.lower() for c in contains if c.strip()]
    if not needles:
        raise ValueError("wait_for_code needs a non-empty filter")

    conn, mailbox = _connect()
    try:
        floor = _latest_uid(conn, mailbox)
        deadline = time.time() + timeout
        seen: set[int] = set()

        while time.time() < deadline:
            conn.select(mailbox, readonly=True)
            ok, data = conn.uid("search", None, f"UID {floor + 1}:*")
            if ok == "OK" and data and data[0]:
                for raw_uid in data[0].split():
                    uid = int(raw_uid)
                    if uid <= floor or uid in seen:
                        continue
                    seen.add(uid)
                    ok2, fetched = conn.uid("fetch", raw_uid, "(RFC822)")
                    if ok2 != "OK" or not fetched or not fetched[0]:
                        continue
                    msg = email.message_from_bytes(fetched[0][1])
                    haystack = f"{_header(msg,'From')} {_header(msg,'Subject')}".lower()
                    if not any(n in haystack for n in needles):
                        continue          # not ours: never opened past the headers
                    if code := extract_code(_body_text(msg)):
                        return code
            time.sleep(poll)
        return None
    finally:
        try:
            conn.logout()
        except Exception:
            pass

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

Two backends, same guarantees and same call. The Gmail REST API is preferred
because it is ordinary HTTPS on 443 and therefore works from a sandbox that
only routes 443 -- IMAP's 993 does not. IMAP stays as a fallback for a plain
mailbox with an app password.

The Gmail scope requested is gmail.readonly, which cannot send, delete or
modify anything even if this code were wrong.

Without credentials this raises MailUnavailable and the caller queues the
application for a human, which is what it does for any other step it cannot
complete.
"""
from __future__ import annotations

import base64
import email
import imaplib
import json
import os
import re
import time
import urllib.parse
import urllib.request
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
    # IMAP needs port 993, which a 443-only sandbox does not route. Say so here
    # rather than letting the caller sit through a connection timeout.
    host = os.getenv("IMAP_HOST", "imap.gmail.com")
    port = int(os.getenv("IMAP_PORT", "993"))
    if not user or not password:
        raise MailUnavailable(
            "no mailbox configured: set GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / "
            "GMAIL_REFRESH_TOKEN for the Gmail API (works anywhere HTTPS does), "
            "or MAIL_USER / MAIL_APP_PASSWORD for IMAP (needs port 993)"
        )
    try:
        # Bounded: where 993 is not routed at all the connect otherwise sits
        # there until the caller's own timeout, which looks like a hang rather
        # than the configuration problem it is.
        conn = imaplib.IMAP4_SSL(host, port,
                                 timeout=int(os.getenv("IMAP_TIMEOUT", "15")))
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


# --- Gmail REST backend ------------------------------------------------------

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def _gmail_configured() -> bool:
    return all(os.getenv(k) for k in
               ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"))


def _access_token() -> str:
    """Trade the long-lived refresh token for a short-lived access token."""
    body = urllib.parse.urlencode({
        "client_id": os.getenv("GMAIL_CLIENT_ID", ""),
        "client_secret": os.getenv("GMAIL_CLIENT_SECRET", ""),
        "refresh_token": os.getenv("GMAIL_REFRESH_TOKEN", ""),
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["access_token"]
    except Exception as exc:                      # noqa: BLE001
        raise MailUnavailable(f"could not refresh Gmail token: {exc}") from exc


def _api(path: str, token: str, **params) -> dict:
    url = f"{GMAIL_API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _b64(data: str) -> str:
    pad = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad).decode("utf-8", "replace")
    except Exception:
        return ""


def _gmail_body(payload: dict) -> str:
    """Flatten a Gmail payload tree into text."""
    out = []
    stack = [payload]
    while stack:
        part = stack.pop()
        if not isinstance(part, dict):
            continue
        stack.extend(part.get("parts") or [])
        if part.get("mimeType", "").startswith("text/"):
            if data := (part.get("body") or {}).get("data"):
                out.append(_b64(data))
    return re.sub(r"<[^>]+>", " ", " ".join(out))


def _gmail_headers(payload: dict) -> dict:
    return {h.get("name", "").lower(): h.get("value", "")
            for h in (payload.get("headers") or [])}


def _wait_gmail(needles: list[str], timeout: int, poll: int,
                since: float | None = None) -> str | None:
    token = _access_token()
    # Everything already in the mailbox is behind this line and stays invisible.
    #
    # Where the line falls matters. Drawn when the wait begins, it lands after
    # the click that asked for the code -- and up to fifteen seconds after,
    # since the page is given time to settle first. BNY's mail arrives inside
    # that gap: four "BNY Careers - Confirm Your Identity" messages sat in the
    # mailbox while the waiter timed out at 180 seconds having ignored every
    # one of them as pre-existing. The caller knows when it pressed the button,
    # so it passes that moment in.
    started_ms = int((since if since is not None else time.time()) * 1000)
    deadline = time.time() + timeout
    seen: set[str] = set()

    while time.time() < deadline:
        try:
            listing = _api("messages", token, q="newer_than:1d", maxResults=25)
        except Exception:
            time.sleep(poll)
            continue

        for stub in listing.get("messages") or []:
            mid = stub.get("id")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            try:
                msg = _api(f"messages/{mid}", token, format="full")
            except Exception:
                continue
            # Arrived after the wait began, or it is pre-existing mail.
            if int(msg.get("internalDate", "0")) < started_ms:
                continue
            payload = msg.get("payload") or {}
            head = _gmail_headers(payload)
            haystack = f"{head.get('from','')} {head.get('subject','')}".lower()
            if not any(n in haystack for n in needles):
                continue          # not ours: the body is never looked at
            if code := extract_code(_gmail_body(payload)):
                return code
        time.sleep(poll)
    return None


def wait_for_code(contains: list[str], timeout: int = 180,
                  poll: int = 10, since: float | None = None) -> str | None:
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

    if _gmail_configured():
        return _wait_gmail(needles, timeout, poll, since)

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


# --- one-time OAuth setup ----------------------------------------------------

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def authorize(port: int = 8765) -> str:
    """Run the loopback OAuth flow and return a refresh token.

    Run this once, on a machine with a browser. It asks only for
    gmail.readonly, and the token it prints is what the runs actually use --
    the client secret and the token together are the credential, so treat the
    output like a password.
    """
    import http.server
    import threading
    import webbrowser

    client_id = os.getenv("GMAIL_CLIENT_ID")
    client_secret = os.getenv("GMAIL_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise MailUnavailable("set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET first")

    redirect = f"http://localhost:{port}"
    got: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                                    # noqa: N802
            query = urllib.parse.urlparse(self.path).query
            got.update({k: v[0] for k, v in urllib.parse.parse_qs(query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Authorized. You can close this tab.")

        def log_message(self, *_args):                        # keep the console clean
            return

    server = http.server.HTTPServer(("localhost", port), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()

    url = f"{AUTH_URL}?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": GMAIL_SCOPE,
        "access_type": "offline",
        "prompt": "consent",          # force a refresh token even on re-auth
    })
    print("Opening your browser to authorize gmail.readonly access.")
    print("If it does not open, paste this into a browser:\n")
    print(url + "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    for _ in range(300):
        if "code" in got or "error" in got:
            break
        time.sleep(1)
    server.server_close()

    if "error" in got:
        raise MailUnavailable(f"authorization refused: {got['error']}")
    if "code" not in got:
        raise MailUnavailable("timed out waiting for the browser redirect")

    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": got["code"],
        "grant_type": "authorization_code",
        "redirect_uri": redirect,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read())
    token = payload.get("refresh_token")
    if not token:
        raise MailUnavailable(
            "Google returned no refresh token. Revoke this app's access at "
            "myaccount.google.com/permissions and run it again."
        )
    return token

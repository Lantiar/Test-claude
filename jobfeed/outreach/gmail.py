"""Send, and find out what came back. Plain REST, no client library.

jobfeed imports nothing outside the standard library and this keeps that true:
Gmail's API is HTTPS and JSON, so a dependency here would be a dependency in
the scheduled runner too.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import parseaddr

API = "https://gmail.googleapis.com/gmail/v1/users/me"


def _token() -> str:
    data = urllib.parse.urlencode({
        "client_id": os.environ["GMAIL_CLIENT_ID"],
        "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
        "refresh_token": os.environ["GMAIL_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }).encode()
    with urllib.request.urlopen("https://oauth2.googleapis.com/token",
                                data=data, timeout=30) as r:
        return json.loads(r.read())["access_token"]


def _get(path: str, token: str, **params):
    url = f"{API}/{path}"
    if params:
        # doseq, because metadataHeaders is a repeated parameter. Without it
        # the list is stringified into one malformed value, Gmail ignores it
        # and answers 200 with the message and no headers at all -- so every
        # bounce classifies as a human reply, the address is never suppressed,
        # and the only symptom is a reply rate that looks too good.
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}, doseq=True)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def send(to: str, subject: str, body: str, thread_id: str | None = None,
         in_reply_to: str | None = None, token: str | None = None) -> dict:
    """Send one plain-text message. Returns the Gmail id and thread id."""
    token = token or _token()
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = os.getenv("OUTREACH_FROM", "")
    msg["Subject"] = subject
    if in_reply_to:
        # Both headers, because clients differ on which they thread by, and a
        # follow-up that starts a new thread reads as a second cold email
        # rather than a nudge on the first.
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)

    payload = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if thread_id:
        payload["threadId"] = thread_id
    req = urllib.request.Request(
        f"{API}/messages/send", data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())

    # The RFC message-id, which is what a reply will name in In-Reply-To. The
    # send response carries Gmail's own id, which is a different thing and does
    # not appear in anybody's headers.
    meta = _get(f"messages/{out['id']}", token, format="metadata",
                metadataHeaders="Message-Id")
    rfc = ""
    for h in meta.get("payload", {}).get("headers", []):
        if h["name"].lower() == "message-id":
            rfc = h["value"]
    return {"id": out["id"], "thread_id": out.get("threadId"), "message_id": rfc}


# ---- reading --------------------------------------------------------------

_OOO = re.compile(r"\b(out of (the )?office|on (vacation|leave|holiday)|"
                  r"automatic reply|auto[- ]?reply|away from|parental leave)\b", re.I)


def classify(headers: dict, snippet: str) -> tuple[str, str]:
    """(kind, bounce_type). kind is human | auto | bounce."""
    frm = (headers.get("from") or "").lower()
    subject = (headers.get("subject") or "").lower()
    # A bounce comes from the mail system, not a person: an empty envelope
    # sender, a daemon address, or a delivery-status content type.
    if (headers.get("return-path") in ("<>", "")
            or "mailer-daemon" in frm or "postmaster" in frm
            or "delivery-status" in (headers.get("content-type") or "").lower()
            or "undeliverable" in subject or "delivery status notification" in subject):
        hard = re.search(r"\b5\.\d\.\d\b", snippet) or "permanent" in snippet.lower()
        soft = re.search(r"\b4\.\d\.\d\b", snippet) or "temporar" in snippet.lower()
        return "bounce", ("hard" if hard or not soft else "soft")
    if headers.get("auto-submitted", "").lower() not in ("", "no") \
            or headers.get("x-autoreply") or headers.get("x-autorespond") \
            or _OOO.search(subject) or _OOO.search(snippet[:300]):
        return "auto", ""
    return "human", ""


def inbound_since(history_id: str | None, token: str | None = None,
                  max_pages: int = 5) -> tuple[list[dict], str]:
    """Messages that arrived since history_id, and the new history_id.

    Polling, deliberately. The push alternative needs a Cloud project, a
    Pub/Sub topic and subscription, IAM for Gmail's service account, a public
    webhook, and a daily renewal -- and the watch expires silently after seven
    days, so a missed renewal looks exactly like a quiet inbox. It buys seconds
    of latency on a workflow whose next action is four days away.
    """
    token = token or _token()
    if not history_id:
        prof = _get("profile", token)
        return [], prof["historyId"]

    out, page, newest = [], None, history_id
    for _ in range(max_pages):
        try:
            data = _get("history", token, startHistoryId=history_id,
                        historyTypes="messageAdded", pageToken=page)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # The history id aged out (Gmail keeps about a week). Re-anchor
                # rather than replaying the whole mailbox.
                prof = _get("profile", token)
                return [], prof["historyId"]
            raise
        newest = data.get("historyId", newest)
        for h in data.get("history", []):
            for added in h.get("messagesAdded", []):
                out.append(added["message"]["id"])
        page = data.get("nextPageToken")
        if not page:
            break

    seen, messages = set(), []
    for mid in out:
        if mid in seen:
            continue
        seen.add(mid)
        m = _get(f"messages/{mid}", token, format="metadata",
                 metadataHeaders=["From", "To", "Subject", "Message-Id",
                                  "In-Reply-To", "References", "Return-Path",
                                  "Auto-Submitted", "Content-Type"])
        headers = {h["name"].lower(): h["value"]
                   for h in m.get("payload", {}).get("headers", [])}
        messages.append({
            "id": mid,
            "thread_id": m.get("threadId"),
            "headers": headers,
            "snippet": m.get("snippet", ""),
            "from": parseaddr(headers.get("from", ""))[1].lower(),
            "received_at": int(m.get("internalDate", 0)) / 1000 or time.time(),
        })
    return messages, newest

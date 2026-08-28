"""Tell you about postings you have not seen yet.

The hard part is not sending mail, it is deciding what is new. "New" cannot
mean "posted recently" -- Simplify carries jobs posted weeks ago that this feed
met for the first time today, and an employer's date says nothing about whether
you have seen it. It has to mean "first appeared in this feed after the last
time you were told", which is a watermark over first_seen_at.

The watermark travels in the published snapshot rather than in the local
database, because the runner is stateless: it rebuilds from that snapshot every
half hour, and a watermark it forgot would mean every run re-announcing all
2,249 jobs. Being mailed the whole corpus twice is the failure this guards
against, and it is the kind you only notice after it has happened.
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
from email.message import EmailMessage

from . import db as _db

STATE_KEY = "notified_through"


def watermark(con) -> float:
    try:
        return float(_db.get_state(con, "notify", STATE_KEY) or 0)
    except (TypeError, ValueError):
        return 0.0


def set_watermark(con, value: float) -> None:
    _db.set_state(con, "notify", STATE_KEY, repr(float(value)))
    con.commit()


def new_jobs(con, since: float | None = None, limit: int = 60) -> list[dict]:
    """Open jobs this feed first saw after the watermark, newest first."""
    since = watermark(con) if since is None else since
    rows = con.execute(
        "SELECT j.*, c.name AS company FROM job j "
        "LEFT JOIN company c ON c.id = j.company_id "
        "WHERE j.status='open' AND j.first_seen_at > ? "
        "ORDER BY j.first_seen_at DESC, j.posted_at DESC LIMIT ?",
        (since, limit)).fetchall()
    return [{
        "company": r["company"] or "?",
        "title": r["title"] or "(untitled)",
        "url": r["canonical_url"] or "",
        "locations": json.loads(r["locations"] or "[]"),
        "season": r["season"] or "",
        "first_seen_at": r["first_seen_at"],
        "sources": r["id"],
    } for r in rows]


def render(jobs: list[dict]) -> tuple[str, str]:
    """(plain text, html). One line per job: what it is, and where to apply."""
    lines, rows = [], []
    for j in jobs:
        where = ", ".join(j["locations"])[:60]
        tail = " — ".join(x for x in (where, j["season"]) if x)
        lines.append(f"{j['company']} — {j['title']}"
                     + (f"\n  {tail}" if tail else "")
                     + f"\n  {j['url']}\n")
        rows.append(
            f'<tr><td style="padding:9px 0;border-bottom:1px solid #e6e2dc">'
            f'<a href="{_esc(j["url"])}" style="color:#1f5f4f;font-weight:600;'
            f'text-decoration:none">{_esc(j["company"])} — {_esc(j["title"])}</a>'
            + (f'<div style="color:#6d675f;font-size:13px;margin-top:2px">'
               f'{_esc(tail)}</div>' if tail else "")
            + "</td></tr>")
    text = "\n".join(lines) or "Nothing new."
    html = (
        '<div style="font:15px/1.5 -apple-system,BlinkMacSystemFont,'
        '\'Segoe UI\',Roboto,sans-serif;color:#1c1a17;max-width:640px">'
        f'<p style="color:#6d675f;font-size:13px;margin:0 0 4px">'
        f'{len(jobs)} new posting{"s" if len(jobs) != 1 else ""}</p>'
        f'<table style="width:100%;border-collapse:collapse">{"".join(rows)}</table>'
        '<p style="color:#6d675f;font-size:12px;margin-top:18px">'
        'From jobfeed · <a href="https://lantiar.github.io/Test-claude/" '
        'style="color:#1f5f4f">the full list</a></p></div>')
    return text, html


def _esc(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def send(subject: str, text: str, html: str = "") -> None:
    user = os.getenv("MAIL_USER", "")
    password = os.getenv("MAIL_APP_PASSWORD", "")
    to = os.getenv("NOTIFY_TO") or user
    if not user or not password:
        raise RuntimeError("MAIL_USER and MAIL_APP_PASSWORD are needed to send")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(user, password)
            s.send_message(msg)
    except (OSError, smtplib.SMTPConnectError) as exc:
        # Said plainly, because the raw errors are misleading. A sandbox with
        # no outbound SMTP reports "Address family not supported by protocol"
        # from the IPv6 attempt and a bare timeout from the IPv4 one, neither
        # of which mentions the port being closed -- and both look like a bug
        # in the credentials rather than in the network.
        raise RuntimeError(
            f"could not reach {host}:{port} ({type(exc).__name__}: {exc}). "
            "Port 587 is blocked in some sandboxes; GitHub's runners allow it. "
            "The credentials are not the problem when this is the error."
        ) from exc
    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError(
            "Gmail refused the login. MAIL_APP_PASSWORD must be an app "
            "password (16 characters, no spaces), not the account password."
        ) from exc


def run(con, dry_run: bool = False, limit: int = 60) -> dict:
    """Mail whatever is new, then move the watermark past it.

    The watermark moves only after the send succeeds. A failed send that
    advanced it would drop those postings on the floor silently, which is worse
    than a duplicate mail -- one is noise, the other is a job you never hear
    about.
    """
    jobs = new_jobs(con, limit=limit)
    if not jobs:
        return {"new": 0, "sent": False}
    text, html = render(jobs)
    subject = (f"{len(jobs)} new internship{'s' if len(jobs) != 1 else ''}"
               f" — {jobs[0]['company']}"
               + (f" and {len(jobs) - 1} more" if len(jobs) > 1 else ""))
    if dry_run:
        return {"new": len(jobs), "sent": False, "subject": subject, "text": text}
    send(subject, text, html)
    set_watermark(con, max(j["first_seen_at"] for j in jobs))
    return {"new": len(jobs), "sent": True, "subject": subject}

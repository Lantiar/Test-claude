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
import urllib.request
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


def send_ntfy(subject: str, jobs: list[dict]) -> None:
    """Push to a phone through ntfy.sh.

    One notification for the batch rather than one per job. Twenty new postings
    at 3am should be one buzz with a list in it, not twenty -- a notifier that
    is annoying gets muted, and a muted notifier is the same as none.

    The topic is the only secret ntfy has: anyone who knows the name can read
    the notifications and publish to it. So it is a random string rather than
    something guessable like "jobfeed", and it belongs in the environment
    beside the other credentials.
    """
    topic = os.getenv("NTFY_TOPIC", "")
    if not topic:
        raise RuntimeError("NTFY_TOPIC is not set")
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

    lines = []
    for j in jobs[:20]:
        where = ", ".join(j["locations"])[:44]
        lines.append(f"[{j['company']} — {j['title']}]({j['url']})"
                     + (f"  \n_{where}_" if where else ""))
    if len(jobs) > 20:
        lines.append(f"\n_and {len(jobs) - 20} more_")
    body = "\n\n".join(lines)

    req = urllib.request.Request(
        f"{server}/{topic}", data=body.encode("utf-8"), method="POST")
    # Headers must be latin-1 safe: ntfy takes the title as a header, and a job
    # title with an en dash or an accent in it raises UnicodeEncodeError deep
    # inside http.client rather than anywhere that mentions the title.
    req.add_header("Title", _ascii(subject))
    req.add_header("Markdown", "yes")
    req.add_header("Tags", "briefcase")
    # Tapping the notification opens the dashboard; the individual links are
    # in the body.
    req.add_header("Click", os.getenv(
        "NTFY_CLICK", "https://lantiar.github.io/Test-claude/"))
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status >= 300:
            raise RuntimeError(f"ntfy returned {r.status}")


def _ascii(s: str) -> str:
    """What survives an HTTP header. Em dashes and smart quotes do not."""
    return (str(s).replace("—", "-").replace("–", "-").replace("’", "'")
            .encode("ascii", "replace").decode("ascii"))


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
        return {"new": len(jobs), "sent": False, "subject": subject,
                "text": text, "channels": channels()}

    # Every channel that is configured gets it, and one failing does not stop
    # the others. The watermark moves if any of them landed -- what it records
    # is "you were told", and being told once is enough.
    sent, failed = [], []
    for name, fn in (("ntfy", lambda: send_ntfy(subject, jobs)),
                     ("email", lambda: send(subject, text, html))):
        if name not in channels():
            continue
        try:
            fn()
            sent.append(name)
        except Exception as exc:
            failed.append(f"{name}: {exc}")
    if sent:
        set_watermark(con, max(j["first_seen_at"] for j in jobs))
    return {"new": len(jobs), "sent": bool(sent), "subject": subject,
            "channels": sent, "failed": failed}


def channels() -> list[str]:
    """Which notifiers are configured. Absent is not an error -- a feed with no
    notifier is a perfectly ordinary way to run this."""
    out = []
    if os.getenv("NTFY_TOPIC"):
        out.append("ntfy")
    if os.getenv("MAIL_USER") and os.getenv("MAIL_APP_PASSWORD"):
        out.append("email")
    return out

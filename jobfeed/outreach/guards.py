"""The rules that decide whether a message may exist, and when it may go.

Everything here is a refusal. Nothing in this module makes outreach happen; it
only stops it, and it runs before a draft is written rather than before it is
sent. A draft that should never have existed is easier to reason about than one
skipped at the last moment for a reason nobody wrote down.
"""
from __future__ import annotations

import datetime as dt
import random
import time

DAY = 86400

# One address, once, ever.
# One company, once a week -- three recruiters at one firm getting near
# identical mail on one day is the pattern that gets reported rather than
# ignored, and recruiting teams share inboxes.
COMPANY_COOLDOWN_DAYS = 7
# A domain that accepts all mail cannot be verified, so it gets a strict
# ration rather than a ban: one speculative send per company per week.
ACCEPT_ALL_PER_COMPANY_DAYS = 7

# How long before the same recruiter may hear from us a second time. A new
# application months later is a real reason to write again; a second one the
# same month is the same note twice.
RECONTACT_DAYS = 90

# Sending shape. Volume is sampled rather than fixed, because a flat number
# every weekday is itself a pattern.
DAILY_MIN, DAILY_MAX = 4, 8
WINDOW_START, WINDOW_END = 9 * 60, 16 * 60 + 30      # recipient-local minutes
GAP_MIN, GAP_MAX = 25, 90                             # minutes between sends
JITTER = 7                                            # minutes either side

# Bounce rate over a rolling week above which everything stops.
BOUNCE_LIMIT = 0.02
BOUNCE_WINDOW_DAYS = 7
# Below this many sends the rate is noise -- one bounce in three is 33% and
# means nothing. Without a floor the breaker trips on its own first week.
BOUNCE_MIN_SENDS = 25


def suppressed(con, email: str, company_id: int | None,
               campaign: str | None = None, followup: bool = False) -> str:
    """Why this may not be written to, or '' if it may.

    `campaign` is one prepare pass at one company -- up to three recruiters,
    one per day. Sends belonging to it do not count toward that company's
    cooldown, or the second and third notes of a batch would each be blocked
    by the first. The cooldown is a gap between campaigns, not between sends;
    since it measures from the most recent send, it starts when the batch
    finishes rather than when it began.
    """
    now = time.time()
    row = con.execute(
        "SELECT reason FROM suppression WHERE email=? AND (until IS NULL OR until>?)",
        ((email or "").lower(), now)).fetchone()
    if row:
        return row["reason"]
    if company_id is None:
        return ""
    row = con.execute(
        "SELECT reason FROM suppression WHERE company_id=? AND email IS NULL "
        "AND (until IS NULL OR until>?)", (company_id, now)).fetchone()
    if row:
        return row["reason"]
    # A follow-up continues a conversation this person is already in. The
    # company cooldown exists to stop a stream of strangers, and applying it
    # here holds a nudge on an open thread because someone *else* at the same
    # employer was written to -- which reads as being dropped, not as restraint.
    # Suppression of the address itself, and every reply and bounce rule, still
    # apply above.
    if followup:
        return ""
    return _company_cooldown(con, company_id, campaign)


def _company_cooldown(con, company_id: int, campaign: str | None = None) -> str:
    sql = ("SELECT MAX(o.sent_at) t FROM outreach o "
           "JOIN contact c ON c.id=o.contact_id "
           "WHERE c.company_id=? AND o.sent_at IS NOT NULL")
    params: list = [company_id]
    if campaign:
        sql += " AND (o.campaign IS NULL OR o.campaign != ?)"
        params.append(campaign)
    row = con.execute(sql, params).fetchone()
    if row and row["t"] and time.time() - row["t"] < COMPANY_COOLDOWN_DAYS * DAY:
        return f"this company was contacted {int((time.time()-row['t'])//DAY)}d ago"
    return ""


def campaign_allowed(con, company_id: int | None) -> str:
    """Whether a NEW batch may start at this company, or why not.

    Checked when drafts are written rather than when they are sent, so that a
    company still working through one batch does not accumulate a second.
    """
    if company_id is None:
        return ""
    row = con.execute(
        "SELECT reason FROM suppression WHERE company_id=? AND email IS NULL "
        "AND (until IS NULL OR until>?)", (company_id, time.time())).fetchone()
    if row:
        return row["reason"]
    pending = con.execute(
        "SELECT COUNT(*) n FROM outreach o JOIN contact c ON c.id=o.contact_id "
        "WHERE c.company_id=? AND o.status IN ('draft','queued')",
        (company_id,)).fetchone()
    if pending and pending["n"]:
        return f"a batch of {pending['n']} is still going out here"
    return _company_cooldown(con, company_id)


def accept_all_allowed(con, company_id: int) -> bool:
    """One speculative send per company per week, and only that."""
    cutoff = time.time() - ACCEPT_ALL_PER_COMPANY_DAYS * DAY
    row = con.execute(
        "SELECT COUNT(*) n FROM outreach o JOIN contact c ON c.id=o.contact_id "
        "WHERE c.company_id=? AND c.email_status='accept_all' "
        "AND o.sent_at IS NOT NULL AND o.sent_at > ?", (company_id, cutoff)).fetchone()
    return (row["n"] if row else 0) == 0


def suppress(con, email: str | None, company_id: int | None, reason: str,
             days: float | None = None) -> None:
    con.execute(
        "INSERT INTO suppression(email, company_id, reason, until, created_at) "
        "VALUES(?,?,?,?,?)",
        ((email or "").lower() or None, company_id, reason,
         time.time() + days * DAY if days else None, time.time()))
    con.commit()


# ---- scheduling -----------------------------------------------------------

def _next_weekday(d: dt.date) -> dt.date:
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def plan_sends(count: int, tz_offset_hours: float = -5.0,
               start: dt.datetime | None = None, rng=None) -> list[float]:
    """Epoch timestamps for `count` sends, scattered like a person.

    Four things are randomised because a bot is recognisable by any one of them
    being fixed: how many go out on a day, which minute the day starts on, the
    gap between sends, and a final wobble on each. Weekends are skipped -- mail
    to a recruiter on a Sunday is either ignored or noticed for the wrong
    reason.

    Times are computed in the recipient's local day and converted back, since
    the window that matters is theirs. tz_offset_hours defaults to US Eastern.
    """
    rng = rng or random.Random()
    now = start or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    local_now = now + dt.timedelta(hours=tz_offset_hours)
    out: list[float] = []

    # Today only if there is still usable window left in the recipient's day.
    # Clamping a past slot forward to "now" instead -- the obvious fix, and the
    # one that was here -- schedules the send for whatever the wall clock says,
    # so a queue built at 2am puts mail in someone's inbox at 2am. Every other
    # guard in this function is about the hour, and that one line undid all of
    # them without failing anything.
    day = _next_weekday(local_now.date())
    floor = 0
    if day == local_now.date():
        floor = local_now.hour * 60 + local_now.minute + 5 + JITTER
        if floor > WINDOW_END - GAP_MIN:
            day = _next_weekday(day + dt.timedelta(days=1))
            floor = 0

    remaining = count
    while remaining > 0:
        quota = min(remaining, rng.randint(DAILY_MIN, DAILY_MAX))
        # Start somewhere in the first ninety minutes of the window, not on it.
        minute = max(WINDOW_START + rng.randint(0, 90), floor)
        floor = 0
        for _ in range(quota):
            if minute > WINDOW_END:
                break
            at_local = dt.datetime.combine(day, dt.time()) + dt.timedelta(
                minutes=minute + rng.randint(-JITTER, JITTER))
            at_utc = at_local - dt.timedelta(hours=tz_offset_hours)
            out.append(at_utc.replace(tzinfo=dt.timezone.utc).timestamp())
            remaining -= 1
            minute += rng.randint(GAP_MIN, GAP_MAX)
        day = _next_weekday(day + dt.timedelta(days=1))
    return sorted(out)


# ---- circuit breaker ------------------------------------------------------

def health(con) -> dict:
    cutoff = dt.date.fromtimestamp(time.time() - BOUNCE_WINDOW_DAYS * DAY).isoformat()
    row = con.execute(
        "SELECT COALESCE(SUM(sent),0) sent, COALESCE(SUM(hard_bounced),0) hard, "
        "COALESCE(SUM(replied),0) replied, MAX(paused) paused "
        "FROM send_health WHERE day >= ?", (cutoff,)).fetchone()
    sent, hard = row["sent"] or 0, row["hard"] or 0
    rate = (hard / sent) if sent else 0.0
    return {"sent": sent, "hard_bounced": hard, "replied": row["replied"] or 0,
            "bounce_rate": rate, "paused": bool(row["paused"]),
            "over_limit": sent >= BOUNCE_MIN_SENDS and rate > BOUNCE_LIMIT}


def bump(con, field: str, when: float | None = None, n: int = 1) -> None:
    day = dt.date.fromtimestamp(when or time.time()).isoformat()
    con.execute("INSERT OR IGNORE INTO send_health(day) VALUES(?)", (day,))
    con.execute(f"UPDATE send_health SET {field}={field}+? WHERE day=?", (n, day))
    con.commit()


def pause(con, reason: str) -> None:
    day = dt.date.today().isoformat()
    con.execute("INSERT OR IGNORE INTO send_health(day) VALUES(?)", (day,))
    con.execute("UPDATE send_health SET paused=1, paused_reason=? WHERE day=?",
                (reason, day))
    con.commit()

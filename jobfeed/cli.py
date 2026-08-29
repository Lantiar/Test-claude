"""jobfeed -- poll the sources, list what came out.

  python -m jobfeed.cli poll simplify
  python -m jobfeed.cli list --since 7 --limit 40
  python -m jobfeed.cli stats
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

from . import db as _db

# Where `sync` reads from by default: the hourly feed this project publishes.
FEED_URL = os.getenv(
    "JOBFEED_URL", "https://lantiar.github.io/Test-claude/jobs.json")
from .ingest import enrich_unresolved, poll, retire_missing
from .sources import load_all, names


def _when(ts, estimate=False) -> str:
    if not ts:
        return "        ?"
    d = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    return f"~{d}" if estimate else f" {d}"


def cmd_poll(args, con) -> int:
    for source in (args.sources or names()):
        try:
            c = poll(con, source)
        except Exception as exc:
            print(f"{source}: FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        con.commit()
        print(f"{source}: {c['seen']} seen, {c['new']} new, "
              f"{c.get('ats',0)} by ats, {c.get('url',0)} by url, "
              f"{c.get('text',0)} by text")
        if source == "simplify" and args.retire:
            print(f"{source}: retired {retire_missing(con, source)} stale")
    return 0


def cmd_list(args, con) -> int:
    where, params = ["j.status='open'"], []
    if args.since:
        where.append("j.first_seen_at > ?")
        params.append(__import__("time").time() - args.since * 86400)
    if args.company:
        where.append("c.norm LIKE ?")
        params.append(f"%{args.company.lower()}%")
    rows = con.execute(
        f"SELECT j.*, c.name AS company FROM job j LEFT JOIN company c "
        f"ON c.id=j.company_id WHERE {' AND '.join(where)} "
        f"ORDER BY j.posted_at DESC LIMIT ?", (*params, args.limit)).fetchall()
    print(f"{'posted':>11}  {'company':<26} {'title':<50} where")
    print("-" * 116)
    for r in rows:
        locs = ", ".join(json.loads(r["locations"] or "[]"))[:26]
        print(f"{_when(r['posted_at'], r['posted_at_is_estimate']):>11}  "
              f"{(r['company'] or '?')[:25]:<26} {r['title'][:49]:<50} {locs}")
    print(f"\n{len(rows)} shown. A ~ date is our first sighting, not the "
          f"employer's posting date.")
    return 0


def cmd_run(args, con) -> int:
    """One full cycle, for a scheduler to call. Never raises on one bad source.

    A source that fails must not stop the others, and must not look like a
    source that found nothing: poll() records the failure in source_run either
    way, and the exit code says whether anything got through -- which is what a
    cron mail or a red Actions run is actually for.
    """
    ok = 0
    for source in names():
        try:
            c = poll(con, source)
            con.commit()
            print(f"{source}: {c['seen']} seen, {c['new']} new, "
                  f"{c.get('ats',0)}+{c.get('url',0)}+{c.get('text',0)} matched, "
                  f"{c.get('link',0)} non-job links")
            ok += 1
        except Exception as exc:
            print(f"{source}: FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
    if ok:
        d = enrich_unresolved(con)
        print(f"enrich: named {d['named']} of {d['looked']}")
        if args.retire:
            print(f"retired {retire_missing(con, 'simplify')} stale")
    return 0 if ok else 1


def cmd_enrich(args, con) -> int:
    d = enrich_unresolved(con, args.limit)
    print(f"looked up {d['looked']}, named {d['named']}, could not read {d['failed']}")
    return 0


def cmd_serve(args, con) -> int:
    from .server import serve

    # The server opens its own connection per request, so hand it the path and
    # let this one go: SQLite objects are not shareable across threads.
    con.close()
    serve(args.db, args.port, args.host)
    return 0


def cmd_notify(args, con) -> int:
    from .notify import run as notify_run
    from .seed import restore_watermark

    if args.watermark_from:
        mark = restore_watermark(con, args.watermark_from)
        print(f"watermark restored: {_when(mark).strip() if mark else 'none found'}")
    d = notify_run(con, dry_run=args.dry_run, limit=args.limit)
    if not d["new"]:
        print("nothing new to send")
        return 0
    if args.dry_run:
        print(f"would send to {', '.join(d['channels']) or '(nothing configured)'}: "
              f"{d['subject']}\n")
        print(d["text"][:2000])
        return 0
    print(f"{d['new']} posting(s): {d['subject']}")
    if d["channels"]:
        print(f"  delivered by: {', '.join(d['channels'])}")
    # Named, not swallowed. A channel that quietly stopped working looks
    # exactly like a quiet week, and this is the whole point of the notifier.
    for problem in d["failed"]:
        print(f"  FAILED {problem}", file=sys.stderr)
    return 0 if d["sent"] else 1


# ---- outreach -------------------------------------------------------------
#
# Every one of these defaults to a dry run. `dispatch` is the only one that can
# put mail in front of a stranger, and it needs --send spelled out: the failure
# mode of this whole pipeline is a batch going out before anyone has read it.

def _outreach(name):
    from .outreach import run as _run
    return getattr(_run, name)


def cmd_outreach_prepare(args, con) -> int:
    d = _outreach("prepare")(con, limit=args.limit, per_company=args.per_company,
                             dry_run=args.dry_run)
    print(f"{d['jobs']} applied job(s), {d['contacts']} contact(s), "
          f"{d['drafts']} draft(s)")
    if d["verify"]:
        print("  addresses: " + ", ".join(f"{k} {v}" for k, v in
                                          sorted(d["verify"].items())))
    for why in d["skipped"]:
        print(f"  skipped {why}")
    return 0


def cmd_outreach_drafts(args, con) -> int:
    """Read what would go out. The review step, before anything is queued."""
    rows = con.execute(
        "SELECT o.*, c.full_name, c.title, c.email, c.email_status, cm.name company "
        "FROM outreach o JOIN contact c ON c.id=o.contact_id "
        "LEFT JOIN company cm ON cm.id=c.company_id "
        "WHERE o.status=? ORDER BY o.created_at LIMIT ?",
        (args.status, args.limit)).fetchall()
    for r in rows:
        when = (f"  send_after {dt.datetime.fromtimestamp(r['send_after']):%a %d %b %H:%M}"
                if r["send_after"] else "")
        print("=" * 78)
        print(f"[{r['id']}] {r['company'] or '?'} / step {r['step']} / "
              f"variant {r['variant']} / {r['email_status']}{when}")
        print(f"To: {r['full_name']} <{r['email']}> -- {r['title'] or '?'}")
        print(f"Subject: {r['subject']}\n")
        print(r["body"])
    print("=" * 78)
    print(f"{len(rows)} {args.status}")
    return 0


def cmd_outreach_polish(args, con) -> int:
    d = _outreach("polish_drafts")(con, limit=args.limit)
    print(f"{d['seen']} draft(s): {d['edited']} edited, {d['unchanged']} left as "
          f"written, {d['rejected']} revision(s) refused"
          + (f", {d['retried']} after a retry" if d.get("retried") else "")
          + f"  (${d['cost']:.5f})")
    for n in d["notes"]:
        print(f"  fixed {n}")
    # Named, not swallowed. A refused revision means the editor tried to change
    # something it may not, and that is worth seeing rather than counting.
    for p in d["problems"]:
        print(f"  REFUSED {p}", file=sys.stderr)
    return 0


def cmd_outreach_schedule(args, con) -> int:
    d = _outreach("schedule")(con)
    if not d["scheduled"]:
        print("no drafts waiting")
        return 0
    print(f"queued {d['scheduled']}: {d['first']} -> {d['last']}")
    return 0


def cmd_outreach_dispatch(args, con) -> int:
    d = _outreach("dispatch")(con, dry_run=not args.send, limit=args.limit)
    if d.get("paused"):
        print(f"PAUSED -- {d['health']}", file=sys.stderr)
        return 1
    verb = "sent" if args.send else "would send"
    print(f"{verb} {d['sent']}")
    for h in d.get("held", []):
        print(f"  held {h}")
    for e in d.get("errors", []):
        print(f"  FAILED {e}", file=sys.stderr)
    return 1 if d.get("errors") else 0


def cmd_outreach_watch(args, con) -> int:
    d = _outreach("watch")(con)
    print(f"{d['seen']} new message(s): {d.get('human',0)} human, "
          f"{d.get('auto',0)} auto, {d.get('bounce',0)} bounce")
    if d.get("paused"):
        print("breaker tripped -- sending is paused", file=sys.stderr)
        return 1
    return 0


def cmd_outreach_followups(args, con) -> int:
    print(f"queued {_outreach('followups')(con)['queued']} follow-up(s)")
    return 0


def cmd_outreach_status(args, con) -> int:
    from .outreach import guards
    rows = con.execute("SELECT status, COUNT(*) n FROM outreach GROUP BY status")
    print("outreach: " + (", ".join(f"{r['status']} {r['n']}" for r in rows) or "empty"))
    rows = con.execute("SELECT email_status, COUNT(*) n FROM contact "
                       "GROUP BY email_status")
    print("contacts: " + (", ".join(f"{r['email_status']} {r['n']}" for r in rows)
                          or "empty"))
    h = guards.health(con)
    print(f"health:   {h['sent']} sent / {h['hard_bounced']} hard bounce "
          f"({h['bounce_rate']:.1%}) over {guards.BOUNCE_WINDOW_DAYS}d"
          + ("  PAUSED" if h["paused"] else ""))
    return 0


def cmd_outreach_verify(args, con) -> int:
    """Probe addresses through Apify without touching the database.

    Standalone because the split it reports -- how many of a firm's addresses
    come back deliverable rather than accept_all -- is the thing that decides
    whether guessing addresses at that firm is worth doing at all.
    """
    from .outreach import apify

    statuses = apify.verify(args.emails)
    tally = {}
    for email in args.emails:
        st = statuses.get(email, "no answer")
        tally[st] = tally.get(st, 0) + 1
        print(f"  {st:<12} {email}")
    print("  " + ", ".join(f"{k} {v}" for k, v in sorted(tally.items())))
    return 0


def cmd_render(args, con) -> int:
    from .server import render

    d = render(con, args.out)
    print(f"wrote {d['path']} -- {d['jobs']} jobs, {d['bytes']//1024}KB")
    return 0


def cmd_stage(args, con) -> int:
    """Set a stage by naming the job rather than by knowing its key."""
    from . import apply as _apply

    like = f"%{args.match.lower()}%"
    rows = con.execute(
        "SELECT j.*, c.name AS company FROM job j LEFT JOIN company c "
        "ON c.id=j.company_id WHERE j.status='open' AND "
        "(LOWER(j.title) LIKE ? OR LOWER(c.name) LIKE ?) LIMIT 12",
        (like, like)).fetchall()
    if not rows:
        print(f"nothing matches {args.match!r}", file=sys.stderr)
        return 1
    if len(rows) > 1:
        # Refusing rather than picking: setting a stage on the wrong job is
        # quiet and wrong, and the list is short enough to choose from.
        print(f"{args.match!r} matches {len(rows)} jobs -- be more specific:",
              file=sys.stderr)
        for r in rows:
            print(f"   {(r['company'] or '?')[:24]:26} {r['title'][:52]}",
                  file=sys.stderr)
        return 1
    r = rows[0]
    saved = _apply.set_stage(con, _apply.job_key(r), args.to, args.note)
    print(f"{r['company']} — {r['title']}: {saved['stage']}")
    return 0


def cmd_sync(args, con) -> int:
    """Pull the hourly published feed into the local database.

    The same code path as the scheduled runner's restore, pointed at the
    published URL instead of a file. Local application stages are untouched:
    they live in their own table, keyed on the job's stable identity rather
    than on a row id, precisely so a sync cannot disturb them.
    """
    from .seed import seed

    try:
        d = seed(con, args.url)
    except Exception as exc:
        print(f"sync: could not read {args.url}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1
    print(f"sync: {d['in_snapshot']} in the feed, {d['restored']} new here, "
          f"{d['already_here']} already had")
    return 0


def cmd_seed(args, con) -> int:
    from .seed import seed

    try:
        d = seed(con, args.source)
    except Exception as exc:
        # Not fatal: a first run has no snapshot to read, and a run that cannot
        # reach one should still poll rather than stop. But it is said out
        # loud, because a silent seed failure looks exactly like a healthy run
        # that has lost every story-only job.
        print(f"seed: could not read {args.source}: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
    print(f"seed: {d['in_snapshot']} in snapshot, {d['restored']} restored, "
          f"{d['already_here']} already here")
    return 0


def cmd_publish(args, con) -> int:
    from .publish import publish

    d = publish(con, args.out)
    print(f"wrote {args.out}/: {d['jobs']} jobs, {d['recent']} recent, "
          f"{d['links']} other links")
    return 0


def cmd_export(args, con) -> int:
    """A plain-text snapshot of the job list, one JSON object per line.

    The database is the working state and a 3MB binary; this is the durable,
    diffable copy. It exists because a scheduler that keeps its SQLite in a
    build cache can lose it -- and what is lost is not the listings, which can
    be refetched in a second, but first_seen_at: the record of when each job
    appeared, which is the one thing here that cannot be reconstructed after
    the fact.
    """
    import os

    rows = con.execute(
        "SELECT j.*, c.name AS company FROM job j "
        "LEFT JOIN company c ON c.id=j.company_id ORDER BY j.first_seen_at").fetchall()
    if d := os.path.dirname(args.out):
        os.makedirs(d, exist_ok=True)
    with open(args.out, "w") as fh:
        for r in rows:
            fh.write(json.dumps({
                "company": r["company"], "title": r["title"],
                "url": r["canonical_url"], "ats_key": r["ats_key"],
                "locations": json.loads(r["locations"] or "[]"),
                "season": r["season"], "status": r["status"],
                "posted_at": r["posted_at"],
                "posted_at_is_estimate": bool(r["posted_at_is_estimate"]),
                "first_seen_at": r["first_seen_at"],
            }, sort_keys=True) + "\n")
    print(f"exported {len(rows)} jobs to {args.out}")
    return 0


def cmd_stats(args, con) -> int:
    q = lambda s, *a: con.execute(s, a).fetchone()[0]
    total = q("SELECT COUNT(*) FROM job")
    open_ = q("SELECT COUNT(*) FROM job WHERE status='open'")
    print(f"jobs           {total} ({open_} open)")
    print(f"companies      {q('SELECT COUNT(*) FROM company')}")
    print(f"sightings      {q('SELECT COUNT(*) FROM sighting')}")
    print(f"other links    {q('SELECT COUNT(*) FROM link')}")
    print(f"real dates     {q('SELECT COUNT(*) FROM job WHERE posted_at_is_estimate=0')}"
          f" of {total}")
    print("\nby ats:")
    for r in con.execute("SELECT COALESCE(ats,'(none)') a, COUNT(*) n FROM job "
                         "GROUP BY a ORDER BY n DESC LIMIT 14"):
        print(f"   {r['n']:5}  {r['a']}")
    print("\nlast run per source:")
    for r in con.execute(
            "SELECT source, MAX(started_at) t, items_seen, items_new, error "
            "FROM source_run GROUP BY source"):
        print(f"   {r['source']:<12} {_when(r['t']).strip()}  seen {r['items_seen']:<6} "
              f"new {r['items_new']:<6} {r['error'] or ''}")
    return 0


def main(argv=None) -> int:
    load_all()
    ap = argparse.ArgumentParser(prog="jobfeed")
    ap.add_argument("--db", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("poll"); p.set_defaults(fn=cmd_poll)
    p.add_argument("sources", nargs="*", choices=names() + [], default=None)
    p.add_argument("--retire", action="store_true",
                   help="close jobs the source has stopped listing")

    p = sub.add_parser("list"); p.set_defaults(fn=cmd_list)
    p.add_argument("--since", type=float, default=0, help="days")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--company", default="")

    p = sub.add_parser("run"); p.set_defaults(fn=cmd_run)
    p.add_argument("--retire", action="store_true")

    p = sub.add_parser("enrich"); p.set_defaults(fn=cmd_enrich)
    p.add_argument("--limit", type=int, default=40)

    p = sub.add_parser("serve"); p.set_defaults(fn=cmd_serve)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")

    p = sub.add_parser("notify"); p.set_defaults(fn=cmd_notify)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=60)
    p.add_argument("--watermark-from", default="",
                   help="a meta.json (path or URL) to restore the watermark from")

    p = sub.add_parser("render"); p.set_defaults(fn=cmd_render)
    p.add_argument("--out", default="dashboard.html")

    p = sub.add_parser("stage"); p.set_defaults(fn=cmd_stage)
    p.add_argument("match", help="company or title substring")
    p.add_argument("to", help="one of: " + ", ".join(__import__(
        "jobfeed.apply", fromlist=["x"]).STAGES))
    p.add_argument("--note", default=None)

    p = sub.add_parser("sync"); p.set_defaults(fn=cmd_sync)
    p.add_argument("--from", dest="url", default=FEED_URL)

    p = sub.add_parser("seed"); p.set_defaults(fn=cmd_seed)
    p.add_argument("source", help="path or URL of a published jobs.json")

    p = sub.add_parser("publish"); p.set_defaults(fn=cmd_publish)
    p.add_argument("--out", default="site")

    p = sub.add_parser("export"); p.set_defaults(fn=cmd_export)
    p.add_argument("--out", default="data/jobs.jsonl")

    p = sub.add_parser("outreach", help="recruiter email pipeline")
    osub = p.add_subparsers(dest="ocmd", required=True)

    q = osub.add_parser("prepare"); q.set_defaults(fn=cmd_outreach_prepare)
    q.add_argument("--limit", type=int, default=5, help="applied jobs to process")
    q.add_argument("--per-company", type=int, default=3)
    q.add_argument("--dry-run", action="store_true",
                   help="skip address verification and title cleaning "
                        "(the recruiter search still runs, and still costs)")

    q = osub.add_parser("drafts"); q.set_defaults(fn=cmd_outreach_drafts)
    q.add_argument("--status", default="draft")
    q.add_argument("--limit", type=int, default=20)

    q = osub.add_parser("polish"); q.set_defaults(fn=cmd_outreach_polish)
    q.add_argument("--limit", type=int, default=20)

    q = osub.add_parser("schedule"); q.set_defaults(fn=cmd_outreach_schedule)

    q = osub.add_parser("dispatch"); q.set_defaults(fn=cmd_outreach_dispatch)
    q.add_argument("--send", action="store_true", help="actually send")
    q.add_argument("--limit", type=int, default=10)

    q = osub.add_parser("watch"); q.set_defaults(fn=cmd_outreach_watch)
    q = osub.add_parser("followups"); q.set_defaults(fn=cmd_outreach_followups)
    q = osub.add_parser("status"); q.set_defaults(fn=cmd_outreach_status)

    q = osub.add_parser("verify"); q.set_defaults(fn=cmd_outreach_verify)
    q.add_argument("emails", nargs="+")

    p = sub.add_parser("stats"); p.set_defaults(fn=cmd_stats)

    args = ap.parse_args(argv)
    con = _db.connect(args.db)
    try:
        return args.fn(args, con)
    finally:
        con.commit(); con.close()


if __name__ == "__main__":
    sys.exit(main())

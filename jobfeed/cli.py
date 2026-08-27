"""jobfeed -- poll the sources, list what came out.

  python -m jobfeed.cli poll simplify
  python -m jobfeed.cli list --since 7 --limit 40
  python -m jobfeed.cli stats
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from . import db as _db
from .ingest import poll, retire_missing
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

    p = sub.add_parser("stats"); p.set_defaults(fn=cmd_stats)

    args = ap.parse_args(argv)
    con = _db.connect(args.db)
    try:
        return args.fn(args, con)
    finally:
        con.commit(); con.close()


if __name__ == "__main__":
    sys.exit(main())

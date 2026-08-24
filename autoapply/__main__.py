"""CLI: python -m autoapply apply <url>"""
from __future__ import annotations

import argparse
import os
import sys

from .pipeline import apply_to
from .store import Store


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    ap = argparse.ArgumentParser(prog="autoapply")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("apply", help="apply to one job link")
    a.add_argument("url")
    a.add_argument("--mode", choices=["approve", "auto"], default=None)
    a.add_argument("--ats", choices=["greenhouse", "lever"], default=None,
                   help="force a worker when host detection can't identify the ATS")
    a.add_argument("--dry-run", action="store_true",
                   help="fill and verify but never submit, whatever the mode")

    sub.add_parser("stats", help="show counts")
    q = sub.add_parser("queue", help="list the review queue")
    q.add_argument("--all", action="store_true")

    args = ap.parse_args(argv)
    store = Store()

    if args.cmd == "stats":
        for k, v in store.stats().items():
            print(f"{k:18} {v}")
        return 0

    if args.cmd == "queue":
        rows = store.queue_list(include_resolved=args.all)
        if not rows:
            print("queue empty")
        for r in rows:
            print(f"[{r['id']}] {r['ats']:10} {r['url']}\n     reasons: {r['reasons_json']}")
        return 0

    result = apply_to(args.url, mode=args.mode, store=store, dry_run=args.dry_run,
                      ats_override=args.ats)
    icon = {"applied": "OK", "queued": "QUEUED", "skipped": "SKIP", "errored": "ERROR"}
    print(f"{icon.get(result.status, result.status)}: {result.job.url}")
    print(f"  ats      : {result.job.ats}")
    if result.outcome:
        o = result.outcome
        print(f"  fields   : {len(o.fields)} discovered, {len(o.filled_ids)} filled")
        print(f"  verified : {o.verified}")
        if o.missing_required:
            print(f"  missing  : {', '.join(o.missing_required)}")
        if o.screenshot_path:
            print(f"  shot     : {o.screenshot_path}")
    if result.gate.reasons:
        print(f"  gate     : {'; '.join(result.gate.reasons)}")
    if result.detail:
        print(f"  detail   : {result.detail.splitlines()[0]}")
    return 0 if result.status in ("applied", "queued", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())

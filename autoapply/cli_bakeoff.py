"""`python -m autoapply.cli_bakeoff <url> [--only dom,computeruse]`"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .bakeoff import bake_off, table
from .fillers import load_all, names


def main(argv=None) -> int:
    load_all()
    ap = argparse.ArgumentParser(prog="autoapply-bakeoff")
    ap.add_argument("url")
    ap.add_argument("--only", default="",
                    help=f"comma-separated subset of {','.join(names())}")
    ap.add_argument("--profile", default=os.getenv("PROFILE_PATH",
                                                   "config/profile.json"))
    args = ap.parse_args(argv)

    profile = json.load(open(args.profile))
    only = [s.strip() for s in args.only.split(",") if s.strip()] or None
    reports = bake_off(args.url, profile, only)

    print()
    print(table(reports))
    print()
    best = reports[0] if reports else None
    if best and best.filled:
        print(f"best: {best.filler} -- {best.filled}/{best.fields_found} "
              f"answered, review {'reached' if best.reached_review else 'not reached'}")
    for r in reports:
        if r.errors:
            print(f"  {r.filler}: {r.errors[0][:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

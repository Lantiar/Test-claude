---
name: jobfeed
description: Show the internship dashboard - pull the hourly feed, report what is new, and hand over a dashboard file. Also sets application stages (applied, oa, interview, final, offer, accepted, rejected) and answers questions about the job list. Use when the user asks for the dashboard, the job list, what is new, or to mark where they are with a job.
---

# jobfeed

One deduplicated list of internships from SimplifyJobs and zero2sudo's
Instagram stories, refreshed hourly by a GitHub Action, plus the user's own
application stage per job.

Everything runs from the repo root with `python3 -m jobfeed.cli <command>`.
No dependencies, no virtualenv: it is standard library only.

## The default run

With no argument, do all of this and report once at the end:

1. `python3 -m jobfeed.cli sync` — pull the published feed into the local
   database. Say how many arrived and how many were new.
2. `python3 -m jobfeed.cli render --out dashboard.html`
3. Send `dashboard.html` with SendUserFile (`display: "render"`).
4. Report **what changed**, not what exists: jobs first seen since the last
   sync, anything at a stage past `interested`, and the feed's age.

Get the feed's age from `meta.json` rather than assuming it is fresh:

```bash
curl -s https://lantiar.github.io/Test-claude/meta.json
```

If `generated_at` is more than about two hours old, say so plainly and check
whether the Action is still running. Stories expire after 24 hours, so a
stalled feed is losing links, not merely going stale.

## Arguments

- `new` / `today` — jobs first seen in the last day.
  `python3 -m jobfeed.cli list --since 1`
- `<company>` — that employer's postings.
  `python3 -m jobfeed.cli list --company <name>`
- `applied` / `interview` / any stage — filter to it; read the `application`
  table directly with sqlite3 for anything the CLI does not cover.
- `mark <something> <stage>` — `python3 -m jobfeed.cli stage "<match>" <stage>`.
  It refuses when the match is ambiguous and prints the candidates; show
  those and ask which, rather than guessing. Setting a stage on the wrong job
  is silent and wrong.
- `serve` — `python3 -m jobfeed.cli serve`, an interactive viewer at
  http://127.0.0.1:8765 where stages can be clicked rather than typed.
  **Only useful when Claude Code is running on the user's own machine.** In a
  remote or container session 127.0.0.1 is the container, not their browser,
  so send the rendered file instead and say why.

## Things worth getting right

**Two dates, and they are not the same.** `posted_at` is when the employer
posted the job; `first_seen_at` is when this feed noticed it. They are often
weeks apart — an Apple posting dated 28 July was shared on Instagram on 28
August. When `posted_is_estimate` is true, no source knew the employer's date
and this is the feed's own first sighting standing in for it. Render those
with a `~`, and never describe them as "posted" — a list headed "posted this
week" that ignores the flag quietly means "noticed this week".

**Stages are the only thing here that cannot be rebuilt.** Everything else is
derived from the sources and comes back on the next poll. Never reset or bulk
edit the `application` table; never pass `--db` to a throwaway path when the
user means their real one.

**Stages are private.** `publish` builds only from the `job` table, so where
they have applied never reaches the public branch. Keep it that way — do not
add stage data to anything under `site/`.

## Checking on the feed itself

- Data: <https://lantiar.github.io/Test-claude/> — `meta.json`, `recent.json`,
  `jobs.json`, `links.json`
- Runs: Actions → jobfeed. Scheduled ones show event `schedule`; GitHub's
  scheduler is best-effort and can slip 10–30 minutes under load, so one late
  run is not a fault.
- `meta.json` carries a `sources[]` block with `last_run_at` and a `note` per
  source. A `note` is a run that had something to say; `null` means clean.

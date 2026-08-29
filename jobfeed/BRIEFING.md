# jobfeed — briefing

Written to be handed to someone (or some model) who has never seen this repo,
so work can carry on somewhere else. Read this first, then
[`ARCHITECTURE.md`](ARCHITECTURE.md) for how it works and
[`README.md`](README.md) for how to operate it.

## The one-paragraph version

`jobfeed` collects internship postings from SimplifyJobs and an Instagram
account, deduplicates them into one list, and publishes that list as static
JSON that a web dashboard reads. Bolted onto it is a recruiter outreach
pipeline: mark a job applied, press a button, and it finds early-career
recruiters at that company, verifies their addresses, writes a short note
each from templates, scatters the sends over working days, mails them from
your own Gmail, watches for replies and bounces, and follows up twice unless
someone answers.

## Where it lives

| | |
|---|---|
| Repo | <https://github.com/Lantiar/Test-claude> |
| Default branch | `claude/plan-reasoning-verification-7i3zz7` |
| Working branch | `claude/autoapply-job-application-1df8wq` |
| Published data | the `jobfeed-data` branch — the runner force-pushes `jobs.json`, `recent.json`, `meta.json`, `links.json` there every run |
| Dashboard | <https://lantiar.github.io/Test-claude/> (Pages) and a Vercel project whose production branch is `jobfeed-data` |
| Runner | `.github/workflows/jobfeed.yml`, triggered every 30 min by cron-job.org calling `workflow_dispatch` |

`jobfeed/` is a subdirectory of a larger `autoapply` repo. Nothing outside
`jobfeed/`, `.github/workflows/jobfeed.yml` and `config/` belongs to this
project.

## Standing rules

These are the owner's, not conventions — they hold in every session.

1. **`--dry-run` only. Never send real mail or submit a real application
   without asking first.** `dispatch` refuses to send without `--send`; on the
   runner the send is additionally gated on the repository variable
   `OUTREACH_SEND=1`, which is the kill switch.
2. **Never commit `config/profile.json`, `.env`, or the resume PDF.** All
   three are gitignored. The resume lives in Upstash, not in git.
3. **No third-party imports anywhere under `jobfeed/`.** Standard library
   only — Gmail, Apify, OpenAI and Upstash are all hand-rolled over `urllib`.
   A dependency here means a `pip install` step on the runner and a supply
   chain for something that sends mail as the owner. `pytest` is the one
   exception, and only in `tests/`.
4. **A model may never write text that goes to a stranger.** Two models are
   in the pipeline and both are constrained mechanically, not by prompting:
   the title cleaner may only *delete* words the employer already used
   (checked with a subsequence test), and the copy editor's revision is
   discarded whole if any fact, number, URL or proper noun moved.

## A prompt to start elsewhere

Paste this into a fresh session that has the repo checked out:

> This is `jobfeed` in <https://github.com/Lantiar/Test-claude> — an
> internship-posting aggregator plus a recruiter outreach pipeline. Read
> `jobfeed/BRIEFING.md`, then `jobfeed/ARCHITECTURE.md`, before changing
> anything.
>
> Rules that hold for the whole session: dry-run only — never send real mail
> or submit a real application without asking me first; never commit
> `config/profile.json`, `.env` or the resume; standard library only under
> `jobfeed/` (pytest in tests is the exception); no model may generate text
> that reaches a stranger — the title cleaner may only delete the employer's
> own words and the copy editor's output is thrown away if any fact moved.
>
> Run `python -m pytest jobfeed/tests -q` first; 131 tests should pass. When
> you change behaviour, write the test so it fails against the old code before
> you make it pass — this project has been bitten repeatedly by checks that
> measured something other than what they claimed.

## The map

```
jobfeed/
  cli.py            every command; start here to trace anything
  db.py             14 tables + MIGRATIONS applied on connect()
  identity.py       the dedupe ladder — the core of the feed half
  dedupe.py         merging, and the merge_log
  ingest.py         sources -> database
  publish.py        database -> jobs.json / recent.json / meta.json
  web.html          the dashboard: job board tab + outreach tab
  sources/          simplify.py, instagram.py
  api/              Vercel functions: stages.js, outreach.js
  outreach/
    apify.py        find recruiters, verify addresses, rank them
    templates.py    the letters
    titles.py       clean a noisy job title (deletion only)
    polish.py       the copy editor, and verify() which vetoes it
    profile.py      who you are; the dashboard-editable fields
    guards.py       cooldowns, suppression, scatter scheduling, the breaker
    gmail.py        send, read, classify replies and bounces
    board.py        Upstash: the shared state between web and runner
    run.py          the five passes, and serve_board() which drives them
  tests/            test_outreach.py (113), e2e_demo.py, timeline_demo.py
```

### Two ideas worth understanding before editing

**The runner is stateless; the published JSON is the store.** Every run
rebuilds the SQLite database from a snapshot. Outreach state cannot live
there — it lives in Upstash, and `board.save()` / `board.load()` move it in
and out. Rows are keyed by things that survive a rebuild (company name, job
key, email address) and never by rowid.

**The identity ladder decides what is one job.** Tier 1 is the ATS posting
id, tier 2 the canonical URL, tier 3 normalized text. Tier 3 may *never*
write identity — it can only suggest a merge. Loosening that is how a feed
starts silently collapsing distinct postings together.

## Environment

Nothing here has a safe default that reaches the network; a missing variable
disables its feature rather than guessing.

**GitHub → Settings → Secrets and variables → Actions.** Secrets:

```
APIFY_TOKEN            recruiter sourcing and address verification
OPENAI_API_KEY         title cleaner + copy editor (optional; without it
                       drafts go out exactly as the templates wrote them)
GMAIL_CLIENT_ID        \
GMAIL_CLIENT_SECRET     >  scopes: gmail.send, gmail.readonly
GMAIL_REFRESH_TOKEN    /
MAIL_USER              SMTP notifications for the feed half
MAIL_APP_PASSWORD
NTFY_TOPIC             push notifications
KV_REST_API_URL        Upstash Redis — the shared state
KV_REST_API_TOKEN
```

Variables (not secret):

```
OUTREACH_SEND          "1" arms the send. Anything else = dry run.
OUTREACH_FROM          the From address
OUTREACH_BRACKET       subject-line bracket, default "Prev Google/Zon"
OUTREACH_COUNTRY       where recruiters must be, default "US"
OUTREACH_SEARCH_LADDER how far to widen a thin search, default "15,45,100,200"
NOTIFY_TO              feed notification recipient
IG_TARGET              the Instagram account, default "zero2sudo"
JOBFEED_MIN_MINUTES    minimum gap between runs, default 20
```

**Vercel project environment.** `JOBFEED_PASSPHRASE` gates every write to
`api/stages` and `api/outreach`; while it is unset, writing is refused
outright rather than allowed. Upstash is connected through Vercel's storage
integration, which injects the `KV_REST_API_*` pair.

**Local `.env`** mirrors the secrets above for running commands by hand. Also
`RESUME_PATH` (default `config/files/resume.pdf`, gitignored) and
`JOBFEED_DB`.

## Running it

```bash
python -m pytest jobfeed/tests -q          # 131 tests, ~1s, no network
python -m jobfeed.tests.e2e_demo           # the whole outreach pipeline, faked clock
python -m jobfeed run                      # one full feed cycle
python -m jobfeed outreach status
python -m jobfeed outreach board           # one pass over what the web asked for
python -m jobfeed outreach dispatch        # dry run; --send actually mails
python -m jobfeed serve                    # local viewer
```

`jobfeed outreach verify <address>…` probes addresses and writes nothing.
Use it with a control that cannot possibly succeed — a verifier that says yes
to everything looks exactly like one that works.

## What is proven, and what is not

Proven against the live services: recruiter search and ranking; address
verification (with negative controls); Gmail send with a PDF attachment;
reply detection; bounce detection end to end, including the circuit breaker
pausing dispatch at 3.3% against a 2% limit; the dashboard button, the
outreach tab, and the settings round-trip through Upstash.

Not proven: a reply from a real recruiter to a real send. The pipeline has
mailed real addresses only in tests aimed at the owner's own mailbox.

## Where it stands today

- The send is armed (`OUTREACH_SEND=1`), so anything queued will go out.
- One job is queued: Philips `workday:philips:r-2027_590404`. Its earlier
  drafts were aimed at two France-based recruiters and have been dropped; it
  will redraft against the US filter on the next runner pass.
- The rest of the queue is empty.

## Known gaps

- **`plan_sends` hardcodes `tz_offset_hours=-5.0`.** That is EST; New York is
  EDT (UTC−4) from March to November, so the send window currently drifts an
  hour early for half the year. Not yet fixed.
- **The SMTP notification channel has never run.** It is blocked in the
  sandbox this was built in and first executes on the runner.
- **GitHub's own `schedule:` cron has never fired** — zero scheduled runs
  across many slots, `state: active`, no root cause found. An external
  trigger calls `workflow_dispatch` instead. If the feed goes stale, check
  that trigger before anything else.
- **No follow-up has ever been sent to a real recruiter**, only in simulation.
- `intern-list.com` was scoped and never built.
- Credentials were pasted into a chat transcript during development and are
  worth rotating: the Upstash token, `OPENAI_API_KEY`, the three Gmail
  values, and `MAIL_APP_PASSWORD`.

## The lesson this project keeps re-learning

Every serious bug here was **a check that measured something other than what
it claimed**, and none of them raised an error. A Gmail request that returned
200 with no headers, so every bounce was classified as a human reply. A
verification status string that was never mapped, so every verified address
scored "unknown". Our own sent mail read back as a reply. `lastrowid` after
an ignored insert, attaching drafts to contacts that did not exist. A
substring company match that accepted "Morgan Philips Group" as Philips. A
dashboard reading "nothing to send" over two letters loaded and waiting.

The e2e suite once passed 27 of 27 while its own log showed two real bugs.
Assert on what you actually want to be true, print enough to see when you
asserted the wrong thing, and give every probe a control that must fail.

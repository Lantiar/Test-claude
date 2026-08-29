# jobfeed — architecture

One deduplicated list of internship postings, from several sources, with dates
— and a pipeline that writes to a recruiter when you mark one applied.

This document is for whoever picks the project up next. It covers what each
part does, why the load-bearing decisions went the way they did, and the
failure modes that have actually happened here. The reasoning matters more
than the file list: most of the bugs in this codebase have been *silent* ones,
where a check measured something other than what it claimed, and the code
looked right in review.

## Ground rules

**No third-party imports anywhere in `jobfeed/`.** Every module is standard
library only. The scheduled runner installs nothing, so a dependency here is a
dependency in CI, and a broken wheel becomes a silent gap in the feed. Gmail
and Apify are both HTTPS + JSON, which is `urllib` and `json`.

**Exact matching only, never fuzzy.** Two postings merge when they share an
identity, not when they look similar. A missed match shows a duplicate, which
is visible and annoying. A false match deletes a job, which is invisible.

**Append-only sightings.** Every observation is recorded; job rows are derived
from them. A source that goes wrong can be reasoned about after the fact.

## The two halves

```
  SOURCES            INGEST                  PUBLISH
  ───────            ──────                  ───────
  Simplify   ─┐                            ┌─ jobs.json  ─→ GitHub Pages
              ├─→ poll → dedupe → job ────→┤               Vercel
  Instagram  ─┘         (identity)          └─ meta.json ─→ notify (ntfy, email)
                                                             │
                                        stage: applied ──────┘
                                              │
  OUTREACH                                    ▼
  ────────    prepare ─→ schedule ─→ dispatch ─→ watch ─→ followups
              (Apify)    (scatter)   (Gmail)    (Gmail)
```

The halves are independent. The feed runs on a 30-minute trigger and knows
nothing about outreach. Outreach is driven by hand and never runs unattended.

---

## Part 1 — the feed

### Sources

| Module | Gives | Notes |
|---|---|---|
| `sources/simplify.py` | The SimplifyJobs internship repo | Structured; carries `terms`, `date_posted`, locations |
| `sources/instagram.py` | zero2sudo's story links | Unstructured; some links are jobs, some are not |

Adapters return `RawListing` (`models.py`) and nothing else. Each poll writes a
`source_run` row, so **"no new jobs" and "the adapter broke" are
distinguishable** — a count alone cannot tell them apart, and `log_note()`
records the reasons that are neither an error nor nothing (no OCR available,
no live stories, a field missing from a response).

### Identity — the core of the whole thing

`identity.py` decides what a posting *is*, on a three-tier ladder. Only the
first two tiers may write identity onto a job row.

1. **ATS posting id.** Parsed per family: Greenhouse, Lever, Ashby,
   SmartRecruiters, Workday, iCIMS, Oracle, Workable, Rippling, TikTok,
   ByteDance, Apple, Amazon, Tesla.
2. **Canonical URL.** Tracking parameters stripped, redirect wrappers
   unwrapped (`l.instagram.com/?u=…` peeled up to three hops).
3. **Normalized text.** Company plus title, as a last resort.

`_GLOBAL_ID` names the ATSs whose posting id is globally unique — those drop
the employer from the key. Workday, iCIMS and Oracle keep the tenant, because
their ids are only unique within one.

**The anti-merge rule** (`dedupe._conflicts`): two identities of the same kind
that disagree mean two different jobs, whatever the text says. This is what
stops a Tier-3 text match from collapsing two real postings.

**Tier 3 never writes identity.** A text match may attach a sighting and
backfill a missing title, but it must not stamp its keys onto the row. It did
once, and put Tencent posting R107344's keys onto R107363.

### Dates

Three distinct timestamps, deliberately not collapsed:

- `posted_at` — when the employer posted it. `posted_at_is_estimate` marks a
  guess.
- `source_reported_at` — when the source said so.
- `first_seen_at` — when this feed first saw it.

"Newest posted" and "newest to the feed" are different questions and the UI
answers both.

### Publishing

`publish.py` writes `jobs.json` plus `meta.json` and copies the viewer. Sort
order is `posted_at DESC`, then `first_seen_at` **rounded to the minute**, then
company and title `COLLATE NOCASE`.

Both tiebreaks are scar tissue. Sub-second `first_seen_at` was meaningless
noise that reordered the list between runs; rounding lets the name decide
within a poll. `COLLATE NOCASE` exists because SQLite's default BINARY
collation disagrees with the browser's `localeCompare` — the server and the
client sorted the same data differently, and 14 rows moved.

### State on the runner

The runner is **stateless**: the published `jobs.json` *is* the store. Each run
seeds from it, polls, and republishes. Notifications track a watermark over
`first_seen_at`, carried in `meta.json` as `notified_through_epoch`.

Seeding needs a title+URL fallback check. Without it, keyless jobs were
re-added on every sync and the database grew past the feed it was seeded from.

---

## Part 2 — outreach

Triggered by marking a job past `interested`. Five passes, run independently on
the same schedule so that one failing does not block the others — particularly
reading replies, since that is what stops follow-ups going to people who have
already answered.

### 1. `prepare` — find people, write drafts

**One note per person, and every role named in it.** Applications are grouped
by company: three roles at one employer become three notes — one to each of
three recruiters — and each names all three roles. Drafting per posting instead
produced *nine* near-identical emails, and the cooldown then held eight of them
permanently, so two of the three applications got no outreach at all.

Titles come from `job.title`, exactly as the source reported them, and are
used three ways: cleaned of any trailing season for the body, additionally
stripped of the team suffix for the subject, and deduplicated so two postings
sharing a title are listed once. Beyond two roles the note switches from a
sentence to a bulleted list — four real posting titles run together are
unreadable.

`outreach_job` records every application a note covers. Without it the roles a
note did not name as primary look undrafted and get written again the next day.

A later application at the same company goes to the **next recruiters who have
not heard from you**. `_recruiters` tops the roster up from Apify when the
untouched ones run short — without that, the first application spends all three
cached names and every later one finds nobody new. Once the roster is genuinely
exhausted, `prepare` stops rather than repeating: a second note to the same
person is only defensible after `RECONTACT_DAYS` (90).

### Campaigns

A **campaign** is one `prepare` pass at one company — up to `per_company` (3)
recruiters, scheduled one per day. Sends inside a campaign do not count toward
that company's cooldown, or the first note out would block the other two; the
cooldown measures from the most recent send, so it starts when the batch
*finishes*. `campaign_allowed()` gates a new batch: a company still working
through one does not accumulate a second, and one whose last batch is inside
the cooldown simply waits — the application is deferred, never dropped.

A sixty-day walk over six applications at one employer (three on day 0, then
days 3, 14 and 45):

```
day  0  three applications  -> one batch of 3, sent days 3/4/5   (recruiters 1-3)
day  3  application         -> deferred by cooldown, sent 15/16/17 (recruiters 4-6)
day 14  application         -> deferred, sent 27/28/29            (recruiters 7-9)
day 45  application         -> company free, sent 48/49/50        (recruiters 10-12)

12 emails, 12 distinct recipients, nothing held, no application without outreach
```

Sources recruiters from Apify (`harvestapi~linkedin-profile-search`, cookie-free
so it never drives your own LinkedIn session), then keeps the addresses the
actor already verified and probes only the rest
(`michael.g~email-verifier-validator`).

Contacts are cached 90 days per company. Re-scraping re-rolls *which* three
people you get, so a second application to the same firm would otherwise write
to a different set of strangers at a company you have already contacted.

Writes drafts. Nothing leaves.

### 2. `schedule` — scatter

`guards.plan_sends()` assigns send times: weekdays only, 09:00–16:30 in the
recipient's local day, with randomized daily volume (4–8), start minute, gaps
(25–90 min) and per-send jitter (±7 min). Four things are randomized because a
bot is recognizable by any one of them being fixed.

`run.schedule()` then enforces **one note per company per day**, and asks for
enough slots to span as many days as the busiest company needs.

### 3. `dispatch` — send

Gmail API, from the user's own address. Refuses entirely while the circuit
breaker is tripped. Re-checks every draft against the guards at send time, not
only when it was written. Requires `--send`; the default is a dry run.

### 4. `watch` — read what came back

Polls `history.list` incrementally, re-anchoring on a 404 when the history id
ages out. Classifies each message as `human`, `auto` or `bounce`, matches it to
an outreach row by threading headers → thread id → sender, and acts:

- **hard bounce** → suppress the address, mark the contact bounced, bump the
  breaker
- **soft bounce** → retry in two days
- **any reply, including an auto-reply** → stop the sequence

Polling, not Pub/Sub push: push needs a Cloud project, a topic, a subscription,
IAM, a public webhook and a daily renewal — and the watch expires *silently*
after seven days, so a missed renewal looks exactly like a quiet inbox. It buys
seconds of latency on a workflow whose next action is four days away.

### 5. `followups` — day 4 and day 9

Re-uses the subject that was actually sent, and the original `Message-Id` and
thread id. A re-rendered subject would drift if the posting were retitled, and
a changed subject is a new thread rather than a nudge on the old one.

### The guards

| Guard | Rule | Why |
|---|---|---|
| Company cooldown | 7 days between campaigns | Three notes from one stranger is what a team forwards to each other |
| One per company per day | Enforced in `schedule` | Three near-identical emails compared over one lunch |
| Window | Weekdays, 09:00–16:30 recipient-local | A Sunday 3am send is the most legible bot signal there is |
| Randomization | Volume, start minute, gaps, jitter | A fixed value in any one of them is the tell |
| `accept_all` quota | One speculative send per company per week | Most large employers accept mail for any local part |
| Circuit breaker | 7-day hard-bounce rate > 2% with ≥25 sends → pause all | A high bounce rate is evidence the addresses are bad |
| Suppression | Per address and per company, with optional expiry | |

**`accept_all` is not a shade of `verified`.** Google Workspace and Microsoft
365 accept mail for any local part and sort it out later, so for most large
employers the probe cannot distinguish a real mailbox from a typo. Folding that
into "verified" is how a pipeline convinces itself an invented address is safe
to write to.

---

## Data model

`db.py`, SQLite, 14 tables.

**Feed:** `company`, `job`, `sighting` (append-only), `link`, `source_run`,
`adapter_state`, `merge_log`, `application`.

**Outreach:** `contact`, `outreach`, `outreach_job`, `reply`, `suppression`,
`send_health`.

`application.job_key` is `COALESCE(ats_key, url_key, canonical_url)` — the
strongest identity a job has, so a stage survives the job row being re-derived.

---

## Operations

```
jobfeed run                      # one full cycle, for the scheduler
jobfeed poll [source] --retire
jobfeed list --since 7 --company amazon
jobfeed sync | seed | publish | export | stats
jobfeed notify --dry-run
jobfeed serve                    # local viewer
jobfeed stage <match> <stage>

jobfeed outreach prepare         # find recruiters, write drafts
jobfeed outreach drafts          # read what would go out
jobfeed outreach schedule
jobfeed outreach dispatch        # dry run; --send to actually send
jobfeed outreach watch
jobfeed outreach followups
jobfeed outreach status
jobfeed outreach verify <email>… # probe addresses, no database writes
```

### Environment

| Variable | For |
|---|---|
| `APIFY_TOKEN` | Recruiter sourcing and address verification |
| `APIFY_PEOPLE_ACTOR`, `APIFY_VERIFY_ACTOR` | Actor overrides — the store churns |
| `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` | Scopes: `gmail.send`, `gmail.readonly` |
| `OUTREACH_FROM` | From address |
| `OUTREACH_BRACKET` | The subject-line bracket, default `Prev Google/Zon` |
| `JOBFEED_URL` | Feed to sync from |
| `JOBFEED_MIN_MINUTES` | Minimum interval guard on the runner |

Never commit `.env`, `config/profile.json`, or the resume.

### Scheduling

An external trigger (cron-job.org) calls `workflow_dispatch` on
`.github/workflows/jobfeed.yml`. GitHub's own `schedule:` cron is configured
and has **never fired** — 0 scheduled runs across many slots despite
`state: active`, no root cause found. It stays as a harmless fallback behind
the minimum-interval guard. If you are debugging a gap in the feed, check the
external trigger first.

---

## Failure modes seen here

Kept because the shape recurs: **a check that measures something other than
what it claims**. None of these raised an error.

| What broke | How it looked | Cost |
|---|---|---|
| `gh_jid` stripped as a tracking param | Clean URLs | 82 Greenhouse listings lost their identity |
| Guard read the raw URL, key used the canonical one | Guard passing | Wrong jobs admitted |
| Tier-3 text match wrote identity | A successful merge | One job's keys stamped onto another's row |
| Seed re-added keyless jobs each sync | Normal syncs | DB grew to 2,322 against a feed of 2,238 |
| Sub-second `first_seen_at` tiebreak | A sorted list | Order changed between runs |
| SQLite BINARY vs JS `localeCompare` | A sorted list | Server and client disagreed on 14 rows |
| Gmail `metadataHeaders` urlencoded without `doseq` | HTTP 200, message returned | **Every header empty → every bounce classified as a human reply** |
| `"good"` missing from the deliverability mapping | Addresses scored `unknown` | `unknown` is allowed to send, so nothing downstream showed it |
| `smtp_unreachable` read as a verdict | `bad` / `risky` per address | Same domain scored both, seconds apart — it is a failed probe |
| LinkedIn search not constrained by employer | Plausible recruiters | Results at Intuit, Roku and Modo Energy for an Amazon search |
| `emails` (list) read as `email` (string) | No addresses found | Full-price run, zero drafts |
| `plan_sends` clamped past slots to "now" | Scheduled sends | A queue built at 2am sent at 2am, undoing every other guard |
| Verifier probed without controls | A confident split | Nonsense addresses scoring `verified` were invisible |
| Team-suffix rule matched a hyphen inside a word | Tidy subjects | "ASIC Package Engineer Intern Co-op" went out as "… Intern Co" |
| Season quoted when the title already carried it | A correct season | "Summer 2027 SWE Intern - Vehicle Software - Summer 2027" |
| One season asserted over roles from several | A confident sentence | Named Summer 2027, then listed a Spring 2027 posting under it |
| "three notes" hardcoded in the closing phrase | Fine at n=3 | "four roles … rather than send you three notes" |
| Drafts written per posting, not per person | Nine tidy drafts | Three applications at one company sent **one** email; the cooldown held the other eight forever |
| Cooldown applied per send, not per campaign | A working guard | The first note of a batch blocked the other two, collapsing three recruiters to one |
| Roster never topped up | A cache hit | The first application spent all three cached names; every later one found nobody new |
| A new column added to an existing `CREATE TABLE IF NOT EXISTS` | A fresh checkout working | Invisible on any machine whose database already existed — hence `MIGRATIONS` in `db.py` |
| `cursor.lastrowid` trusted after `INSERT OR IGNORE` | A normal insert | Not zero for an ignored row — it is the connection's last rowid, so drafts pointed at contact ids that did not exist |

The last one is the general lesson: **the verification probe only became
trustworthy once it included addresses that could not possibly exist.** When
you add a source or an actor, probe it with a control first.

## Tests

`jobfeed/tests/`, 67 tests, `python -m pytest jobfeed/tests -q`.

Fixtures in `jobfeed/tests/fixtures/` are captured live API responses.
`people_probe.json` has had identities replaced — the shape is what the tests
depend on, and it is preserved exactly: the profiles at the wrong employer,
the multi-word `firstName`, the `emails`-is-a-list structure with its empty
and absent variants, and each address's verdict.

## Known gaps

- **The email notification channel has never run.** SMTP is blocked in the
  sandbox this was built in; it will first execute on the GitHub runner.
- **No outreach send has happened yet.** `dispatch --send` is unexercised
  against a real recipient. The send path itself is verified via a
  self-addressed message.
- Outreach is not wired into any scheduled job. Nothing sends unattended, by
  design — revisit deliberately.
- `intern-list.com` was scoped but never built; Simplify plus Instagram covers
  it for now.

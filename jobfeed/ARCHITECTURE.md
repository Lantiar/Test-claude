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

**Finding recruiters is two passes, not one.** LinkedIn's free-text search is
not a company filter: "Philips recruiter" returned recruiters at Anduril and
Synopsys, a woman whose *surname* is Philips, and two agencies with Philips in
their name — one genuine employee in fifteen results. The actor does have an
exact company filter, but it wants a LinkedIn company URL rather than a name —
and the profiles themselves carry that URL. So one cheap pass learns the
company page, and the second filters on it exactly. Philips went from one
uncontactable person to thirteen employees, ten with addresses.

Two things widen the pool once more, because both are the same dead end: too
few people at the company, or nobody with an address.

Contacts are ranked **reachable first, then whose job it is, then how good the
address is**. Role fit alone put three American Express campus recruiters at
the top with not one address between them; a perfect target nobody can write to
is worth less than a generic one who answers. Among people we can reach, the
campus recruiter wins — `title_rank` puts university and early-careers titles
above technical recruiters, above generic talent acquisition, and marks
seniority *down*: a VP of Talent does not read cold mail from students.

When a campus-level recruiter is found with no address, one dedicated
email-finder call fills the gap — but **only when a colleague's real address
has already taught us the company's mail domain**. Guessing the domain as well
as the local part is two coin flips, and one that lands wrong is a bounce
against your own sending reputation. Its answer is never trusted as verified
either: checked against two people whose addresses were already known it
returned both exactly, but it also returned `rg@aexp.com` marked "valid" —
two initials at a domain that accepts everything, which is what valid means at
a catch-all. `verify` decides that separately.

A **personal mailbox is never used**. A recruiter's work address is a
professional contact; their private Outlook is not, and a cold email there is
visibly scraped whatever it says.

Sources recruiters from Apify (`harvestapi~linkedin-profile-search`, cookie-free
so it never drives your own LinkedIn session), then keeps the addresses the
actor already verified and probes only the rest
(`michael.g~email-verifier-validator`).

Contacts are cached 90 days per company. Re-scraping re-rolls *which* three
people you get, so a second application to the same firm would otherwise write
to a different set of strangers at a company you have already contacted.

Writes drafts. Nothing leaves.

### 1a. `titles` — a model judges, but may only delete

A posting title arrives from the employer and goes into the email verbatim, so
it is the one place text a stranger wrote reaches a recruiter under your name.
Most are fine; a minority carry a fragment that reads as machine output —
`- 2026 Start`, `- 4 Months`, `Plus one semester`.

A regex cannot make this call. In this feed `Geometry and 3D Vision` and
`- 2027 Summer` both contain digits and only one is noise. So the judgement is
a model's — but **its only permitted action is deletion**. The answer is
accepted only if it is a *subsequence* of the original: same words, same order,
nothing introduced. `is_subsequence` is the whole guarantee, and it means a
title can never be written, only chosen from the employer's own words.

That is also what keeps generated prose out of the mail entirely. Every word a
recruiter reads comes from the template or from the posting itself, so there is
no model-written sentence to recognise.

- **Only flagged titles are sent.** A cheap detector (digit runs, requisition
  ids, durations, trailing fragments, shouted words, repeats) fires on about
  10% of the feed. `Geometry and 3D Vision` is never shown to the model at all,
  so its digits are safe by construction rather than by good judgement.
- **A dangling separator is cleaned up by us**, not by asking the model to.
- **Two words is the floor.** "Intern" alone does not name a role.
- **An unsalvageable title is dropped from the list**; if it was the only one,
  the whole draft waits for a human rather than going out naming nothing.
- Seasons are read off the titles **before** trimming, or removing them is what
  makes two postings that disagree look like they agree.

About $0.10 per 1,000 titles.

### 1b. `polish` — a cheap model fixes grammar, and only grammar

Template copy fails mechanically: a doubled space where a field was empty, a
comma splice, a sentence that reads as assembled. A small model
(`gpt-4.1-mini` by default, about **$0.07 per 100 emails**) is good at those.

Measured on nine real drafts: **nine unchanged, none refused**. That is the
expected result and the reason the editor is a safety net rather than a step —
the copy is deterministic and tested, so on a good day there is nothing to do.
Fed a draft with real faults (a doubled space, a lowercase "i", a duplicated
clause) it fixes exactly those and nothing else.

`gpt-4.1-nano` was the first default and returned **HTML on a third of real
drafts** — `<ul>` markup in a plain-text email — which the checker caught every
time. Telling it not to did not help. mini refuses less often, so it is both
better behaved and, after the retries nano forced, no more expensive.

Unsupervised it is also good at *helping* — rounding out a claim, adding a
number that was not there, warming a sentence with a conversation that never
happened. So the model is never trusted on its own. It proposes a revision;
`polish.verify()` decides whether it stayed inside grammar and readability,
and a revision that touched anything else is **discarded whole** and the
original sent instead. The model can subtract nothing and add nothing — it can
only fail to change.

The prompt is **built from the checker's own rules** — the allowed-word list
is generated from `ADDABLE`, the length band from `LENGTH_BAND` — so the two
cannot drift apart. Described in prose instead, revisions came back rejected
for constraints the model was never told, which is a wasted call every time.
A rejected revision is retried **once**, with the checker's actual complaint
fed back; a second failure returns the original. Temperature is 0, and dropped
automatically if the model refuses it, since some newer ones accept only the
default and a model swap should not look like an outage.

`verify` asks three questions:

1. **Did anything immutable move?** Company, every role title, name, school,
   degree, graduation, both URLs, the greeting, the subject bracket — checked
   per field, since a company surviving in the subject says nothing about the
   body.
2. **Did anything appear that was not there before?** Any URL, number, proper
   noun or *word* in the output that is absent from the input did not come
   from the email. Only a closed list of function words may be introduced —
   fixing a run-on needs "and", it never needs "admirer".
3. **Did the text grow?** More than 20% is an addition whatever it claims.

The signature block is **never sent to the model**. Sent and reattached, an
edit that merely reformatted it made the reattach point unfindable and the
block was appended a second time — a mail signed twice, which passed every
check because each required token was present and the length stayed inside the
band. Withheld, it cannot be reformatted at all.

Any failure — no key, an outage, a rate limit, an unparseable answer — returns
the original unchanged. This runs on mail about to be sent, so the fallback is
the text that was already tested, never nothing and never a guess.

### 1c. The board — the tracker's button

The dashboard shows a **▶ reach out** button beside any job marked applied or
beyond. The page cannot do the work — sourcing is Apify, sending is Gmail, both
Python elsewhere — so a click writes an intent to Upstash (`jobfeed:outreach`)
and the scheduled runner acts on it, writing the outcome back.

```
▶ reach out  ->  queued…  ->  reached out ↗  ->  replied ↗
   click        recorded      mail is out      a recruiter answered
```

The link opens the Gmail thread. `held ↻` and `failed ↻` say what stopped it
and can be pressed again.

- **The state is a report, not a hope.** A browser may only ask; declaring
  `reached` is refused from the page, because a record saying mail went out
  when none did is worse than no record.
- **Scoped to the pressed key.** `prepare(only=[...])` — one click is consent
  for one job, and everything else marked applied is left alone.
- **The runner mirrors the tracker's stages first.** It starts from a published
  snapshot with no `application` rows of its own, so without that it sees
  nobody as having applied to anything.
- **Everything in flight is reported on, not just this pass's requests.** Watch
  only the newly-queued and a job reaches "reached out" and can never move to
  "replied".
- **Outreach state lives in Upstash, not in the runner's database.** That
  database is thrown away and reseeded from a published snapshot every run, and
  the snapshot holds the feed only. Left there, a draft written at 09:00 was
  gone by 09:30: the recruiter search paid for again, the batch rescheduled two
  days out again, and no draft ever reaching its send time. `board.save/load`
  carry contacts, drafts, send times, replies and suppression across, keyed by
  company name, job key and address — **never by rowid**, which is handed out
  fresh each seed and would reattach a draft to whichever contact happened to
  hold that id.
- `OUTREACH_SEND=1` in the repository variables is the kill switch. Unset, the
  button drafts and schedules but sends nothing.

### 1d. The outreach dashboard

A second tab on the same page, fed by the same store the runner writes.
Summary tiles (queued, sent, replies, bounces, bounce rate against the 2%
limit) and a row per note: company, recruiter, address and its verification
class, the roles it names, its state, and when it goes or went.

Per row, while it is still in flight: **edit**, **send now**, **move** (pick a
time), **cancel**. Editing opens the subject and body as written and saves
them verbatim — the pipeline drafts the letter, the sender has the last word.
A hand-edited note is marked polished on the runner so the copy editor leaves
it alone; its job is tidying generated text, and "improving" a hand-written
line would undo the edit on the next pass. A sent note cannot be edited: the
dashboard would then disagree with the recruiter's inbox. A cancelled note can be **restored**. A sent one offers only the
Gmail thread — nothing here can unsend, and offering it would be a control
that lies.

Every action is a *request*, queued in `jobfeed:outreach:cmds` and applied by
the runner on its next pass, for the same reason the button is: the database
these act on lives on the runner and is rebuilt each run, so a page editing it
directly would be writing somewhere that no longer exists by the time it
matters. Until the runner has applied one, the row reads "applying…" rather
than showing the change as done. An instruction is cleared only after it has
been applied, so a pass that dies halfway leaves it to be carried out rather
than losing it.

**The detail view is behind the passphrase.** It names real recruiters and
their addresses, and the page sits on a public URL beside a public job feed.
The coarse per-job state, which names nobody, stays open because the job
board's buttons need it.

### Settings, and what they may not touch

The facts a cold email states go stale — a graduation date moves, a portfolio
moves, and the three things worth saying about yourself change every few
months. Editing `profile.py` for that means a commit and a deploy to change one
sentence of a letter, so these live in the store (`jobfeed:outreach:profile`)
and the runner applies them before it renders anything: graduation, portfolio,
LinkedIn, GPA, honours, the three achievements, whether the resume attaches,
and the resume itself.

**Name, school and degree are deliberately not settable.** They are the
identity the whole mail rests on, and a typo in one is not a setting.

The resume is stored as **bytes, not a path** — the runner has no filesystem
that outlives a run, so a path set on a laptop means nothing to it and the
attachment would quietly stop happening. It is refused unless it really begins
`%PDF`, and its filename is reduced to a basename: it names an attachment, and
a value straight from a web form must not be able to point at a file.

A blank field means "whatever the repository says", which is a real state and
must not blank the sentence.

**Saved settings become the default for every note drafted afterwards** — the
bullets, the graduation date, the portfolio. **They do not rewrite notes that
already exist**, deliberately: a settings change would otherwise silently
reword something already read and approved. To bring a queued note up to date,
edit it, or cancel it and press ▶ again.

**Editing works on scheduled notes, not only unsent drafts.** Anything
`draft`, `queued` or `held` can be rewritten and keeps its send time. Only a
sent note refuses, because editing that would make the dashboard disagree with
the recruiter's inbox.

### How the mail is built

Sent as `multipart/mixed` → `multipart/alternative` (plain + HTML) → the
resume. **The HTML part says the same words as the plain one** and exists only
because Gmail, given plain text alone, rewraps it in a proportional font: a
wrapped bullet's second line starts back at the margin, so one three-line
achievement reads as three separate thoughts and the note looks pasted. The
markup states the structure the client was guessing at; a test asserts word
equality between the two parts.

The resume goes on the **first note only** — attached to a follow-up as well,
the same PDF arrives twice in one thread, which reads as a script that forgot
what it had already sent. A missing or unset file sends the note without it:
the text already links to the portfolio, so a file that cannot be found is a
worse email, not a reason to send none. The PDF itself is gitignored.

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

### 5. `followups` — day 4, then five days after that

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
| `OUTREACH_SIGNATURE` | A block under the sign-off. Empty by default — everything it carried is already in the mail |
| `RESUME_PATH` | The PDF attached to a first note. Default `config/files/resume.pdf`, which is gitignored |
| `OPENAI_API_KEY` | The copy editor. Without it, drafts go out exactly as the templates wrote them |
| `OUTREACH_POLISH_MODEL` | Default `gpt-4.1-nano` |
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
| Both follow-up steps keyed off the original send | A queued follow-up | "Following up again" written before the first follow-up was sent, both landing together |
| Follow-ups scheduled themselves individually | A scattered send time | Five landed on one day, three at one company — the burst everything else prevents, down the one path that skipped the spacing |
| Follow-ups subject to the company cooldown | A guard holding | A nudge on an open thread held because a *different* person at that employer was written to |
| Signature sent to the copy editor and reattached | An accepted edit | A reformatted signature made the reattach point unfindable, so the mail went out signed twice |
| A test that corrupted a draft without checking the corruption applied | A passing check | `str.replace` matched nothing, so "unchanged" measured an unmodified draft |
| Seasons read off titles *after* trimming | A clean title | Trimming removed the evidence that two postings disagreed, so the note asserted one season again |
| Our own outbound read back as inbound | A reply arriving | `history.list` reports sent mail as `messageAdded`; the first email sent would match itself, mark the thread replied, cancel every follow-up and show a 100% reply rate |
| `prepare` defaulting to dry-run | Drafts appearing | The default quietly skipped title cleaning, while still paying for the recruiter search |
| `cursor.lastrowid` trusted after `INSERT OR IGNORE` | A normal insert | Not zero for an ignored row — it is the connection's last rowid, so drafts pointed at contact ids that did not exist |

The last one is the general lesson: **the verification probe only became
trustworthy once it included addresses that could not possibly exist.** When
you add a source or an actor, probe it with a control first.

## Tests

`jobfeed/tests/`, 127 tests, `python -m pytest jobfeed/tests -q`.

`jobfeed/tests/e2e_demo.py` walks the whole pipeline on a simulated calendar
against the **real feed**: one posting, three at one company on one day, a
later application, the copy editor, a human reply, a hard bounce, an
out-of-office, follow-ups and the circuit breaker. Only the Apify search and
the Gmail network hop are stubbed; everything between them is production code.
It found both follow-up bugs above, neither of which any unit test was
asking about.

Fixtures in `jobfeed/tests/fixtures/` are captured live API responses.
`people_probe.json` has had identities replaced — the shape is what the tests
depend on, and it is preserved exactly: the profiles at the wrong employer,
the multi-word `firstName`, the `emails`-is-a-list structure with its empty
and absent variants, and each address's verdict.

## What is proven, and what is not

Worth being exact about before this sends to anyone real.

**Exercised against the live services:** the feed itself (running for weeks);
the Apify recruiter search (real names and addresses, for Amazon); address
verification (33 addresses with deliberate controls); title cleaning and the
copy editor (real postings, real drafts); `schedule` and `dispatch --dry-run`;
Gmail send (one self-addressed message, with the RFC `Message-Id` captured);
and Gmail history polling at real volume — 46 real inbound messages, none
falsely classified as a bounce, and our own outbound correctly filtered out.

**Never exercised for real:**

- **No email has been sent to a recruiter.** Zero.
- **No real reply, bounce or auto-reply has been processed.** Every inbound
  test constructs the message itself. The path from a genuine reply through
  `In-Reply-To` matching to a status change has never run. This is the
  largest gap and it cannot be closed without either a real reply or a second
  mailbox to reply from.
- **No follow-up has been sent.**
- **Time is simulated by rewinding stored timestamps**, never by waiting.
  Anything depending on real wall-clock behaviour across days is untested.
- **The recruiter search is proven for one company.** Search quality varies by
  employer; a company with few recruiters on LinkedIn may return nobody, or
  the wrong people. The end-to-end walk stubs it.
- **Deliverability is unknown.** Whether these land in an inbox or a spam
  folder cannot be tested without sending.
- **Nothing runs unattended.** Outreach is in no cron, by design.

A staged first run would close most of this: send one batch to a single
company, watch it, and only widen once a real reply or bounce has been seen
travelling through `watch`.

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

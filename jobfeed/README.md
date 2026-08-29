# jobfeed data

Internship postings from several sources, deduplicated into one list, refreshed
hourly. Published as static JSON so anything can read it.

## The dashboard

`index.html` sits beside the data and fetches it with relative paths, so it
works unchanged wherever these files are served from:

- GitHub Pages: <https://lantiar.github.io/Test-claude/>
- Vercel: import the repo, set the production branch to `jobfeed-data` and the
  framework to "Other". Every hourly push redeploys it. `vercel.json` is in
  the output and stops the JSON being cached.

## Stages, centrally

The Vercel deployment carries `api/stages`, which keeps every application
stage in one place so a laptop and a phone show the same list. Two settings
make it work, both in the Vercel project:

1. **Storage → Upstash Redis**, connected to the project. It injects
   `KV_REST_API_URL` and `KV_REST_API_TOKEN`; the function reads either those
   or the `UPSTASH_REDIS_REST_*` pair.
2. **Environment variable `JOBFEED_PASSPHRASE`.** Anyone may read the stages;
   only someone with the passphrase may change one. Writing is refused
   outright while this is unset -- on a public site, a missing variable must
   not quietly mean "anyone".

The page asks for the passphrase the first time you change a stage and keeps
it in that browser afterwards.

Stages are held as a Redis hash rather than one JSON blob, so two devices
setting two different jobs at the same moment touch two different fields and
neither edit can silently lose the other.

Served anywhere without that API -- GitHub Pages, or the files opened
locally -- the page finds no endpoint and keeps stages in the browser
instead. The footer says which of the two is in force.

## Endpoints

Once GitHub Pages is pointed at the `jobfeed-data` branch:

| file | what | size |
|---|---|---|
| `meta.json` | when this was generated, counts, per-source health | ~1 KB |
| `recent.json` | jobs the employer posted in the last 14 days | ~480 KB |
| `jobs.json` | every open job | ~1.2 MB |
| `links.json` | links from stories that are not job postings | small |
| `jobs.jsonl` | the same jobs, one JSON object per line | ~880 KB |

Served with `Access-Control-Allow-Origin: *`, so a browser viewer can fetch
them directly with no proxy.

Fetch `meta.json` first. It is a kilobyte, it carries `generated_at`, and it
tells a viewer whether the data is fresh before it downloads a megabyte of it.

## A job

```json
{
  "id": 1412,
  "company": "GlossGenius",
  "title": "Engineering Intern",
  "url": "https://job-boards.greenhouse.io/glossgenius/jobs/7978666003",
  "locations": ["NYC"],
  "season": "Summer 2027",
  "category": "Software",
  "sponsorship": "Does Not Offer Sponsorship",
  "ats": "greenhouse",
  "posted_at": "2026-08-26T22:05:00+00:00",
  "posted_at_epoch": 1787868300.0,
  "posted_is_estimate": false,
  "first_seen_at": "2026-08-28T01:52:00+00:00",
  "first_seen_epoch": 1787882297.9,
  "status": "open",
  "sources": ["instagram", "simplify"]
}
```

### The two dates are not the same thing

`posted_at` is when the employer posted the job. `first_seen_at` is when this
feed noticed it. They can be a month apart -- an Apple posting dated 28 July
was shared on Instagram on 28 August.

`posted_is_estimate` says which you are looking at. When it is `true`, no
source knew the employer's date and `posted_at` is this feed's own first
sighting standing in for it: an upper bound, not the release date. Show those
differently -- a `~`, a lighter weight, anything -- or a list headed "posted
this week" will quietly mean "noticed this week".

`sources` names every source that has reported the job. A job in both
`simplify` and `instagram` was matched on the ATS's own posting id, not on its
title.

## Is it fresh?

`meta.json` carries `sources[]`, one entry per source with `last_run_at`,
`items_seen`, `items_new` and `note`. A `note` is a run that had something to
say -- a source that failed, or found stories with no links in them. `null`
means the run was clean.

A viewer should treat data older than a couple of hours as stale and say so.
Instagram stories expire after 24 hours, so a feed that stops updating starts
losing links immediately rather than merely going out of date.


## Keeping it running

The pipeline runs in GitHub Actions every half hour. GitHub's own cron is the
fallback rather than the primary trigger, because it is best-effort and skipped
every slot for the first hour after the workflow was added -- and a scheduler
that silently does not fire is the worst kind in front of a source that
expires.

So cron-job.org calls `workflow_dispatch` on a real schedule:

```
GH_PAT=... CRONJOB_KEY=... bash scripts/setup-trigger.sh
```

`GH_PAT` is a fine-grained token for this repo with exactly two permissions --
**Actions: read and write**, and the Metadata read GitHub adds itself.
`CRONJOB_KEY` comes from cron-job.org under Settings -> API. Neither goes near
the repository: cron-job.org holds the token and sends it as a header.

The script dispatches once before creating the schedule, so a token missing
the right scope fails while someone is watching rather than at 3am. GitHub
answers a permissions problem with 404 rather than 403, so that check is worth
more than it looks.

Both triggers can fire. The workflow refuses to poll again within
`JOBFEED_MIN_MINUTES` (default 20) of the last publish, so a duplicate costs
nothing -- the expensive half of a run is a paid API call, and policing which
trigger is allowed is harder than refusing to do the work twice. A manual run
from the Actions tab always goes through.

### What each poll costs

Measured, not estimated: $0.01505 per Instagram run, identical across every run
so far. Simplify is a raw GitHub file with ETag caching and costs nothing at
any frequency.

| cadence | runs/month | Apify |
|---|---|---|
| hourly | 720 | $10.84 |
| **every 30 min** | **1,440** | **$21.67** |
| every 15 min | 2,880 | $43.34 |
| every 5 min | 8,640 | $130.03 |

The Apify STARTER plan includes $29 of usage, so anything up to about every 25
minutes is covered by it. GitHub Actions is free on a public repository and
QStash's free tier is 500 messages a day, so neither is a factor.


## Being told about new postings

Each run mails whatever appeared since the last mail, as a plain list of
company, title and link.

"New" means new *to this feed*, not recently posted -- Simplify carries jobs
the employer posted weeks ago that this feed met for the first time today, and
an employer's date says nothing about whether you have seen it. So it is a
watermark over first_seen_at, and the watermark travels in meta.json rather
than in the local database: the runner rebuilds from that snapshot every half
hour, and a watermark it forgot would re-announce all 2,249 jobs at once.

The watermark moves only after a send succeeds. A failed send that advanced it
would drop those postings silently, which is worse than a duplicate mail -- one
is noise, the other is a job you never hear about.

Delivery is by whatever is configured; each channel is tried and one failing
does not stop the others. A channel that quietly stopped working looks exactly
like a quiet week, so failures are named rather than swallowed.

**ntfy** (push to a phone, free, no account):

    NTFY_TOPIC           the topic name

The topic *is* the secret -- anyone who knows it can read the notifications
and publish to it -- so it is a random string, and it goes in the repository
secrets like any other credential. Install the ntfy app, subscribe to that
topic, and that is the whole setup.

**Email**:

    MAIL_USER            the Gmail address to send from
    MAIL_APP_PASSWORD    a Google app password, 16 characters, not the
                         account password

and optionally a `NOTIFY_TO` variable if the mail should go somewhere other
than the sending account.

Locally, `jobfeed notify --dry-run` prints what would be sent without sending
it or moving the watermark.

## Tiers

Companies on a curated S/A/B list get a badge on the dashboard and a mark in
the notifications, and tiered postings sort to the top of a notification --
it is read for about two seconds, and the interesting one must not be twelfth.

The lists live in `jobfeed/tiers.py`. A tier is derived at publish time rather
than stored, so editing that file re-badges the whole corpus on the next run
instead of only what arrives afterwards.

Matching is on the normalised company name -- the same function the
deduplicator uses -- and it is exact, with aliases written out. Substring
matching would be shorter and wrong: "Meta" is inside Metabolon and Metagenomi,
"Block" inside Blockdaemon, "Apple" inside Applied Materials. Fuzzy matching is
worse still; the corpus contains a company called Intropic, which is 82%
similar to Anthropic and has nothing to do with it. A badge on the wrong
company is worse than no badge, because the point is to trust it at a glance.

Names carrying a bracketed alias are tried both ways, since either half can be
the listed one -- Susquehanna appears as both "Susquehanna International Group"
and "Susquehanna International Group (SIG)".

486 of 2,244 live postings currently carry a badge: 145 S, 67 A, 274 B.

## Recruiter outreach

Marking a job past `interested` makes it eligible for outreach: the pipeline
finds early-career recruiters at that company, writes a draft each, scatters
them over working days, sends from your own Gmail, and reads the replies.

```
jobfeed outreach prepare          # find recruiters, write drafts (no sending)
jobfeed outreach drafts           # read what would go out
jobfeed outreach schedule         # give each draft a send time
jobfeed outreach dispatch         # dry run; --send to actually send
jobfeed outreach watch            # read replies and bounces
jobfeed outreach followups        # queue day-4 and day-9 nudges
jobfeed outreach status           # counts, and the bounce breaker
```

`dispatch` is the only command that can put mail in front of a stranger and it
will not do so without `--send`.

### What stops it embarrassing you

- **One note per company per day**, and one company per week across
  applications. Three near-identical emails to one recruiting team in an
  afternoon is the failure this is built around.
- **Weekday sends only**, inside 9:00-16:30 in the recipient's day, with
  randomised daily volume, start minute, gaps and per-send jitter.
- **A bounce circuit breaker**: a rolling 7-day hard-bounce rate over 2% (with
  at least 25 sends) pauses everything rather than throttling it.
- **`accept_all` is not `verified`.** Most large employers accept mail for any
  local part, so the probe cannot confirm a mailbox: those get one speculative
  send per company per week, not three.
- **Any reply stops the sequence**, including an out-of-office. A follow-up
  landing after someone has already answered is what turns a polite note into
  a complaint.
- **Drafts are re-checked at send time**, not only when written -- a queued
  draft can sit for a week, and the address may have bounced in between.

### Configuration

`APIFY_TOKEN` for sourcing and verification; `GMAIL_CLIENT_ID`,
`GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` (scopes: `gmail.send` and
`gmail.readonly`) for sending and reading. `OUTREACH_FROM` sets the From
address, `OUTREACH_BRACKET` the subject-line bracket
(default `Prev Google/Zon`).
`APIFY_PEOPLE_ACTOR` and `APIFY_VERIFY_ACTOR` override the actors, since the
Apify store churns.

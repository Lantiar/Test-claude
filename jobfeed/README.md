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

So an external scheduler calls `workflow_dispatch` on a real cron:

```
GH_PAT=... QSTASH_TOKEN=... bash scripts/setup-trigger.sh
```

`GH_PAT` is a fine-grained token for this repo with **Actions: read and
write**; `QSTASH_TOKEN` comes from the Upstash console. Neither goes near the
repository -- QStash holds the token and forwards it as a header. The script
dispatches once first, so a token without the right scope fails immediately
rather than at 3am.

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

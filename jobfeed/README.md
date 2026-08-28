# jobfeed data

Internship postings from several sources, deduplicated into one list, refreshed
hourly. Published as static JSON so anything can read it.

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

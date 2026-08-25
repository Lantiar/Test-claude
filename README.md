# autoapply

Give it a job link. It opens the application, fills every field it can answer
from your profile, verifies what it typed actually landed, and then either
submits it or puts it in a review queue for you.

```bash
python -m autoapply apply https://jobs.lever.co/acme/1234
```

![dashboard](docs/dashboard.png)

## Status

Working prototype with two lanes.

| ATS | Lane | Needs a model? |
|---|---|---|
| Greenhouse, Lever | DOM worker, single page | no |
| Workday | DOM worker, multi-step wizard | no |
| Ashby, and anything else with an ordinary form | generic DOM worker | only for novel questions |
| iCIMS, Oracle Cloud HCM / Taleo, unknown, anything the DOM lane cannot read | browser-use agent + playbook | yes |

Every ATS gets the deterministic DOM pass first, including ones with no
dedicated worker: the generic worker reads labels the way a person does, groups
radio and checkbox sets into one field per question, drives `react-select`-style
comboboxes and uploads files. Ashby fills with no Ashby-specific code. Only when
that discovers nothing does the run fall through to the agent.

The DOM lane is deterministic and free. The agent lane drives
[browser-use](https://github.com/browser-use/browser-use) with a per-ATS
playbook from `autoapply/playbooks/`, and its work is checked by an independent
judge pass (`judge.py`) rather than the agent's own say-so. Without a model
configured, the agent lane queues with a clear reason instead of guessing at
selectors.

Oracle Cloud HCM is what JPMorgan and most large banks use; `*.taleo.net`
routes to the same lane.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
cp config/profile.example.json config/profile.json   # then fill it in
# put your resume where profile.files.resume points
```

`config/profile.json` is gitignored and holds all your PII. Nothing else needs
editing to get started — the default mapper needs no API key.

## The two modes

| Mode | Behaviour |
|---|---|
| `approve` (default) | Fills everything, submits nothing. Every application waits in the queue. |
| `auto` | Fills everything and submits, unless the gate blocks it. |

```bash
python -m autoapply apply <url>                 # uses MODE from .env
python -m autoapply apply <url> --mode auto
python -m autoapply apply <url> --dry-run       # fill + verify, never submit
python -m autoapply queue                       # what's waiting
python -m autoapply stats
```

## What blocks an auto-submit

Only things that make the submission *wrong*, not things that are merely
unreviewed:

- a **required field nothing could answer** — from the profile, the cache, a
  learned answer, or the LLM
- **verification failed** — what we typed isn't what's on the form
- a **CAPTCHA** is on the page — the run can't proceed unattended
- the **daily cap** (`DAILY_SUBMIT_CAP`) or the kill switch (`touch data/STOP`)

Sensitive fields — work authorization, sponsorship, demographics, salary — are
deliberately *not* in that list. They fill from the explicit answers in your
profile. Anything your profile doesn't answer is already caught by the
required-field rule above.

## Dashboard

```bash
python dashboard/app.py          # http://127.0.0.1:8000
```

Each queued application shows the filled values, anything left unanswered, and
a screenshot of the real form. Edit any value inline and hit **Approve &
submit** — it re-drives the form with your edits and submits. Your edits are
remembered: a typed answer is reused the next time the same question appears on
that ATS.

It binds to loopback. The queue holds your PII, screenshots of filled forms, and
your application history — don't expose it without putting auth in front.

## LLM providers

The default (`LLM_PROVIDER=rules`) makes **no network calls at all**: Greenhouse
and Lever field labels are standardized enough that deterministic matching
handles them. A model is only consulted for fields the rules can't place.

```
LLM_PROVIDER=rules       # default, no key needed
LLM_PROVIDER=anthropic   # ANTHROPIC_API_KEY
LLM_PROVIDER=openai      # OPENAI_API_KEY, OPENAI_BASE_URL
LLM_PROVIDER=ollama      # OLLAMA_HOST — nothing leaves the box
```

## Architecture

```
url → router → worker.discover → mapper → worker.fill → verify → gate → submit | queue
                                    ↑                                        ↓
                                  cache ←──────── learned corrections ───────┘
```

| Module | Job |
|---|---|
| `router.py` | URL → ATS. A data table; adding an ATS is one line. |
| `workers/generic.py` | The DOM worker for any ATS without a dedicated one. |
| `mapper.py` | Field → value: rules, then cache, then learned answers, then LLM. |
| `workers/base.py` | Discovery, fill, submit. `WizardWorker` adds multi-step flows. |
| `workers/workday.py` | The wizard: `data-automation-id` containers, button+listbox dropdowns. |
| `workers/agent.py` | browser-use driver for everything without a DOM worker. |
| `playbooks/` | Per-ATS notes handed to the agent as guidance. |
| `verify.py` | Deterministic readback of every field we set. No LLM. |
| `judge.py` | Independent LLM check of an agent fill. The agent never grades itself. |
| `gate.py` | The only place that decides submit vs queue. |
| `store.py` | sqlite: applied log, mapping cache, queue, corrections. |

## Tests

```bash
python -m pytest tests/ -q
```

The e2e tests drive the real pipeline against local fixtures that mimic
Greenhouse and Lever, including a **React-style controlled input** — a filler
that sets `el.value` directly fails those tests, which is the bug the native
setter in `workers/base.py` exists to avoid.

## Docker

```bash
docker compose run --rm worker apply <url>
docker compose up dashboard
```

## Verification status

Greenhouse, Lever and Workday are tested end to end against local fixtures that
replicate their real markup — including a React controlled input and Workday's
button+listbox dropdowns, neither of which a naive filler can drive.

Three live postings have now been dry-run end to end, and each one broke
something the fixtures did not:

| Posting | Result |
|---|---|
| Cloudflare (Greenhouse) | 24 fields discovered, 17 filled |
| Notion (Ashby, via the generic worker) | 23 discovered, 15 filled |
| Blackstone (Workday) | reaches the wizard, stops at the Create Account wall |

What the live markup broke, and the fixtures could not: Workday's SPA render
timing, its `noCaptchaWrapper` reading as a CAPTCHA, byte-exact verification of
values the widget reformats, file inputs that report empty once the upload is
swapped for a filename chip, `react-select` comboboxes that ignore a written
value while still *reading back* as verified, radio and checkbox groups
discovered one-field-per-option, and `#id` selectors against UUID ids that begin
with a digit.

Use `--dry-run` against a new tenant before trusting it: it fills and verifies
but never submits.

### Known limits

- **Account walls.** Most Workday tenants gate the application behind Create
  Account / Sign In. The run stops there and queues with that reason; sign-in
  and account creation are not built.
- **CAPTCHA.** Greenhouse and Ashby both load reCAPTCHA. Detection is correct
  and blocks auto-submit; there is no handoff yet.
- **Questions the profile does not answer.** A required field with no matching
  option and no basis in the profile is deliberately left empty rather than
  guessed, and queues for review. Your answer is remembered for next time.

## Not built yet

Scraping and job matching (this takes a link you supply), Gmail-based
verification codes, automatic account creation, and the live CAPTCHA handoff.
Applications that hit a sign-in wall queue with `sign-in or account creation
required`.

## Credits

- [browser-use](https://github.com/browser-use/browser-use) (MIT) — the agent lane.
- [berellevy/job_app_filler](https://github.com/berellevy/job_app_filler) — its
  Workday field definitions confirmed the `data-automation-id` conventions this
  project's Workday worker relies on.
- [ShipItAndPray/job-apply-ai](https://github.com/ShipItAndPray/job-apply-ai) (MIT)
  and [liruihan000/claude-job-auto-apply](https://github.com/liruihan000/claude-job-auto-apply)
  were read for ATS behaviour notes. The latter carries no licence, so nothing
  was copied from it — the playbooks here are written from scratch.

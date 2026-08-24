# Workday

Hosts: `*.myworkdayjobs.com` (each tenant is its own subdomain: `amat.wd1`, `disney.wd5`).

React SPA, multi-page wizard. Assigning `input.value` directly does not register —
type into fields and Tab out so React's onChange/onBlur fire.

Structure: every field sits in a `div[data-automation-id^="formField-"]`.
- text: `.//input` inside that div
- dropdown: `button[aria-haspopup="listbox"]` — click to open, then pick from the listbox
- searchable dropdown: `div[data-automation-id="multiSelectContainer"]`, options are
  `div[data-automation-id="promptOption"]`
- file upload: `div[data-automation-id="file-upload-drop-zone"]` — set the file on the
  underlying `input[type=file]`, never click the drop zone
- date: separate inputs with `aria-label` of Month / Day / Year
- yes-no: two `input[type=radio]` inside one formField
- section headings are `h4`

Wizard order: My Information → My Experience → Application Questions → Review.
Advance with the bottom Next / Save and Continue button; the last page's button submits.

Notes:
- Many tenants require an account. If a sign-in or create-account screen appears,
  stop and report `needs_auth` — do not attempt to create one.
- Workday's own "Autofill with Resume" parses badly. Prefer filling fields directly.
- Work experience and education sections may already be pre-populated from the
  resume; correct them rather than adding duplicates.

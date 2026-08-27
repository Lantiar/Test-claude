# iCIMS

Hosts: `careers-*.icims.com`, `jobs-*.icims.com`, `*.icims.com`.

The application form is frequently rendered **inside an iframe** — look for an iframe
whose src points at icims.com and work inside it. Field discovery on the top-level
document alone will find nothing.

Multi-page: job description → apply → account/login → form pages → review.
Older portals are plain server-rendered HTML with ordinary `<input>`/`<select>`
elements, which fill normally; newer ones are React.

Notes:
- Many iCIMS portals require an account before the form is reachable. If a sign-in
  or registration screen appears, stop and report `needs_auth`.
- Resume upload sometimes triggers a parse step that rewrites fields — wait for it to
  settle, then correct what it got wrong.

## The email door, and what holds it shut

Some tenants put the application's own first page at a `/login` URL: an email
box, a privacy acceptance and a Next button, with no password anywhere. That is
a door, not a wall — `needs_auth()` distinguishes them by ENTRY_TEXT, and
`pass_entry_step()` walks through it, filling the address and ticking the one
consent the form will not proceed without.

AMD's tenant (`campus-amd.icims.com/jobs/<id>/login`) is one of these, and it is
held by an **hCaptcha**. The door fills correctly — `css_loginName` takes the
address, the required `accept_gdpr` box ticks — and then an hCaptcha widget
(`newassets.hcaptcha.com`) sits between it and the form, alongside a "Please
Enable Cookies to Continue" notice. Runs against it end with `CAPTCHA present`,
which is the accurate outcome, not a bug to route around: a run cannot get past
it unattended, and it is not there to be defeated.

If this link has to be filled, a person needs to open the door once in a browser
the run can then reuse.

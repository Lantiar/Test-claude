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

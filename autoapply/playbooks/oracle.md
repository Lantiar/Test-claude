# Oracle Cloud HCM / Taleo

Hosts: `*.fa.oraclecloud.com/hcmUI/CandidateExperience/*`, `fa-*.fa.ocs.oraclecloud.com`,
`*.taleo.net`. This is what JPMorgan and most large banks use.

Oracle Redwood React SPA. Usually one long page rather than a wizard, but Taleo
variants are multi-step.

Quirks that matter:
- Dropdowns are custom comboboxes, **not** `<select>`. Click to open, wait for
  `[role="option"]` to render, then click the option by its exact text.
- Yes/No questions render as `<button>` elements, not radios.
- File upload goes through a `<label>` that opens a file chooser — set the file on
  the `input[type=file]` directly.
- There is often an e-signature text field near the bottom that wants your full name typed.
- Taleo adds "knockout" screening questions early in the flow; answer them from the
  profile and do not guess.

Login: these portals usually require an account. If a sign-in or registration screen
appears, stop and report `needs_auth`.

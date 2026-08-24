# Ashby

Hosts: `jobs.ashbyhq.com/<org>/<job-id>/application`.

Modern React SPA, single page, clean markup with real `<label>` elements — the
easiest of the non-Greenhouse ATSs.

Notes:
- The location field is a typeahead: type slowly, wait for the dropdown, click the
  match. Format is "City, State, Country".
- Resume upload may kick off an "Analyzing resume" step. Wait for it to finish before
  submitting or the submit is rejected.
- Custom screening questions sit at the bottom as ordinary radios, checkboxes and
  textareas.

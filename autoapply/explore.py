"""Work out how to drive a control nobody wrote code for, then remember it.

Every widget this project has met so far cost a round of hand-written support:
Workday's button+listbox, then its multiselect, then the fact that the
multiselect nests two levels deep and its top level is categories rather than
answers. Each fix was correct and none of them generalises -- the next tenant's
odd control needs another one.

So instead: when the filler cannot set a field, look at what is actually on
screen and ask the model which thing to click, execute that, look again, and
repeat until the field holds a value. That is a computer-use loop over the page
the worker already has -- the session, the sign-in and the wizard position are
all still ours, which a separate agent browser could not say.

The output is not just a filled field. It is the sequence of labels that
worked, stored the same way a human correction is stored, so the next run
replays it directly and never asks a model about that field again. A widget
defeats this project once.

What it deliberately will not do is invent facts: the model is told to choose
among what the page offers, and the answer it settles on is verified from the
DOM afterwards like any other.
"""
from __future__ import annotations

import json
import re

from . import log as _log
from .clicking import click as _click
from .models import Field

# What a person can see and click near a control: the field's own container,
# and whatever menu opened as a result of touching it.
CANDIDATES_JS = r"""
(root) => {
  const out = [];
  const seen = new Set();
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();

  const scopes = [root];
  document.querySelectorAll(
    '[role=listbox], [data-automation-id=promptOption], [role=menu], [role=dialog]'
  ).forEach(n => { const s = n.closest('[role=listbox],[role=menu],[role=dialog]') || n;
                   if (!scopes.includes(s)) scopes.push(s); });

  for (const scope of scopes) {
    if (!scope || !scope.querySelectorAll) continue;
    const nodes = scope.querySelectorAll(
      'button, [role=button], [role=option], [role=checkbox], [role=radio],' +
      ' a[href], input, textarea, select, li, [data-automation-id]');
    for (const n of nodes) {
      const r = n.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      const label = clean(n.getAttribute('aria-label') || n.innerText ||
                          n.getAttribute('placeholder') || n.value);
      if (!label || label.length > 90) continue;
      const key = n.tagName + '|' + label;
      if (seen.has(key)) continue;
      seen.add(key);
      // Tag the node so the click can find this exact element again. Looking
      // it back up by innerText cannot work: an <input> has none -- its label
      // here came from aria-label, placeholder or value -- so every
      // input-backed control failed the lookup and the loop broke without a
      // word. That is most of them, including the dropdown button this tier
      // exists to drive. Matching on text also picks the first node with that
      // text anywhere on the page, which need not be the one in scope.
      n.setAttribute('data-autoapply-pick', String(out.length));
      out.push({i: out.length, tag: n.tagName.toLowerCase(), label: label,
                aid: n.getAttribute('data-automation-id') || '',
                role: n.getAttribute('role') || ''});
      if (out.length >= 40) return out;
    }
  }
  return out;
}
"""

SYSTEM = (
    "You are operating one control on a job application form, by choosing what "
    "to click next.\n"
    "You get the question, the candidate's profile, what the control currently "
    "shows, and the things visible on screen right now.\n"
    "Reply with JSON only: {\"click\": <index>, \"done\": false} to click "
    "something, or {\"done\": true} when the control already holds a real "
    "answer.\n"
    "Rules:\n"
    "- Choose only from the listed items, by index.\n"
    "- Some menus are nested: a category has to be opened before the real "
    "answer appears underneath it. Opening one is progress, not an answer.\n"
    "- Pick the answer that is true for this candidate. Where the profile's "
    "answer is not offered, pick the closest offered one; never invent a fact "
    "about them.\n"
    "- Do not click Save, Continue, Submit, Next, Back, or anything that "
    "leaves the page. You are filling one field, not advancing the form.\n"
)

# Clicking any of these ends the run rather than filling the field.
NAVIGATION = ("save and continue", "continue", "next", "submit", "back",
              "cancel", "sign out", "previous")


def _is_navigation(label: str) -> bool:
    """Would clicking this leave the field behind?

    Compared on whole words. A bare prefix test refuses "Backend Engineer" as
    if it were the Back button, and a job title is exactly the kind of thing
    this loop is picking from.
    """
    low = re.sub(r"[^a-z0-9 ]", " ", (label or "").lower()).strip()
    return any(low == n or low.startswith(n + " ") for n in NAVIGATION)


def solve_field(worker, field: Field, profile: dict, provider,
                read_value, max_steps: int = 6) -> tuple[str, list[str]]:
    """Fill one stubborn field by looking and clicking. Returns (value, path).

    `read_value` reports what the control currently holds, so success is judged
    from the DOM rather than from the model's own account of it.
    """
    log = _log.get("explore")
    if provider is None or getattr(provider, "name", "rules") == "rules":
        return "", []

    ctx = worker.frame_for(field)
    path: list[str] = []
    clicked: set[str] = set()

    for step in range(max_steps):
        container = ctx.query_selector(field.selector)
        if container is None:
            log.debug("%s: selector %s matches nothing", field.label,
                      field.selector)
            break
        try:
            root = container.evaluate_handle(
                "el => el.closest('[data-automation-id^=formField-]') "
                "|| el.parentElement").as_element()
            items = ctx.evaluate(CANDIDATES_JS, root or container)
        except Exception as exc:
            log.debug("could not read the page: %s", exc)
            break
        if not items:
            log.debug("%s: nothing clickable on screen", field.label)
            break

        current = read_value()
        try:
            raw = provider._chat(SYSTEM, json.dumps({
                "question": field.label,
                "currently_shows": current,
                "profile": profile,
                "on_screen": items,
            }, indent=2))
        except Exception as exc:
            log.debug("model unavailable: %s", exc)
            break

        start, end = raw.find("{"), raw.rfind("}")
        if start < 0:
            log.debug("%s: model replied without JSON: %s", field.label,
                      _log.brief(raw, 80))
            break
        try:
            decision = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            log.debug("%s: model replied with bad JSON: %s", field.label,
                      _log.brief(raw[start:end + 1], 80))
            break

        if decision.get("done"):
            if path:
                log.info("%s solved in %d click(s): %s -> %s", field.label,
                         len(path), " > ".join(path), _log.brief(current, 40))
            else:
                log.debug("%s: model says the control is already answered "
                          "(shows %r)", field.label, current)
            break
        index = decision.get("click")
        if not isinstance(index, int) or not 0 <= index < len(items):
            log.debug("%s: model chose index %r, which is not on offer",
                      field.label, index)
            break

        choice = items[index]
        # Clicking the same thing again is not progress. On Oracle's "City,
        # state, country" the model chose "Search by Location" six times in a
        # row, and the loop reported the field solved with a recipe reading
        # "Search by Location > Search by Location > ..." six times -- which was
        # then taught, so every later run would replay that nonsense in
        # preference to the rules beneath it.
        if choice["label"] in clicked:
            log.debug("%s: model chose %r again; stopping rather than looping",
                      field.label, choice["label"])
            break
        clicked.add(choice["label"])
        if _is_navigation(choice["label"]):
            # The model reaching for Save and Continue means it thinks the field
            # is done. Advancing here would leave the step half-filled.
            log.debug("refused to click navigation: %s", choice["label"])
            break

        target = ctx.query_selector(f'[data-autoapply-pick="{index}"]')
        if target is None:
            log.debug("chose %r but the element is gone", choice["label"])
            break
        if not _click(target):
            log.debug("chose %r but it would not click", choice["label"])
            break
        before = {i["label"] for i in items}
        path.append(choice["label"])
        log.debug("step %d: clicked %r", step + 1, choice["label"])
        worker.page.wait_for_timeout(800)

        # "The displayed value changed" is not success. Opening a category
        # changes what the control shows while committing nothing -- the first
        # live run of this stopped at "Job Board" and reported it solved, with
        # the real answer still one level down. Options that were not there a
        # moment ago mean we descended, so keep going.
        try:
            after = {i["label"] for i in ctx.evaluate(CANDIDATES_JS, root or container)}
        except Exception:
            after = before
        if after - before:
            log.debug("  descended a level (%d new choice(s))", len(after - before))
            continue

        value = read_value()
        if value and value != current:
            # Changed, not necessarily answered. This returned here, so any
            # non-empty value the control came to hold was the answer -- and
            # what this tier returns is taught, replayed with confidence 1.0 on
            # every future run, and outranks every rule beneath it. A wrong
            # lesson is worse than no lesson.
            #
            # So go round once more instead. The next pass shows the model the
            # value it just produced under "currently_shows", and its own
            # "done" then means it looked at the result and accepted it, rather
            # than meaning nobody checked. It can also click again from there,
            # which is how a nested menu gets past the category it opened.
            log.debug("%s: now shows %r after %s -- confirming",
                      field.label, _log.brief(value, 40), " > ".join(path))
            continue
        log.debug("%s: clicked %r, control still shows %r", field.label,
                  choice["label"], value)

    final = read_value()
    if not final:
        # The tier whose job is to adapt to a widget nobody wrote code for
        # gave up. Saying so is the difference between a bug that gets fixed
        # and one that gets worked around somewhere upstream.
        log.info("%s: could not work it out in %d step(s)%s", field.label,
                 max_steps, f" (tried {' > '.join(path)})" if path else "")
    return final, path

"""Review dashboard.

Binds to loopback by default: the queue holds your PII, screenshots of filled
forms, and your application history. Set DASHBOARD_HOST deliberately if you
ever want it reachable from another machine.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from autoapply.pipeline import apply_to, load_profile   # noqa: E402
from autoapply.store import Store                       # noqa: E402

app = FastAPI(title="autoapply")
templates = Jinja2Templates(directory=str(pathlib.Path(__file__).parent / "templates"))
store = Store()


def _rows():
    out = []
    for r in store.queue_list():
        mappings = json.loads(r["mappings_json"] or "[]")
        out.append({
            "id": r["id"], "url": r["url"], "ats": r["ats"],
            "company": r["company"] or "-", "title": r["title"] or "",
            "reasons": json.loads(r["reasons_json"] or "[]"),
            "created_at": r["created_at"],
            "has_shot": bool(r["screenshot_path"]),
            "filled": [m for m in mappings if m["action"] in ("fill", "generate")],
            "unknown": [m for m in mappings if m["action"] == "unknown"],
        })
    return out


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(
        request, "index.html",
        {"items": _rows(), "stats": store.stats(),
         "mode": os.getenv("MODE", "approve")},
    )


@app.get("/shot/{qid}")
def shot(qid: int):
    row = store.queue_get(qid)
    if row and row["screenshot_path"] and os.path.exists(row["screenshot_path"]):
        return FileResponse(row["screenshot_path"])
    return RedirectResponse("/", status_code=303)


@app.post("/queue/{qid}/approve")
async def approve(qid: int, request: Request):
    """Re-drive the form with any edits, then submit. Edits also train the cache."""
    row = store.queue_get(qid)
    if row is None:
        return RedirectResponse("/", status_code=303)

    form = await request.form()
    overrides = {k[6:]: v for k, v in form.items() if k.startswith("value:")}

    # apply_to drives the sync Playwright API, which cannot run on the event
    # loop — hand it to a worker thread.
    result = await run_in_threadpool(
        apply_to, row["url"], mode="auto", store=store,
        profile=load_profile(), ats_override=row["ats"], overrides=overrides,
    )
    store.resolve_queue(qid, f"approved -> {result.status}")
    return RedirectResponse("/", status_code=303)


@app.post("/queue/{qid}/skip")
def skip(qid: int):
    store.resolve_queue(qid, "skipped")
    return RedirectResponse("/", status_code=303)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("DASHBOARD_HOST", "127.0.0.1"),
                port=int(os.getenv("DASHBOARD_PORT", "8000")))

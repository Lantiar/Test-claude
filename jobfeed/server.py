"""A local viewer: the job list, and where you are with each one.

Local on purpose. The published feed is public and derived; this shows it
alongside the one thing that is neither -- your own application status -- so it
runs on your machine, against your database, and nothing it knows leaves it.

Standard library only, like the rest of jobfeed: http.server is more than
enough for one reader on localhost, and a dependency here would be a
dependency in the scheduled runner too.
"""
from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import apply as _apply
from . import db as _db

HERE = os.path.dirname(__file__)


def rows(con) -> list[dict]:
    stages = _apply.all_stages(con)
    out = []
    for r in con.execute(
            "SELECT j.*, c.name AS company, "
            " (SELECT GROUP_CONCAT(DISTINCT s.source) FROM sighting s "
            "   WHERE s.job_id=j.id) AS sources "
            "FROM job j LEFT JOIN company c ON c.id=j.company_id "
            "WHERE j.status='open' ORDER BY j.posted_at DESC"):
        key = _apply.job_key(r)
        app = stages.get(key)
        out.append({
            "key": key,
            "company": r["company"] or "—",
            "title": r["title"] or "(untitled)",
            "url": r["canonical_url"],
            "locations": json.loads(r["locations"] or "[]"),
            "season": r["season"] or "",
            "ats": r["ats"] or "",
            "posted_at": r["posted_at"],
            "posted_is_estimate": bool(r["posted_at_is_estimate"]),
            "sources": sorted((r["sources"] or "").split(",")) if r["sources"] else [],
            "stage": (app or {}).get("stage") or "",
            "note": (app or {}).get("note") or "",
        })
    return out


class Handler(BaseHTTPRequestHandler):
    db_path: str | None = None

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "viewer.html"), "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        if path == "/api/jobs":
            con = _db.connect(self.db_path)
            try:
                payload = {
                    "jobs": rows(con),
                    "stages": list(_apply.STAGES),
                    "counts": _apply.counts(con),
                    "generated_at": time.time(),
                }
            finally:
                con.close()
            return self._send(200, json.dumps(payload).encode())
        return self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        if urlparse(self.path).path != "/api/stage":
            return self._send(404, b'{"error":"not found"}')
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, b'{"error":"expected JSON"}')
        key, stage = body.get("key"), body.get("stage")
        if not key:
            return self._send(400, b'{"error":"which job? send a key"}')
        con = _db.connect(self.db_path)
        try:
            if not stage:
                _apply.clear(con, key)
                result = {"key": key, "stage": ""}
            else:
                try:
                    result = _apply.set_stage(con, key, stage, body.get("note"))
                except ValueError as exc:
                    return self._send(400, json.dumps({"error": str(exc)}).encode())
            counts = _apply.counts(con)
        finally:
            con.close()
        return self._send(200, json.dumps({"saved": result, "counts": counts}).encode())

    def log_message(self, fmt, *args):
        pass            # one line per fetch is noise for a local single-user tool


def serve(db_path: str | None, port: int = 8765, host: str = "127.0.0.1") -> None:
    Handler.db_path = db_path
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"jobfeed viewer on http://{host}:{port}  (ctrl-c to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


def render(con, out: str = "dashboard.html") -> dict:
    """The same viewer as one self-contained file, data baked in.

    The server is the better tool when it can be reached; it cannot always be
    reached. A session running in a container serves 127.0.0.1 to itself and
    nobody else, so the useful artefact there is a file: the same page with the
    job list inlined and no fetch, openable from anywhere and mailable to a
    phone.

    Read-only about stages by design rather than by omission. The database is
    where a stage lives, and a copy of the page writing to its own browser
    storage would be a second, quieter answer to the same question -- fine
    until the two disagree and neither knows it. The page shows the stages the
    database holds; changing one goes through the database.
    """
    jobs = rows(con)
    with open(os.path.join(HERE, "viewer.html")) as fh:
        html = fh.read()
    payload = json.dumps({
        "jobs": jobs, "stages": list(_apply.STAGES), "counts": _apply.counts(con),
        "generated_at": time.time(),
    })
    html = html.replace(
        'async function load(){\n  const r = await fetch("/api/jobs");\n  const d = await r.json();',
        'const BAKED = ' + payload + ';\nasync function load(){\n  const d = BAKED;')
    # Without a server there is nothing to save to, so the control that implies
    # otherwise is disabled rather than left to fail silently on click.
    html = html.replace('<select class="stage', '<select disabled class="stage')
    html = html.replace(
        "Stage is stored locally and is never published.",
        "Stage is read from the database and is never published. This is a "
        "snapshot: to change a stage, run the viewer or ask for it to be set.")
    with open(out, "w") as fh:
        fh.write(html)
    return {"jobs": len(jobs), "bytes": len(html), "path": out}

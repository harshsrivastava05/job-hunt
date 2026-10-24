"""The laptop app: the watcher in a background thread + the queue page at http://127.0.0.1:8000.

Usage: python -m jobhunt.app          # with a console; logs to the terminal
       pythonw -m jobhunt.app         # what the Windows login task runs: no window, logs to ~/.jobhunt/jobhunt.log
       pythonw -m jobhunt.app --open  # the desktop shortcut: start it if it isn't running, then open the page

Bound to 127.0.0.1, so only this laptop can reach it. For the phone: `tailscale serve --bg 8000` shares it with
your own Tailscale devices only.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from jobhunt import decide, importer, resume, worker
from jobhunt.db import DB_PATH, connect, now

PORT = int(os.environ.get("JOBHUNT_PORT", "8000"))
PAGE = Path(__file__).parent / "index.html"
INR_PER_USD = 96.5  # 2026-10-24; only for showing spend in rupees
ACTIONS = {"queued", "opened", "applied", "skipped"}
NUMBERS = {"min_score": (0, 100), "max_age_days": (1, 30), "retention_days": (3, 90), "interval_min": (15, 24 * 60)}
WORDS = {"title_include", "title_exclude"}
LISTED = "EXISTS (SELECT 1 FROM jobs j WHERE j.source_id = s.id AND j.last_seen_at = s.last_ok_at)"


def jobs(conn):
    """Everything that reached the queue. Expired skips drop out; applied jobs stay as history."""
    rows = conn.execute(
        "SELECT j.id, j.title, j.location, j.url, j.posted_at, j.first_seen_at, j.score, j.verdict, j.apply_status, j.applied_at,"
        " (SELECT group_concat(DISTINCT name) FROM companies WHERE source_id = j.source_id) AS company"
        " FROM jobs j WHERE j.apply_status IS NOT NULL AND (j.status != 'expired' OR j.apply_status = 'applied')"
        " ORDER BY j.score DESC, coalesce(j.posted_at, j.first_seen_at) DESC"
    ).fetchall()
    return [{**dict(r), "verdict": json.loads(r["verdict"] or "{}")} for r in rows]


def status(conn):
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    day = now(datetime.now(timezone.utc) - timedelta(days=1))
    cv = conn.execute("SELECT filename, uploaded_at FROM resumes WHERE active = 1 ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "companies": one("SELECT count(*) FROM companies"),
        "companies_with_jobs": one(f"SELECT count(*) FROM companies c JOIN sources s ON s.id = c.source_id WHERE s.active = 1 AND {LISTED}"),
        "failing": one("SELECT count(*) FROM sources s WHERE active = 1 AND fail_count > 0 AND EXISTS (SELECT 1 FROM companies c WHERE c.source_id = s.id)"),
        "last_check": one("SELECT max(last_ok_at) FROM sources"),
        "new_24h": one("SELECT count(*) FROM jobs WHERE first_seen_at >= ? AND status != 'baseline'", day),
        "matched_24h": one("SELECT count(*) FROM jobs WHERE first_seen_at >= ? AND status = 'matched'", day),
        "usage": {"24 h": usage(conn, day), "30 days": usage(conn, "")},  # llm_calls keeps 30 days
        "inr_per_usd": INR_PER_USD,
        "provider": decide.PROVIDER,
        "resume": dict(cv) if cv else None,
        "settings": {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM settings")},
        "db": DB_PATH,
    }


def usage(conn, since):
    """Decision-model requests per provider since a time: jobs judged, links checked on company pages, cost."""
    return [dict(r) for r in conn.execute(
        "SELECT provider, count(*) AS requests, sum(kind = 'judge') AS jobs, coalesce(sum(CASE WHEN kind = 'extract' THEN questions END), 0) AS links,"
        " coalesce(sum(usd), 0) AS usd FROM llm_calls WHERE at >= ? GROUP BY provider ORDER BY provider", (since,),
    )]


def sources(conn):
    return [dict(r) for r in conn.execute(
        "SELECT s.id, s.url, s.ats, s.active, s.last_ok_at, s.fail_count, s.last_error,"
        " (SELECT group_concat(DISTINCT name) FROM companies WHERE source_id = s.id) AS company,"
        " (SELECT count(*) FROM jobs j WHERE j.source_id = s.id AND j.last_seen_at = s.last_ok_at) AS listed"
        " FROM sources s WHERE EXISTS (SELECT 1 FROM companies c WHERE c.source_id = s.id) ORDER BY s.fail_count > 0 DESC, company"
    )]


def act(conn, job_id, apply_status):
    if apply_status not in ACTIONS:
        raise ValueError(f"apply_status must be one of {sorted(ACTIONS)}")
    with conn:
        n = conn.execute(
            "UPDATE jobs SET apply_status = ?, applied_at = CASE WHEN ? = 'applied' THEN ? END WHERE id = ? AND apply_status IS NOT NULL",
            (apply_status, apply_status, now(), job_id),
        ).rowcount
    if not n:
        raise ValueError(f"job {job_id} isn't in the queue")
    return {"ok": True}


def edit_source(conn, source_id, body):
    with conn:
        if "active" in body:
            conn.execute("UPDATE sources SET active = ? WHERE id = ?", (1 if body["active"] else 0, source_id))
        if body.get("check"):  # due on the watcher's next pass, within a minute
            conn.execute("UPDATE sources SET next_check_at = NULL WHERE id = ?", (source_id,))
    return {"ok": True}


def save_settings(conn, body):
    clean = {}
    for k, v in body.items():
        if k in NUMBERS:
            lo, hi = NUMBERS[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise ValueError(f"{k} must be a number from {lo} to {hi}")
            clean[k] = int(v)
        elif k in WORDS:
            if not isinstance(v, list) or not all(isinstance(w, str) for w in v):
                raise ValueError(f"{k} must be a list of words")
            clean[k] = list(dict.fromkeys(w.strip().lower() for w in v if w.strip()))
        else:
            raise ValueError(f"unknown setting {k}")
    with conn:  # the watcher re-applies them to already-judged jobs on its next pass (judge.resettle)
        conn.executemany("UPDATE settings SET value = ? WHERE key = ?", [(json.dumps(v), k) for k, v in clean.items()])
    return {"ok": True}


def upload_resume(conn, name, data):
    rid, requeued = resume.add(conn, Path(name).name or "resume.pdf", data)
    return {"ok": True, "message": f"Resume #{rid} is active. {requeued} recent jobs will be re-judged against it."}


def import_companies(conn, name, data):
    suffix = Path(name).suffix.lower()
    if suffix not in (".csv", ".xlsx", ".xlsm"):
        raise ValueError("expected a .csv or .xlsx file")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / f"import{suffix}"
        path.write_bytes(data)
        stats = importer.import_file(conn, path)
    return {"ok": True, "message": ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in stats.items()) or "nothing to import"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # a line per request is noise in the log file
        pass

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _send(self, code, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _route(self, method):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost") and not host.endswith(".ts.net"):
            return self._send(403, {"error": "unknown host"})  # DNS rebinding: someone else's domain pointed at 127.0.0.1
        if method == "POST" and self.headers.get("X-Jobhunt") != "1":
            return self._send(403, {"error": "missing X-Jobhunt header"})  # other sites can't set it without a CORS preflight
        url = urlsplit(self.path)
        parts, query = url.path.strip("/").split("/"), {k: v[0] for k, v in parse_qs(url.query).items()}
        size = int(self.headers.get("Content-Length") or 0)
        if size > 20_000_000:
            return self._send(413, {"error": "file too large"})
        raw = self.rfile.read(size) if method == "POST" else b""
        conn = connect()
        try:
            match method, parts:
                case "GET", [""]:
                    return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                case "GET", ["icon.png"]:
                    return self._send(200, (PAGE.parent / "icon.png").read_bytes(), "image/png")
                case "GET", ["api", "jobs"]:
                    return self._send(200, jobs(conn))
                case "GET", ["api", "status"]:
                    return self._send(200, status(conn))
                case "GET", ["api", "sources"]:
                    return self._send(200, sources(conn))
                case "POST", ["api", "jobs", job_id]:
                    return self._send(200, act(conn, int(job_id), json.loads(raw).get("apply_status")))
                case "POST", ["api", "sources", source_id]:
                    return self._send(200, edit_source(conn, int(source_id), json.loads(raw)))
                case "POST", ["api", "settings"]:
                    return self._send(200, save_settings(conn, json.loads(raw)))
                case "POST", ["api", "resume"]:
                    return self._send(200, upload_resume(conn, query.get("name", ""), raw))
                case "POST", ["api", "import"]:
                    return self._send(200, import_companies(conn, query.get("name", ""), raw))
            self._send(404, {"error": "not found"})
        except SystemExit as e:  # the importer's CLI-style errors (missing columns)
            self._send(400, {"error": str(e.code)})
        except Exception as e:  # bad input, unreadable PDF, ...: tell the page instead of dropping the connection
            self._send(400, {"error": str(e) or type(e).__name__})
        finally:
            conn.close()


class Server(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"  # on Windows it would let a second copy share the port instead of failing
    daemon_threads = True


def _up():
    try:
        return httpx.get(f"http://127.0.0.1:{PORT}/api/status", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


def open_page():
    """Desktop shortcut: make sure the app runs (through its login task, so there's still only one), then open the page.
    Without the task (not installed, or disabled) this process becomes the app."""
    url = f"http://127.0.0.1:{PORT}/"
    if not _up():
        subprocess.run(["schtasks", "/run", "/tn", "Job Hunt"], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(20):
            if _up():
                break
            time.sleep(1)
    if _up():
        webbrowser.open(url)
        return
    threading.Timer(3, webbrowser.open, [url]).start()
    main()


def main():
    if sys.stdout is None:  # pythonw: no console, so log to a file next to the DB (rolled over at 5 MB)
        log = Path(DB_PATH).parent / "jobhunt.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        if log.exists() and log.stat().st_size > 5_000_000:
            log.replace(log.with_suffix(".log.1"))
        sys.stdout = sys.stderr = open(log, "a", buffering=1, encoding="utf-8")
    server = Server(("127.0.0.1", PORT), Handler)  # raises if a copy is already running: one watcher only
    threading.Thread(target=worker.loop, name="watcher", daemon=True).start()
    print(f"{now()} jobhunt running: http://127.0.0.1:{PORT}  (db {DB_PATH})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    open_page() if "--open" in sys.argv else main()

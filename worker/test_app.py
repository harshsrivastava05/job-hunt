"""Run: python test_app.py (or pytest). The local API: host and cross-site guards, queue actions, settings, import."""
import os
import tempfile
import threading

os.environ["JOBHUNT_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")  # before jobhunt.db reads it

import httpx  # noqa: E402

from jobhunt.app import Handler, Server  # noqa: E402
from jobhunt.db import connect, now  # noqa: E402

OK = {"X-Jobhunt": "1"}


def test_api():
    conn = connect()
    conn.execute("INSERT INTO sources (id, url) VALUES (1, 'https://acme.com/careers')")
    conn.execute("INSERT INTO companies (name, source_id) VALUES ('Acme', 1)")
    conn.execute(
        "INSERT INTO jobs (id, source_id, external_id, url, title, first_seen_at, last_seen_at, status, score, verdict, apply_status)"
        " VALUES (1, 1, 'a', 'https://acme.com/a', 'SDE', ?, ?, 'matched', 47, '{\"fit\": 0.47}', 'queued')", (now(), now()))
    conn.commit()
    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    assert "Job Hunt" in httpx.get(base + "/").text
    assert [(j["title"], j["company"], j["verdict"]["fit"]) for j in httpx.get(base + "/api/jobs").json()] == [("SDE", "Acme", 0.47)]
    assert httpx.get(base + "/api/jobs", headers={"Host": "evil.example"}).status_code == 403  # DNS rebinding
    assert httpx.post(base + "/api/jobs/1", json={"apply_status": "applied"}).status_code == 403  # a form on another site
    assert httpx.post(base + "/api/jobs/1", json={"apply_status": "applied"}, headers=OK).status_code == 200
    assert tuple(conn.execute("SELECT apply_status, applied_at IS NOT NULL FROM jobs").fetchone()) == ("applied", 1)
    assert httpx.post(base + "/api/jobs/1", json={"apply_status": "hired"}, headers=OK).status_code == 400

    assert httpx.post(base + "/api/settings", json={"min_score": 500}, headers=OK).status_code == 400
    assert httpx.post(base + "/api/settings", json={"surprise": 1}, headers=OK).status_code == 400
    assert httpx.post(base + "/api/settings", json={"min_score": 40, "title_exclude": ["Senior", " lead ", ""]}, headers=OK).status_code == 200
    settings = httpx.get(base + "/api/status").json()["settings"]
    assert (settings["min_score"], settings["title_exclude"]) == (40, ["senior", "lead"]), settings

    r = httpx.post(base + "/api/import?name=more.csv", content=b"company_name,careers_url,city\nNewCo,newco.com/careers,Pune\n", headers=OK)
    assert r.status_code == 200 and "1 new sources" in r.json()["message"], r.text
    assert httpx.post(base + "/api/import?name=bad.csv", content=b"name,url\nx,y\n", headers=OK).status_code == 400  # missing columns
    assert httpx.post(base + "/api/resume?name=cv.pdf", content=b"not a pdf", headers=OK).status_code == 400
    server.shutdown()


if __name__ == "__main__":
    test_api()
    print("ok test_api")

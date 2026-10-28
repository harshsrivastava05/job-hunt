"""Run: python test_judge.py (or pytest). Offline: the free gates, retention, resume re-judging."""
import json
import re
from datetime import datetime, timedelta, timezone

from jobhunt import decide, judge
from jobhunt.db import SCHEMA, connect, now
from jobhunt.judge import gate, judge_pending, purge
from jobhunt.resume import add

UTC = datetime.now(timezone.utc)
CFG = {k: json.loads(re.search(r"\('%s',\s*'([^']*)'\)" % k, SCHEMA)[1]) for k in ("title_include", "title_exclude")}
CFG.update(max_age_days=3)


def _job(title="Backend Engineer", location="", posted_days=1):
    return {"title": title, "location": location, "posted_at": now(UTC - timedelta(days=posted_days)), "first_seen_at": now(UTC)}


def test_gates():
    assert gate(_job(), CFG, UTC) is None
    assert gate(_job(posted_days=5), CFG, UTC) == ("stale", "older than max_age_days")  # never reaches the paid judge
    assert gate({**_job(), "posted_at": None, "first_seen_at": now(UTC - timedelta(days=4))}, CFG, UTC)[0] == "stale"
    for loc in ("", "India", "Bengaluru, Karnataka, India", "Pune, Maharashtra, India", "Gurugram", "Remote"):
        assert gate(_job(location=loc), CFG, UTC) is None, loc  # anywhere in India
    for title in ("SDE 1", "SDE-1", "AI Engineer", "Software Developer Intern", "Data Engineer", "Member of Technical Staff", "AI or ML Engineer 2395441"):
        assert gate(_job(title), CFG, UTC) is None, title
    for title in ("Senior Backend Engineer", "Sr. Software Engineer", "Engineering Manager", "Principal Engineer", "Account Executive", "Chartered Accountant",
                  "SDE-2", "SDE II", "Software Engineer II", "Software Engineer - II, AI/ML", "Technical Support Engineer-2", "Platform Software Engineer 2", "Software Engineer III"):
        assert gate(_job(title), CFG, UTC) == ("filtered", "title"), title


def test_job_page_date_stops_old_undated_jobs():
    calls = []
    real = judge.describe, decide.decide
    judge.describe = lambda row: row["jd"]
    decide.decide = lambda *a: calls.append(1) or {"fit": 0.9}
    try:
        cutoff = UTC - timedelta(days=2)
        row = lambda jd, posted=None: {"jd": jd, "posted_at": posted, "title": "SDE", "companies": "X", "location": ""}  # noqa: E731
        assert judge._judge(row("Data Engineering Bangalore Posted 2 years ago. Python, Spark"), "cv", cutoff)[1] is None
        assert judge._judge(row("Posted: 2024-05-01 We build payment rails"), "cv", cutoff)[1] is None  # JSON-LD datePosted
        assert judge._judge(row(f"Date posted: {UTC:%b %d, %Y}. Backend role"), "cv", cutoff)[1] == {"fit": 0.9}
        assert judge._judge(row("Backend role, no date anywhere"), "cv", cutoff)[1] == {"fit": 0.9}
        assert judge._judge(row("Posted 2 years ago", now(UTC)), "cv", cutoff)[1] == {"fit": 0.9}  # the list's own date wins
    finally:
        judge.describe, decide.decide = real
    assert len(calls) == 3


def test_closed_jobs_leave_the_queue():
    conn = connect(":memory:")
    listed = now(UTC)
    for sid, ats in [(1, "greenhouse"), (2, "generic_js"), (3, "workday")]:
        conn.execute("INSERT INTO sources (id, url, ats, last_ok_at) VALUES (?, ?, ?, ?)", (sid, f"https://x{sid}.com", ats, listed))
    ago = lambda h: now(UTC - timedelta(hours=h))  # noqa: E731
    for i, sid, seen, applied in [
        (1, 1, ago(7), "queued"),    # off a full-list board for 7 h: closed
        (2, 1, listed, "opened"),    # still listed
        (3, 2, ago(7), "queued"),    # a rendered company page: 7 h unlisted can be a different subset on each load
        (4, 2, ago(25), "opened"),   # unlisted for a day: closed
        (5, 3, ago(30), "queued"),   # Workday reads newest-first only, so unlisted means nothing; its API says closed
        (6, 3, ago(30), "queued"),   # its API says open
        (7, 1, ago(30), "applied"),  # applied jobs are history
    ]:
        conn.execute("INSERT INTO jobs (id, source_id, external_id, url, title, first_seen_at, last_seen_at, status, apply_status)"
                     " VALUES (?, ?, ?, 'u', 't', ?, ?, 'matched', ?)", (i, sid, str(i), seen, seen, applied))
    real = judge.closed
    judge.closed = lambda r: True if r["id"] == 5 else False if r["ats"] == "workday" else None
    try:
        assert judge.close_gone(conn) == 3
    finally:
        judge.closed = real
    got = dict(conn.execute("SELECT id, coalesce(apply_status, status) FROM jobs").fetchall())
    assert got == {1: "closed", 2: "opened", 3: "queued", 4: "closed", 5: "closed", 6: "queued", 7: "applied"}, got


def test_purge_keeps_cursor_and_applied():
    conn = connect(":memory:")
    conn.execute("INSERT INTO sources (id, url) VALUES (1, 'https://x.com')")
    old, fresh = now(UTC - timedelta(days=9)), now(UTC - timedelta(hours=1))
    rows = [  # id, posted, last_seen, status, apply_status
        (1, old, fresh, "matched", "queued"),    # old, still listed: leaves the queue, row stays as cursor
        (2, old, old, "rejected", None),         # old and gone from the board: deleted
        (3, old, old, "matched", "applied"),     # applied: kept forever
        (4, fresh, fresh, "matched", "queued"),  # fresh: untouched
        (5, old, fresh, "baseline", None),       # baseline still listed: untouched
    ]
    for i, posted, seen, status, applied in rows:
        conn.execute(
            "INSERT INTO jobs (id, source_id, external_id, url, title, posted_at, first_seen_at, last_seen_at, status, apply_status, description, verdict)"
            " VALUES (?, 1, ?, 'u', 't', ?, ?, ?, ?, ?, 'jd', '{}')", (i, str(i), posted, posted, seen, status, applied))
    purge(conn, 7)
    got = {r[0]: tuple(r[1:]) for r in conn.execute("SELECT id, status, apply_status, description FROM jobs")}
    assert got == {1: ("expired", None, None), 3: ("matched", "applied", None), 4: ("matched", "queued", "jd"), 5: ("baseline", None, None)}, got


def test_new_resume_requeues_recent_jobs():
    conn = connect(":memory:")
    conn.execute("INSERT INTO sources (id, url) VALUES (1, 'https://x.com')")
    for i, status, applied, seen in [(1, "matched", "queued", now(UTC)), (2, "rejected", None, now(UTC)), (3, "matched", "applied", now(UTC)),
                                     (4, "rejected", None, now(UTC - timedelta(days=10))), (5, "filtered", None, now(UTC))]:
        conn.execute("INSERT INTO jobs (id, source_id, external_id, url, title, first_seen_at, last_seen_at, status, apply_status)"
                     " VALUES (?, 1, ?, 'u', 't', ?, ?, ?, ?)", (i, str(i), seen, seen, status, applied))
    rid, requeued = add(conn, "cv.txt", ("python fastapi " * 60).encode())
    assert requeued == 2 and {r[0] for r in conn.execute("SELECT id FROM jobs WHERE status = 'new'")} == {1, 2}
    try:
        add(conn, "scan.txt", b"too short")
        raise AssertionError("short resume accepted")
    except ValueError:
        pass
    assert conn.execute("SELECT count(*) FROM resumes WHERE active = 1").fetchone()[0] == 1


def test_settings_apply_without_rejudging():
    conn = connect(":memory:")
    conn.execute("INSERT INTO sources (id, url) VALUES (1, 'https://x.com')")
    conn.execute("INSERT INTO resumes (filename, text) VALUES ('cv', 'python')")
    conn.execute("UPDATE settings SET value = '35' WHERE key = 'min_score'")
    v = lambda fit, skills, role, exp=0.9: json.dumps({"fit": fit, "skills": skills, "experience": exp, "role": role})
    for i, title, status, applied, verdict in [
        (1, "Python Developer", "rejected", None, v(0.45, 0.64, 0.97)),        # over the cutoff: queued
        (2, "AI/ML Data Science", "rejected", None, v(0.31, 0.88, 0.96)),      # strong skills match: queued
        (3, "Client Solutions Associate", "rejected", None, v(0.37, 0.35, 0.14)),  # not a software role: stays out
        (4, "Web Developer", "matched", "queued", v(0.20, 0.30, 0.90)),        # now under the cutoff: leaves the queue
        (5, "Web Developer II", "matched", "applied", v(0.20, 0.30, 0.90)),    # the user's: untouched
        (6, "Backend Engineer", "filtered", None, '{"gate": "location"}'),     # reopened, then judged
        (7, "Infra Engineer", "rejected", None, json.dumps({"fit": 0.6, "skills": 0.9, "role": 0.9, "india": 0.3})),  # abroad: stays out
        (8, "Software Developer Jobs", "matched", "queued", json.dumps({"fit": 0.61, "skills": 0.73, "role": 0.91, "experience": 0.9, "posting": 0.07})),  # category page
        (9, "Data Engineer", "rejected", None, v(0.8, 0.9, 0.95, exp=0.05)),  # asks 3+ years: stays out however well it fits
        (10, "SDE-2", "matched", "queued", v(0.8, 0.9, 0.95)),                # a level-2 title, judged before the keyword: leaves
    ]:
        conn.execute("INSERT INTO jobs (id, source_id, external_id, url, title, location, first_seen_at, last_seen_at, status, apply_status, verdict)"
                     " VALUES (?, 1, ?, 'u', ?, 'Pune', ?, ?, ?, ?, ?)", (i, str(i), title, now(UTC), now(UTC), status, applied, verdict))
    import jobhunt.judge as j
    j.decide.enabled, j._judge = lambda: True, lambda r, resume, cutoff: ("jd", {"fit": 0.6, "skills": 0.7, "experience": 0.5, "role": 0.9})
    assert judge_pending(conn) == 1
    got = {r[0]: tuple(r[1:]) for r in conn.execute("SELECT id, status, apply_status FROM jobs")}
    assert got == {1: ("matched", "queued"), 2: ("matched", "queued"), 3: ("rejected", None), 4: ("rejected", None),
                   5: ("matched", "applied"), 6: ("matched", "queued"), 7: ("rejected", None), 8: ("rejected", None), 9: ("rejected", None),
                   10: ("filtered", None)}, got


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)

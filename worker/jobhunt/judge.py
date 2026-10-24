"""Judge every job with status 'new': free gates first, then the decision model against the active resume.

Usage: python -m jobhunt.judge [--limit N]   # judge pending jobs now
       python -m jobhunt.judge --sample 30   # a spread of verdicts, to sanity-check min_score

Statuses: stale (older than max_age_days) | filtered (title gate) | duplicate (same title, same source)
          | matched (see wanted(), queued) | rejected | error (model refused).
Location isn't gated: the boards are already filtered to India, and anywhere in India is fine.
"""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from jobhunt import ats, decide, generic
from jobhunt.db import DB_PATH, connect, now

RUBRIC = "You screen job postings for one candidate and judge only from the candidate's resume."
QUESTIONS = {  # see wanted() for how they combine; skills/experience/role are the reasons shown on the queue card
    # the user applies to jobs asking up to 3 years, so fit must not mark those down; 4+ years is the experience gate's job
    "fit": "Leaving aside any experience requirement of up to 3 years, is the candidate a credible applicant for this job: "
           "do their skills, projects and education match what the role needs?",
    "skills": "Does the candidate's resume show most of the core skills this job asks for?",
    # measured on real JDs: asks of 4+ years <= 0.07; 0-3 years, internships and unstated >= 0.77
    "experience": "Is this job open to someone with less than 4 years of work experience? Yes if it asks for 0-3 years, is entry-level "
                  "or an internship, or doesn't say; no if it asks for 4 or more years or is a senior or lead role.",
    "role": "Is this a hands-on software role (backend, full-stack, AI/ML or data engineering)?",
    # company pages often show no location, so global companies' foreign jobs get this far (measured: abroad <= 0.31, India >= 0.94)
    "india": "Is this job based in India, or remote and open to candidates who live in India?",
    # company pages also link category pages ("Software Developer Jobs") that read like a role, and their text, a list of
    # jobs, looks relevant to every other question (measured: category pages <= 0.11, real postings >= 0.55)
    "posting": "Is this page one specific job posting (a single role someone can apply to), rather than a list, search or category page showing several jobs?",
}


def _words(terms):
    return re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b", re.I) if terms else None


def _norm(title):
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def gate(row, cfg, utc):
    """Free checks before any paid call -> (status, reason), or None to send the job to the judge."""
    if (row["posted_at"] or row["first_seen_at"]) < now(utc - timedelta(days=cfg["max_age_days"])):
        return "stale", "older than max_age_days"
    exclude, include = _words(cfg["title_exclude"]), _words(cfg["title_include"])
    if (exclude and exclude.search(row["title"])) or (include and not include.search(row["title"])):
        return "filtered", "title"
    return None


def wanted(p, min_score):
    """The queue rule. fit is the model's overall call, and a strong skills match is queued even with a low fit.
    Blocked regardless: jobs asking 4+ years (experience), non-software jobs (role), foreign ones (india), category or
    list pages (posting; verdicts from before that question was added don't have it, and pass)."""
    return (
        (p.get("role") or 0) >= 0.5
        and (p.get("experience") or 0) >= 0.5
        and (p.get("india") is None or p["india"] >= 0.5)
        and (p.get("posting") is None or p["posting"] >= 0.3)
        and ((p.get("fit") or 0) * 100 >= min_score or (p.get("skills") or 0) >= 0.8)
    )


def resettle(conn, cfg, utc):
    """Apply edited settings to jobs already settled: reopen filtered jobs the gates now let through, and re-run
    the queue rule on judged ones from their stored probabilities (no new model calls)."""
    with conn:
        for r in conn.execute("SELECT * FROM jobs WHERE status = 'filtered'").fetchall():
            if not gate(r, cfg, utc):
                conn.execute("UPDATE jobs SET status = 'new', verdict = NULL WHERE id = ?", (r["id"],))
        for r in conn.execute(
            "SELECT id, status, verdict FROM jobs WHERE status = 'rejected' OR (status = 'matched' AND apply_status = 'queued')"
        ).fetchall():
            want = wanted(json.loads(r["verdict"]), cfg["min_score"])
            if want != (r["status"] == "matched"):  # opened/applied/skipped jobs are the user's: never moved
                conn.execute("UPDATE jobs SET status = ?, apply_status = ? WHERE id = ?", ("matched", "queued", r["id"]) if want else ("rejected", None, r["id"]))


def _text(url, raw):
    return " ".join(" ".join(generic.parse(url, raw).text).split())


def _from_page(url, page_html):
    """The description inside a job page: Phenom's embedded job data, else a JSON-LD JobPosting, else the whole page."""
    m = re.search(r"phApp\.ddo\s*=\s*(\{.*?\});\s*phApp\.", page_html, re.S)
    if m:
        try:
            job = ((json.loads(m[1]).get("jobDetail") or {}).get("data") or {}).get("job") or {}
            if job.get("description"):
                return job["description"]
        except ValueError:
            pass
    posting = [j for j in generic.jsonld_jobs(generic.parse(url, page_html)) if j.description]
    return posting[0].description if posting else page_html


def describe(row):
    """Job description as plain text, up to 8000 chars. What the list gave counts only if it's a real description:
    Oracle HCM and Phenom lists carry one-line summaries, and judging on those is judging on the title.
    Then the board's detail API, else the job page (rendered in a browser if it's JS-only)."""
    listed = _text(row["url"], row["description"] or "")
    if len(listed) >= 400 and row["ats"] not in ("oraclehcm", "phenom"):  # those two lists only ever carry a summary
        return listed[:8000]
    raw = ""
    try:
        with ats.client() as c:
            if row["ats"] == "workday":
                host, tenant, site = row["ats_key"].split("|")
                raw = ats._json(c, "GET", f"https://{host}/wday/cxs/{tenant}/{site}{row['external_id']}")["jobPostingInfo"].get("jobDescription", "")
            elif row["ats"] == "smartrecruiters":
                ad = ats._json(c, "GET", f"https://api.smartrecruiters.com/v1/companies/{row['ats_key']}/postings/{row['external_id']}")["jobAd"]
                raw = " ".join((s or {}).get("text", "") for s in (ad.get("sections") or {}).values())
            elif row["ats"] == "oraclehcm":
                host, site = row["ats_key"].split("|")
                params = {"expand": "all", "onlyData": "true", "finder": f'ById;Id="{row["external_id"]}",siteNumber={site}'}
                it = (ats._json(c, "GET", f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails", params=params).get("items") or [{}])[0]
                raw = " ".join(it.get(k) or "" for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr"))
            else:
                r = c.get(row["url"])
                raw = _from_page(str(r.url), r.text) if r.status_code < 400 else ""
    except (httpx.HTTPError, KeyError, ValueError):
        pass
    text = _text(row["url"], raw)
    if len(text) < 300 and row["ats"] in ("generic", "generic_js"):  # a JS-only job page: let a browser render it
        try:
            p, _ = generic.fetch(row["url"], js=True)
            posting = [j for j in generic.jsonld_jobs(p) if j.description]
            text = _text(row["url"], posting[0].description) if posting else " ".join(generic._lines(p))
        except Exception:  # browser failed: what we have will do
            pass
    return (text if len(text) > len(listed) else listed)[:8000]  # worst case: title, company and location alone


def _judge(row, resume):
    jd = describe(row)
    job = f"Title: {row['title']}\nCompany: {row['companies']}\nLocation: {row['location'] or 'not stated'}\n\n{jd or '(no description available)'}"
    return jd, decide.decide("judge", {"candidate_resume": f"{RUBRIC}\n\n{resume}", "job": job}, QUESTIONS)


def judge_pending(conn, limit=500, workers=4):
    """-> number of jobs given a final status this pass (0 when nothing to do or the model is unreachable)."""
    resume = conn.execute("SELECT id, text FROM resumes WHERE active = 1 ORDER BY id DESC LIMIT 1").fetchone()
    if not resume or not decide.enabled():
        return 0
    cfg = {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM settings")}
    utc = datetime.now(timezone.utc)
    resettle(conn, cfg, utc)
    rows = conn.execute(
        "SELECT j.*, s.ats, s.ats_key, (SELECT group_concat(name, ', ') FROM companies WHERE source_id = s.id) AS companies"
        " FROM jobs j JOIN sources s ON s.id = j.source_id WHERE j.status = 'new' ORDER BY j.first_seen_at LIMIT ?",
        (limit,),
    ).fetchall()
    window = now(utc - timedelta(days=cfg["retention_days"]))
    twins = {
        (r["source_id"], _norm(r["title"])): r["id"]
        for r in conn.execute("SELECT id, source_id, title FROM jobs WHERE status IN ('matched', 'rejected') AND first_seen_at >= ?", (window,))
    }
    pending, done = [], 0
    with conn:
        for r in rows:
            g = gate(r, cfg, utc)
            twin = twins.get((r["source_id"], _norm(r["title"])))
            if g or twin:  # same role posted again (another city, a repost): one verdict is enough
                status, verdict = g or ("duplicate", None)
                conn.execute(
                    "UPDATE jobs SET status = ?, verdict = ?, description = NULL WHERE id = ?",
                    (status, json.dumps({"gate": g[1]} if g else {"twin": twin}), r["id"]),
                )
                done += 1
            else:
                twins[(r["source_id"], _norm(r["title"]))] = r["id"]
                pending.append(r)

    failures = []
    with ThreadPoolExecutor(workers) as ex:
        futures = {ex.submit(_judge, r, resume["text"]): r for r in pending}
        for f in as_completed(futures):
            r = futures[f]
            try:
                jd, p = f.result()
            except Exception as e:  # stays 'new' and is retried next pass
                failures.append(f"{type(e).__name__}: {str(e)[:120]}")
                if len(failures) >= 5:  # out of credit, bad key, provider down: stop instead of hammering it
                    ex.shutdown(cancel_futures=True)
                    break
                continue
            fit = p.get("fit")
            status = "error" if fit is None else "matched" if wanted(p, cfg["min_score"]) else "rejected"
            with conn:
                conn.execute(
                    "UPDATE jobs SET status = ?, score = ?, verdict = ?, resume_id = ?, description = ?,"
                    " apply_status = CASE WHEN ? = 'matched' THEN 'queued' END WHERE id = ?",
                    (status, None if fit is None else round(fit * 100), json.dumps(p), resume["id"], jd or None, status, r["id"]),
                )
            done += 1
    if failures:
        print(f"judge: {len(failures)} failed, e.g. {failures[0]}", flush=True)
    return done


def purge(conn, retention_days):
    """Jobs older than retention lose their description/verdict and leave the queue; the row stays as the cursor
    while the board still lists it, and is deleted once the board hasn't listed it for retention_days.
    Applied jobs are kept, so a repost isn't queued again."""
    utc = datetime.now(timezone.utc)
    cut = now(utc - timedelta(days=retention_days))
    with conn:
        conn.execute(
            "UPDATE jobs SET description = NULL,"
            " verdict = CASE WHEN apply_status = 'applied' THEN verdict END,"
            " status = CASE WHEN status = 'baseline' OR apply_status = 'applied' THEN status ELSE 'expired' END,"
            " apply_status = CASE WHEN apply_status IN ('queued', 'opened') THEN NULL ELSE apply_status END"
            " WHERE coalesce(posted_at, first_seen_at) < ? AND (status NOT IN ('baseline', 'expired') OR description IS NOT NULL)",
            (cut,),
        )
        conn.execute("DELETE FROM jobs WHERE last_seen_at < ? AND apply_status IS NOT 'applied'", (cut,))
        conn.execute("DELETE FROM llm_calls WHERE at < ?", (now(utc - timedelta(days=30)),))
        conn.execute("DELETE FROM link_answers WHERE seen_at < ?", (now(utc - timedelta(days=30)),))  # links gone from their page


def daily(conn):
    """Purge + backup, once per IST day (whenever the worker first runs after midnight)."""
    day = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y%m%d")
    folder = Path(DB_PATH).parent / "backups"
    target = folder / f"jobhunt-{day}.db"
    if target.exists():
        return
    purge(conn, json.loads(conn.execute("SELECT value FROM settings WHERE key = 'retention_days'").fetchone()[0]))
    folder.mkdir(parents=True, exist_ok=True)
    conn.execute("VACUUM INTO ?", (str(target),))
    for old in sorted(folder.glob("jobhunt-*.db"))[:-7]:
        old.unlink()


def sample(conn, n):
    rows = conn.execute(
        "SELECT j.id, j.score, j.verdict, j.title, j.location, j.url, (SELECT group_concat(name, ', ') FROM companies WHERE source_id = j.source_id) co"
        " FROM jobs j WHERE status IN ('matched', 'rejected') ORDER BY score"
    ).fetchall()
    for r in [rows[round(i * (len(rows) - 1) / max(n - 1, 1))] for i in range(min(n, len(rows)))]:
        v = json.loads(r["verdict"])
        tags = " ".join(f"{k[:5]}={round(v.get(k) or 0, 2):<4}" for k in ("skills", "experience", "role"))
        print(f"#{r['id']:<6} fit={r['score']:>3}  {tags}  {r['title'][:48]:48} | {(r['co'] or '')[:22]:22} | {(r['location'] or '-')[:24]:24} {r['url']}")


if __name__ == "__main__":
    conn = connect()
    if "--sample" in sys.argv:
        sample(conn, int(sys.argv[sys.argv.index("--sample") + 1]))
        sys.exit()
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 5000
    print(f"provider: {decide.PROVIDER}")
    print(f"judged/settled {judge_pending(conn, limit, workers=8)} jobs")
    print("status now:", dict(conn.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall()))

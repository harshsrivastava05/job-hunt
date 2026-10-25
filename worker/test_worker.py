"""Run: python test_worker.py (or pytest). The cursor: what counts as new, what gets marked seen."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["JOBHUNT_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")  # before jobhunt.db reads it

from jobhunt import decide, generic
from jobhunt.ats import Job
from jobhunt.db import connect
from jobhunt.generic import AGGREGATOR, canon, decided_jobs, heuristic_jobs, job_links, parse
from jobhunt.worker import save

CFG = {"interval_min": 60, "max_age_days": 3}


def _src(conn):
    return conn.execute("SELECT * FROM sources WHERE id = 1").fetchone()


def _status(conn):
    return dict(conn.execute("SELECT external_id, status FROM jobs ORDER BY id").fetchall())


def test_cursor():
    conn = connect(":memory:")
    conn.execute("INSERT INTO sources (id, url, ats, ats_key) VALUES (1, 'https://x.com/careers', 'lever', 'x')")
    utc = datetime.now(timezone.utc)
    fresh, old = Job("a", "SDE", "u/a", posted_at=utc - timedelta(days=1)), Job("b", "PM", "u/b", posted_at=utc - timedelta(days=20))
    undated = Job("c", "SRE", "u/c")

    # first crawl = baseline: only the job posted inside max_age_days is worth judging
    assert save(conn, _src(conn), {"jobs": [fresh, old, undated], "hash": "api:h1"}, "2026-10-24 10:00:00", CFG) == ("changed", 1)
    assert _status(conn) == {"a": "new", "b": "baseline", "c": "baseline"}
    assert conn.execute("SELECT description FROM jobs WHERE external_id = 'b'").fetchone()[0] is None

    # same list again: nothing inserted, all still marked seen
    assert save(conn, _src(conn), {"jobs": [fresh, old, undated], "hash": "api:h1"}, "2026-10-24 11:00:00", CFG) == ("unchanged", 0)
    assert conn.execute("SELECT count(*) FROM jobs WHERE last_seen_at = '2026-10-24 11:00:00'").fetchone()[0] == 3

    # after bootstrap any unseen id is new, dated or not; 'b' dropped off the board and stops being seen
    later = Job("d", "Backend Engineer", "u/d")
    assert save(conn, _src(conn), {"jobs": [fresh, undated, later], "hash": "api:h2"}, "2026-10-24 12:00:00", CFG) == ("changed", 1)
    assert _status(conn)["d"] == "new"
    assert conn.execute("SELECT last_seen_at FROM jobs WHERE external_id = 'b'").fetchone()[0] == "2026-10-24 11:00:00"

    # generic page unchanged (jobs=None): previously listed jobs stay seen without re-extracting
    assert save(conn, _src(conn), {"jobs": None, "hash": "api:h2"}, "2026-10-24 13:00:00", CFG) == ("unchanged", 0)
    assert conn.execute("SELECT count(*) FROM jobs WHERE last_seen_at = '2026-10-24 13:00:00'").fetchone()[0] == 3

    # failures back off and keep the cursor intact
    assert save(conn, _src(conn), {"error": "boom"}, "2026-10-24 14:00:00", CFG) == ("failed", 0)
    src = _src(conn)
    assert (src["fail_count"], src["last_ok_at"], src["list_hash"]) == (1, "2026-10-24 13:00:00", "api:h2")


def test_extractor_switch_is_a_baseline():
    conn = connect(":memory:")
    conn.execute("INSERT INTO sources (id, url, ats, ats_key) VALUES (1, 'https://x.com/careers', 'generic', 'https://x.com/careers')")
    save(conn, _src(conn), {"jobs": [Job("u/a", "SDE", "u/a")], "hash": "heuristic:p1"}, "2026-10-24 10:00:00", CFG)
    # decision-model key added: same page now yields more jobs; they were open all along, so none are 'new'
    more = [Job("u/a", "SDE", "u/a"), Job("u/b", "Data Engineer", "u/b"), Job("u/c", "Designer", "u/c")]
    assert save(conn, _src(conn), {"jobs": more, "hash": "decide:p1"}, "2026-10-24 11:00:00", CFG) == ("changed", 0)
    # from then on, the LLM finding a new job is new
    assert save(conn, _src(conn), {"jobs": more + [Job("u/d", "SRE", "u/d")], "hash": "decide:p2"}, "2026-10-24 12:00:00", CFG) == ("changed", 1)


def test_first_jobs_on_an_empty_page_are_a_baseline():
    conn = connect(":memory:")
    conn.execute("INSERT INTO sources (id, url, ats, ats_key) VALUES (1, 'https://x.com/jobs', 'generic_js', 'https://x.com/jobs')")
    assert save(conn, _src(conn), {"jobs": [], "hash": "decide2:empty"}, "2026-10-24 10:00:00", CFG) == ("changed", 0)  # didn't render
    utc = datetime.now(timezone.utc)
    shown = [Job("u/a", "Data Engineer Posted 2 years ago", "u/a"), Job("u/b", "SDE", "u/b", posted_at=utc - timedelta(hours=5))]
    assert save(conn, _src(conn), {"jobs": shown, "hash": "decide2:p1"}, "2026-10-25 07:00:00", CFG) == ("changed", 1)
    assert _status(conn) == {"u/a": "baseline", "u/b": "new"}  # only the one dated inside max_age_days is judged
    later = shown + [Job("u/c", "Backend Engineer", "u/c")]
    assert save(conn, _src(conn), {"jobs": later, "hash": "decide2:p2"}, "2026-10-25 08:00:00", CFG) == ("changed", 1)


def test_heuristic_jobs():
    page = parse("https://acme.com/careers", """
      <a href="/careers">Careers</a> <a href="/about">About us</a> <a href="/careers/life">Life at Acme</a>
      <a href="/careers/jobs/123?utm_source=x">Senior Backend Engineer</a>
      <a href="https://acme.icims.com/jobs/77/data-analyst/job">Data Analyst - Bengaluru</a>
      <a href="/careers/apply">Apply now</a> <a href="/blog/how-we-hire">How we hire engineers at Acme</a>
    """)
    titles = [j.title for j in heuristic_jobs(page)]
    assert titles == ["Senior Backend Engineer", "Data Analyst - Bengaluru"], titles
    assert heuristic_jobs(page)[0].external_id == "https://acme.com/careers/jobs/123"  # tracking params stripped


def test_card_links_take_the_heading_as_title():
    page = parse("https://acme.com/careers", """
      <div class="card"><h3>Backend Engineer</h3><p>Bengaluru</p><a href="/careers/jobs/42">Apply now</a></div>
      <div class="card"><h3>Product Designer</h3><span>Remote</span><a href="/careers/jobs/43">View details</a></div>
      <a href="/careers/openings">View open positions</a>
      <a href="/jobs/sde-ii"><h4>SDE II</h4><span>tech</span><span>Full-time</span><span>Bangalore</span>
         <p>Join us to build and optimize scalable web applications using React and Node.js.</p><span>Learn more</span></a>
    """)
    assert page.links == [
        ("https://acme.com/careers/jobs/42", "Backend Engineer"),  # the role-like line wins over the location under it
        ("https://acme.com/careers/jobs/43", "Product Designer"),
        ("https://acme.com/careers/openings", "View open positions"),  # informative link text is left alone
        ("https://acme.com/jobs/sde-ii", "SDE II"),  # whole card as one link: first line is the title
    ]


def test_titles_from_job_urls_and_junk_links():
    page = parse("https://acme.com/careers", """
      <div><span>30-Jan-2026</span><a href="/jobs/senior-backend-engineer-4521">Apply</a></div>
      <div><span>Full Time</span><a href="/jobs/data-analyst_2/">View</a></div>
      <div><span>Hyderabad</span><a href="/jobs/8812">Apply</a></div>
      <a href="https://x.com/intent/tweet?url=acme">X</a>""")
    assert [t for _, t in page.links] == ["Senior Backend Engineer", "Data Analyst", "Hyderabad", "X"]  # no slug to use: unchanged
    assert AGGREGATOR.search("x.com") and AGGREGATOR.search("www.facebook.com") and not AGGREGATOR.search("x.company.com")


def test_changed_page_only_asks_about_new_links():
    asked = []

    def fake(kind, state, questions):
        lines = state["items"].splitlines()
        asked.append([line.split(" ", 1)[1].split(" -> ")[0] for line in lines])
        return {f"i{i}": 0.9 if "Engineer" in line else 0.1 for i, line in enumerate(lines)}

    real, decide.decide = decide.decide, fake
    try:
        html = '<a href="/jobs/1">Backend Engineer</a><a href="/stories">Customer stories</a>'
        assert [j.title for j in decided_jobs(parse("https://acme.com/careers", html))] == ["Backend Engineer"]
        html += '<a href="/jobs/2">Data Engineer</a><a href="/cdn-cgi/content?id=abc">Data Engineer</a>'
        assert [j.title for j in decided_jobs(parse("https://acme.com/careers", html))] == ["Backend Engineer", "Data Engineer"]
        assert decided_jobs(parse("https://acme.com/careers", html)) and len(asked) == 2  # nothing new: no request
    finally:
        decide.decide = real
    assert asked == [["Backend Engineer", "Customer stories"], ["Data Engineer"]], asked


def test_page_whose_jobs_need_scripts_is_rendered():
    shell = parse("https://acme.com/careers", "<p>We are hiring! " + "Life at Acme is great. " * 40 + "</p>")
    rendered = parse("https://acme.com/careers", '<a href="/jobs/7">Platform Engineer</a>')
    real = generic.fetch, decide.decide
    generic.fetch = lambda url, js=False: (rendered, True) if js else (shell, False)
    decide.decide = lambda kind, state, questions: {k: 0.9 for k in questions}
    try:
        jobs, h, updates = generic.crawl("https://acme.com/careers", False, None, follow=False)
    finally:
        generic.fetch, decide.decide = real
    assert [j.title for j in jobs] == ["Platform Engineer"] and updates == {"ats": "generic_js"}, (jobs, updates)


def test_canon_and_aggregators():
    a = "https://www.linkedin.com/jobs/view/sde-at-acme-4374162161?position=1&pageNum=0&refId=LgO%2F1&trackingId=OCGs"
    b = "https://www.linkedin.com/jobs/view/sde-at-acme-4374162161?position=1&pageNum=0&refId=4HqR&trackingId=4ScT"
    assert canon(a) == canon(b) == "https://www.linkedin.com/jobs/view/sde-at-acme-4374162161"
    assert canon("https://x.com/job?id=42&utm_source=li&gh_src=abc#apply") == "https://x.com/job?id=42"
    assert canon("https://x.com/careers/#/jobs/42") == "https://x.com/careers#/jobs/42"  # hash routes are kept
    page = parse("https://acme.com/careers", '<a href="https://www.linkedin.com/company/acme/jobs">View open positions</a>'
                                             '<a href="/careers/openings">View open positions</a>')
    assert job_links(page) == ["https://acme.com/careers/openings"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)

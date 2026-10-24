"""Run: python test_ats.py (or pytest). Offline: detection, date parsing, India filter."""
from datetime import datetime, timedelta, timezone

from jobhunt.ats import _sf_html, detect, in_india, parse_dt


def test_detect_picks_real_boards():
    page = """
      <a href="https://careers.unilever.com/en/india">not lever</a>
      <link href="https://cdn.lever.co/fonts/x.woff"> <link href="https://job-boards.cdn.greenhouse.io/assets/a.css">
      <a href="https://amgen.wd1.myworkdayjobs.com/en-US/Careers/login">sign in</a>
      <a href="https://amgen.eightfold.ai/careers/join?domain=amgen.com">talent network</a>
      <script src="https://vs-errors.eightfold.ai/x.js"></script>
    """
    assert detect("https://www.amgen.com/careers", page) == [
        ("workday", "amgen.wd1.myworkdayjobs.com|amgen|Careers"),
        ("eightfold", "amgen.eightfold.ai|amgen.com"),
    ]


def test_detect_key_shapes():
    cases = {
        '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme&b=1">': ("greenhouse", "acme"),
        "https:\\/\\/jobs.lever.co\\/cred\\/aa50": ("lever", "cred"),
        "https://jobs.lever.co/oaknorth.ai/x https://jobs.lever.co/cdn-cgi/l/email": ("lever", "oaknorth.ai"),
        "https://wd3.myworkdaysite.com/en-US/recruiting/acme/External/job/x": ("workday", "wd3.myworkdaysite.com|acme|External"),
        "https://acme.keka.com/careers/": ("keka", "acme"),
        "https://jodo.keka.com/careers/api/embedjobs/fc1b8ba5-76ef-4c3f-88c3-3b54f88acc15": ("keka", "jodo|fc1b8ba5-76ef-4c3f-88c3-3b54f88acc15"),
        "https://app.pyjamahr.com/careers?company=Acme&amp;company_uuid=25D3B9BFB0": ("pyjamahr", "company_uuid=25D3B9BFB0"),
        "https://eofe.fa.us2.oraclecloud.com:443/hcmUI/CandidateExperience/en/sites/BNY-Careers/jobs": ("oraclehcm", "eofe.fa.us2.oraclecloud.com|BNY-Careers"),
        "https://x.fa.ocs.oraclecloud.com:443/hcmUI/CandidateExperience/images?id=1": ("oraclehcm", "x.fa.ocs.oraclecloud.com|CX_1"),
        "https://join.smartrecruiters.com/Experian/59c5-join-our-community": ("smartrecruiters", "Experian"),
        '<a href="https://careers.zohocorp.com/jobs/Careers/123/x">': ("zoho", "careers.zohocorp.com"),
        "https://apply.hp.com/careers?start=0&pid=44240108&sort_by=timestamp": ("eightfold", "apply.hp.com|hp.com"),
    }
    for page, want in cases.items():
        assert detect("https://example.com/careers", page)[:1] == [want], (page, detect("https://example.com/careers", page))
    # the page's own URL wins over anything it links to
    assert detect("https://careers.abb/global/en", 'cdn.phenompeople.com/CareerConnectResources/ABB1GLOBAL/en_global/x.ico')[0] == (
        "phenom", "https://careers.abb|en_global")
    assert detect("https://www.virtusa.com/careers/job-search/global/en/home", "cdn.phenompeople.com/CareerConnectResources/VIR/en_global/a")[0] == (
        "phenom", "https://www.virtusa.com/careers/job-search|en_global")
    sf = '<link href="https://rmkcdn.successfactors.com/84a4/main.css">'
    assert detect("https://careers.wipro.com/", sf) == [("successfactors", "https://careers.wipro.com")]
    assert detect("https://careers.firstsource.com/India/go/India-Jobs/655444/", sf) == [("successfactors", "https://careers.firstsource.com/India/go/India-Jobs/655444")]
    assert detect("https://careers.amd.com/careers-home/jobs?page=2", '<script src="https://app.jibecdn.com/prod/search/4.11.225/main.js">') == [
        ("icims", "https://careers.amd.com/careers-home/jobs")]


def test_successfactors_html():
    """Table rows (each title link twice: desktop + phone) and tiles (fields in labelled divs) on one results page."""
    page = """Results <b>1 – 3</b> of <b>3</b>
      <tr class="data-row"><td><a href="/job/Bangalore-SDE/111/" class="jobTitle-link">SDE &amp; ML</a>
        <a class="jobTitle-link" href="/job/Bangalore-SDE/111/">SDE &amp; ML</a></td>
        <td><span class="jobLocation"> Hoskote, IN, 562114 </span></td><td><span class="jobDate">Oct 22, 2026</span></td></tr>
      <tr class="data-row"><td><a href="/job/Inverness-Tech/222/" class="jobTitle-link">HGV Technician</a></td>
        <td><span class="jobLocation"> Inverness, GB, IV1 1SU </span></td></tr>
      <div class="job-tile"><a class="jobTitle-link" href="/job/Bengaluru-Engineer/333/"> Workshop Engineer </a>
        <span id="job-333-desktop-section-city-label" aria-describedby="job-333-desktop-section-city-value">Location</span>
        <div id="job-333-desktop-section-city-value">Bengaluru </div></div>"""

    class Client:
        def get(self, url):
            return type("R", (), {"text": page, "raise_for_status": lambda self: None})()

    jobs = _sf_html(Client(), "https://careers.x.com", "https://careers.x.com", datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert [(j.external_id, j.title, j.location, j.posted_at and j.posted_at.date().isoformat(), j.url) for j in jobs] == [
        ("111", "SDE & ML", "Hoskote, IN, 562114", "2026-10-22", "https://careers.x.com/job/Bangalore-SDE/111/"),
        ("333", "Workshop Engineer", "Bengaluru", None, "https://careers.x.com/job/Bengaluru-Engineer/333/"),
    ], jobs


def test_parse_dt():
    now = datetime.now(timezone.utc)
    assert parse_dt("2026-10-16T00:00:00.000+0000") == datetime(2026, 10, 16, tzinfo=timezone.utc)
    assert parse_dt("2025-10-15T18:53:44.116Z") == datetime(2025, 10, 15, 18, 53, 44, 116000, tzinfo=timezone.utc)
    assert parse_dt("2026-10-23") == datetime(2026, 10, 23, tzinfo=timezone.utc)
    assert abs(parse_dt(1790573982021).timestamp() - 1790573982.021) < 1e-3 and abs(parse_dt("1790573982").timestamp() - 1790573982) < 1e-3
    assert abs(parse_dt("Posted Yesterday") - (now - timedelta(days=1))) < timedelta(minutes=1)
    assert abs(parse_dt("Posted 30+ Days Ago") - (now - timedelta(days=30))) < timedelta(minutes=1)
    assert parse_dt("Posted Today") > now - timedelta(minutes=1)
    assert parse_dt(None) is None and parse_dt("soon") is None
    assert parse_dt("24 October 2026") == parse_dt("Oct 24, 2026") == datetime(2026, 10, 24, tzinfo=timezone.utc)


def test_in_india():
    for loc, country in [("Bengaluru-VTP, India", ""), ("hyderabad", ""), ("Remote", ""), ("", ""), ("2 Locations", ""),
                         ("Krakow, Poland / Bangalore, India", "PL"), ("anything", "IN"),
                         ("Hoskote, IN, 562114", ""), ("Nashik, MH, IN", ""), ("Bhubaneswar, IND-21, IND, 751024", "")]:
        assert in_india(loc, country), (loc, country)
    for loc, country in [("Dallas, Texas, United States", ""), ("Europe Remote", ""), ("Remote", "US"), ("Indianapolis, IN", ""),
                         ("Indianapolis, IN, 46204", "")]:
        assert not in_india(loc, country), (loc, country)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)

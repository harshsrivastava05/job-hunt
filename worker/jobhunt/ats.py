"""Job-board detection + one adapter per board.

Every adapter is `fn(client, key, since) -> list[Job]`, keeps only jobs that are in India (or don't say where),
and stops paging once results are older than `since` where the board sorts newest-first.
"""
import html
import json
import re
import threading
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
MAX_JOBS = 500  # per source per crawl; nobody needs page 26 of a global employer
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


@dataclass
class Job:
    external_id: str
    title: str
    url: str
    location: str = ""
    posted_at: datetime | None = None  # UTC; None = board doesn't say, first_seen_at stands in
    description: str = ""  # only when the list API includes it


def client():
    return httpx.Client(
        headers={"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"}, follow_redirects=True, timeout=30
    )


def _json(c, method, url, **kw):
    r = c.request(method, url, **kw)
    r.raise_for_status()
    return r.json()


_BROWSERS = threading.Semaphore(2)  # ponytail: 2 Chromiums at once (~300 MB each); a shared pool if crawls queue up


@contextmanager
def browser_page(url, wait="domcontentloaded"):
    """Chromium page already at `url`, plus the list of every URL it requested."""
    from playwright.sync_api import sync_playwright

    with _BROWSERS, sync_playwright() as p:
        b = p.chromium.launch()
        try:
            pg = b.new_page(user_agent=UA, locale="en-IN")  # the default UA says HeadlessChrome and gets blocked
            urls = []
            pg.on("request", lambda r: urls.append(r.url))
            # job lists are text: skipping images, video and fonts saves most of the bandwidth
            pg.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media", "font") else r.continue_())
            pg.goto(url, wait_until=wait, timeout=60_000)
            yield pg, urls
        finally:
            b.close()


# ---------------------------------------------------------------- helpers

INDIA = re.compile(
    r"\b(india|bengaluru|bangalore|hyderabad|secunderabad|mumbai|pune|delhi|gurgaon|gurugram|noida|chennai|kolkata"
    r"|ahmedabad|kochi|cochin|jaipur|chandigarh|coimbatore|indore|trivandrum|thiruvananthapuram|mysore|mysuru"
    r"|visakhapatnam|vizag|karnataka|telangana|maharashtra|tamil nadu|kerala|haryana|uttar pradesh|west bengal|gujarat)\b",
    re.I,
)


def in_india(text="", country=""):
    """True if the location says India, or says nothing beyond remote/hybrid (the Phase 4 location gate decides)."""
    country = (country or "").strip()
    if country.lower() in ("in", "ind", "india") or INDIA.search(text or ""):
        return True
    # country codes: 'Ahmedabad, IND-24, IND', 'Hoskote, IN, 562114', 'Nashik, MH, IN'; a bare 'Indianapolis, IN' is Indiana
    parts = [p.strip() for p in re.split(r"[,/|;]", text or "")]
    if "IND" in parts or ("IN" in parts and (parts[-1] == "IN" and len(parts) >= 3 or any(re.fullmatch(r"\d{6}", p) for p in parts))):
        return True
    leftover = re.sub(r"(?i)\b(remote|hybrid|on-?site|none|anywhere|\d+ locations)\b", "", text or "")
    return not country and not re.search(r"[a-z]", leftover, re.I)


def parse_dt(v):
    """ISO string, epoch s/ms, or Workday's 'Posted 3 Days Ago' -> aware UTC datetime (None if unknown)."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        v = float(v)
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc)
    m = re.search(r"posted (today|yesterday|(\d+)\+? days? ago)", v, re.I)
    if m:
        days = {"today": 0, "yesterday": 1}.get(m[1].lower()) if not m[2] else int(m[2])
        return datetime.now(timezone.utc) - timedelta(days=days)
    try:
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%d %B %Y", "%d %b %Y", "%b %d, %Y", "%B %d, %Y"):  # '24 October 2026', 'Oct 22, 2026'
            try:
                dt = datetime.strptime(v.strip(), fmt)
                break
            except ValueError:
                pass
        else:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")


def _older(dt, since):
    return dt is not None and dt < since


# ---------------------------------------------------------------- adapters


def workday(c, key, since):
    host, tenant, site = key.split("|")
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    page_url = f"https://{host}/recruiting/{tenant}/{site}" if "myworkdaysite" in host else f"https://{host}/{site}"
    body = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
    d = _json(c, "POST", api, json=body)
    india = _workday_india(d.get("facets", []))
    if india:  # filter server-side, global employers list thousands of jobs
        body["appliedFacets"] = {india[0]: [india[1]]}
        d = _json(c, "POST", api, json=body)
    total, posts, jobs = d.get("total", 0), d.get("jobPostings", []), []
    while posts:  # newest first
        for p in posts:
            loc = p.get("locationsText", "")
            if india or in_india(loc):
                jobs.append(Job(p["externalPath"], p["title"], page_url + p["externalPath"], loc, parse_dt(p.get("postedOn"))))
        body["offset"] += len(posts)
        if body["offset"] >= min(total, MAX_JOBS) or _older(parse_dt(posts[-1].get("postedOn")), since):
            break
        posts = _json(c, "POST", api, json=body).get("jobPostings", [])
    return jobs


def _workday_india(facets):
    """(facetParameter, id) of 'India' in a country facet; facets can be nested in groups."""
    for f in facets:
        for v in f.get("values", []):
            if "facetParameter" in v:
                if hit := _workday_india([v]):
                    return hit
            elif v.get("descriptor", "").strip().lower() == "india" and "country" in f.get("facetParameter", "").lower():
                return f["facetParameter"], v["id"]
    return None


def greenhouse(c, key, since):
    d = _json(c, "GET", f"https://boards-api.greenhouse.io/v1/boards/{key}/jobs", params={"content": "true"})
    jobs = []
    for j in d["jobs"]:
        loc = (j.get("location") or {}).get("name", "")
        if in_india(" / ".join([loc] + [o.get("name", "") for o in j.get("offices", [])])):
            posted = parse_dt(j.get("first_published") or j.get("updated_at"))
            jobs.append(Job(str(j["id"]), j["title"], j["absolute_url"], loc, posted, html.unescape(j.get("content") or "")))
    return jobs


def lever(c, key, since):
    jobs = []
    for j in _json(c, "GET", f"https://api.lever.co/v0/postings/{key}", params={"mode": "json"}):
        cat = j.get("categories") or {}
        loc = ", ".join(cat.get("allLocations") or [cat.get("location") or ""])
        if in_india(loc, j.get("country")):
            jobs.append(Job(j["id"], j["text"], j["hostedUrl"], loc, parse_dt(j.get("createdAt")), j.get("descriptionPlain", "")))
    return jobs


def ashby(c, key, since):
    jobs = []
    for j in _json(c, "GET", f"https://api.ashbyhq.com/posting-api/job-board/{key}")["jobs"]:
        if not j.get("isListed", True):
            continue
        places = [j] + (j.get("secondaryLocations") or [])
        loc = " / ".join(p.get("location", "") for p in places)
        countries = " ".join(((p.get("address") or {}).get("postalAddress") or {}).get("addressCountry", "") for p in places)
        if in_india(f"{loc} {countries}"):
            jobs.append(Job(j["id"], j["title"], j["jobUrl"], loc, parse_dt(j.get("publishedAt")), j.get("descriptionPlain", "")))
    return jobs


def smartrecruiters(c, key, since):
    jobs, offset = [], 0
    while offset < MAX_JOBS:
        params = {"country": "in", "limit": 100, "offset": offset}
        d = _json(c, "GET", f"https://api.smartrecruiters.com/v1/companies/{key}/postings", params=params)
        for j in d.get("content", []):
            l = j.get("location") or {}
            loc = ", ".join(filter(None, [l.get("city"), l.get("region"), "India", "remote" if l.get("remote") else ""]))
            jobs.append(Job(j["id"], j["name"], f"https://jobs.smartrecruiters.com/{key}/{j['id']}", loc, parse_dt(j.get("releasedDate"))))
        offset += 100
        if offset >= d.get("totalFound", 0):
            break
    return jobs


def workable(c, key, since):
    jobs = []
    for j in _json(c, "GET", f"https://apply.workable.com/api/v1/widget/accounts/{key}").get("jobs", []):
        loc = " / ".join(", ".join(filter(None, [l.get("city"), l.get("country")])) for l in j.get("locations") or [j])
        if in_india(loc + (" remote" if j.get("telecommuting") else "")):
            jobs.append(Job(j["shortcode"], j["title"], j["url"], loc, parse_dt(j.get("published_on") or j.get("created_at"))))
    return jobs


def keka(c, key, since):
    """key: 'sub' or 'sub|org-guid' (the guid when the company page embeds it)."""
    sub, _, org = key.partition("|")
    base = f"https://{sub}.keka.com/careers"
    if not org:
        r = c.get(f"{base}/api/organization/default/careerportalinfo")
        r.raise_for_status()
        # ponytail: the org id only appears inside an asset path here; re-sniff the careers page if Keka moves it
        m = re.search(UUID, r.text)
        if not m:
            raise ValueError("keka org id not found")
        org = m[0]
    jobs = []
    for j in _json(c, "GET", f"{base}/api/embedjobs/default/active/{org}"):
        loc = " / ".join(
            ", ".join(filter(None, [l.get("city") or l.get("name"), l.get("state"), l.get("countryName") or l.get("country")]))
            for l in j.get("jobLocations") or []
        )
        if in_india(loc):
            jobs.append(Job(str(j["id"]), j["title"], f"{base}/jobdetails/{j['id']}", loc, parse_dt(j.get("publishedOn")), j.get("description") or ""))
    return jobs


_POST_JSON = """async ([url, body]) => {
  const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  return [r.status, await r.text()];
}"""


def darwinbox(c, key, since):
    # Cloudflare 403s plain HTTP clients here, so call the API from inside a real page
    api, jobs = f"https://{key}/ms/candidateapi/job/alljobs?companyId=main", []
    # open the page the old /ms/candidate/careers URL redirects to, or the fetch dies mid-navigation
    with browser_page(f"https://{key}/ms/candidatev2/main/careers/allJobs", wait="load") as (pg, _):
        for page in range(1, MAX_JOBS // 50 + 1):
            status, text = pg.evaluate(_POST_JSON, [api, {"companyId": "main", "page": page, "sort_option": "new", "limit": 50}])
            if status != 200:
                raise RuntimeError(f"darwinbox alljobs HTTP {status}")
            batch = json.loads(text).get("data") or []
            for j in batch:
                loc = (j.get("locations") or j.get("officelocation_show_arr") or "").replace("\r", "")
                if in_india(loc + (" remote" if j.get("is_remote") else "")):
                    url = f"https://{key}/ms/candidatev2/main/careers/jobDetails/{j['id']}"
                    title = j.get("title") or j.get("designation_name") or ""
                    jobs.append(Job(j["id"], title, url, loc, parse_dt(j.get("posted_on") or j.get("created_on")), html.unescape(j.get("jd") or "")))
            if len(batch) < 50 or _older(parse_dt(batch[-1].get("posted_on")), since):
                break
    return jobs


def zoho(c, key, since):
    r = c.get(f"https://{key}/jobs/Careers")
    r.raise_for_status()
    m = re.search(r'<input[^>]*\bid="jobs"[^>]*\bvalue="([^"]*)"', r.text) or re.search(r'<input[^>]*\bvalue="([^"]*)"[^>]*\bid="jobs"', r.text)
    if not m:
        raise ValueError("zoho: no embedded job list")
    jobs = []
    for j in json.loads(html.unescape(m[1])):  # no posting date in this list
        if str(j.get("Publish")).lower() == "false":
            continue
        loc = ", ".join(str(j[k]) for k in ("City", "State", "Country") if j.get(k) not in (None, "", "None"))
        remote = str(j.get("Remote_Job")).lower() == "true"
        if in_india(loc + (" remote" if remote else "")):
            title = j.get("Posting_Title") or j.get("Job_Opening_Name") or ""
            url = f"https://{key}/jobs/Careers/{j['id']}/{_slug(title)}?source=CareerSite"
            jobs.append(Job(str(j["id"]), title, url, loc or ("Remote" if remote else "")))
    return jobs


def kula(c, key, since):
    jobs, page, pages = [], 1, 1
    while page <= pages:
        params = {"accountName": key, "page": page, "type": "ats_job_post.index", "items": 99}
        d = _json(c, "GET", "https://careers.kula.ai/api/internal/ats_job_posts", params=params)
        pages, page = (d.get("meta") or {}).get("pages", 1), page + 1
        for j in d.get("data", []):
            a = j.get("ats_job") or {}
            loc = " / ".join(o.get("location") or o.get("name") or "" for o in a.get("offices") or [])
            if j.get("listed", True) and in_india(loc):
                url = f"https://careers.kula.ai/{key}/{j['id']}-{_slug(j['title']).lower()}"
                jobs.append(Job(str(j["id"]), j["title"], url, loc, parse_dt(j.get("launch_at")), a.get("job_description") or ""))
    return jobs


def freshteam(c, key, since):
    d = _json(c, "GET", f"https://{key}/hire/widgets/jobs.json")
    branches = {b["id"]: b for b in d.get("branches", [])}
    jobs = []
    for j in d.get("jobs", []):
        b = branches.get(j.get("branch_id")) or {}
        loc = ", ".join(filter(None, [b.get("city"), b.get("country_code")]))
        if not j.get("deleted") and in_india(loc + (" remote" if j.get("remote") else ""), b.get("country_code")):
            jobs.append(Job(str(j["id"]), j["title"], j["url"], loc, parse_dt(j.get("created_at")), j.get("description") or ""))
    return jobs


def pyjamahr(c, key, since):
    """key: 'company_uuid=…' or 'company_slug=…'. No posting date in the list."""
    url, jobs = f"https://api.pyjamahr.com/api/career/jobs/?{key}&page=1", []
    while url and len(jobs) < MAX_JOBS:
        d = _json(c, "GET", url)
        for j in d.get("results", []):
            loc = j.get("location") or ""
            if in_india(loc):
                link = (
                    f"https://app.pyjamahr.com/careers?{key}&job_id={j['id']}"
                    if key.startswith("company_uuid")
                    else f"https://jobs.pyjamahr.com/{key.split('=', 1)[1]}"
                )
                jobs.append(Job(str(j["id"]), j["title"], link, loc))
        url = d.get("next")
    return jobs


_TURBOHIRE = "https://thapi-stage2.azurewebsites.net/api"  # TurboHire's live API, despite the name


def turbohire(c, key, since):
    host, org = key.split("|")
    hdr = {"Origin": f"https://{host}", "Referer": f"https://{host}/"}  # token call 400s without these
    hdr["Authorization"] = "Bearer " + _json(c, "GET", f"{_TURBOHIRE}/token/noauth", headers=hdr)["access_token"]
    jobs = []
    for page in range(1, MAX_JOBS // 50 + 1):
        body = {"SortByV2": {"Key": "PostedDate", "Order": 2}, "PageNumber": page, "PageSize": 50}
        params = {"orgId": org, "pageType": 0}
        batch = _json(c, "POST", f"{_TURBOHIRE}/careerpagev2/filteredjobs", params=params, headers=hdr, json=body).get("Result") or []
        for j in batch:
            try:  # a JSON string: '[{"Address": "Guwahati, Assam, India", ...}]'
                loc = " / ".join(a.get("Address") or "" for a in json.loads(j.get("Location") or "[]"))
            except (ValueError, AttributeError):
                loc = str(j.get("Location") or "")
            if in_india(loc):
                url = f"https://{host}/job/publicjobs/{j['JobIdObfuscated']}" if j.get("JobIdObfuscated") else f"https://{host}/dashboardv2?orgId={org}"
                jobs.append(Job(j["JobId"], j["JobTitle"], url, loc, parse_dt(j.get("PublishedDate")), j.get("JobDescV2") or ""))
        if len(batch) < 50 or _older(parse_dt(batch[-1].get("PublishedDate")), since):
            break
    return jobs


def trakstar(c, key, since):
    jobs, offset = [], 0
    while offset < MAX_JOBS:
        params = {"client_name": key, "limit": 50, "offset": offset}
        d = _json(c, "GET", "https://jsapi.recruiterbox.com/v1/openings", params=params)  # no posting date here
        for o in d.get("objects", []):
            l = o.get("location") or {}
            loc = ", ".join(filter(None, [l.get("city"), l.get("state"), l.get("country")]))
            if in_india(loc + (" remote" if o.get("allows_remote") else ""), l.get("country")):
                jobs.append(Job(o["id"], o["title"], o["hosted_url"], loc, None, o.get("description") or ""))
        offset += 50
        if offset >= (d.get("meta") or {}).get("total", 0):
            break
    return jobs


def oraclehcm(c, key, since):
    host, site = key.split("|")
    base = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations&finder="

    def page(offset, extra=""):
        finder = f"findReqs;siteNumber={site},facetsList=LOCATIONS,limit=25,offset={offset},sortBy=POSTING_DATES_DESC{extra}"
        return (_json(c, "GET", base + finder).get("items") or [{}])[0]

    it = page(0)
    india = next((l["Id"] for l in it.get("locationsFacet") or [] if l.get("Name") == "India"), None)
    extra = f",locationId={india}" if india else ""
    if india:
        it = page(0, extra)
    jobs, offset = [], 0
    while True:  # newest first
        reqs = it.get("requisitionList") or []
        for r in reqs:
            if india or in_india(r.get("PrimaryLocation", ""), r.get("PrimaryLocationCountry")):
                url = f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r['Id']}"
                jobs.append(Job(str(r["Id"]), r["Title"], url, r.get("PrimaryLocation", ""), parse_dt(r.get("PostedDate")), r.get("ShortDescriptionStr") or ""))
        offset += 25
        if not reqs or offset >= min(it.get("TotalJobsCount", 0), MAX_JOBS) or _older(parse_dt(reqs[-1].get("PostedDate")), since):
            return jobs
        it = page(offset, extra)


def eightfold(c, key, since):
    host, domain = key.split("|")
    jobs, start, pcsx = [], 0, False
    while start < MAX_JOBS:  # newest first
        params = {"domain": domain, "start": start, "location": "India", "sort_by": "timestamp"}
        if pcsx:
            d = _json(c, "GET", f"https://{host}/api/pcsx/search", params={**params, "query": ""}).get("data") or {}
        else:
            r = c.get(f"https://{host}/api/apply/v2/jobs", params={**params, "num": 50})
            if r.status_code == 403 and start == 0:  # "Not authorized for PCSX": tenant is on the newer careers site
                pcsx = True
                continue
            r.raise_for_status()
            d = r.json()
        batch = d.get("positions") or []
        for p in batch:
            loc = " / ".join((p.get("locations") or [p.get("location") or ""]) + (p.get("standardizedLocations") or []))
            if in_india(loc):  # the location param is a fuzzy search, not a filter
                url = p.get("canonicalPositionUrl") or f"https://{host}/careers/job/{p['id']}"
                posted = parse_dt(p.get("postedTs") or p.get("t_create"))
                jobs.append(Job(str(p["id"]), p["name"], url, loc, posted, p.get("job_description") or ""))
        start += len(batch)
        if not batch or start >= d.get("count", 0) or _older(parse_dt(batch[-1].get("postedTs") or batch[-1].get("t_create")), since):
            break
    return jobs


def phenom(c, key, since):
    origin, locale = key.split("|")
    lang, country = locale.split("_", 1)
    body = {  # what the site's own search page posts; "Most recent" isn't honoured, so read all India pages
        "lang": locale, "deviceType": "desktop", "country": country, "pageName": "search-results", "ddoKey": "refineSearch",
        "sortBy": "Most recent", "subsearch": "", "from": 0, "jobs": True, "counts": True, "all_fields": ["country"],
        "size": 50, "clearAll": False, "jdsource": "facets", "isSliderEnable": False, "pageId": "page20",
        "siteType": "external", "keywords": "", "global": True, "selected_fields": {"country": ["India"]},
    }
    jobs = []
    while body["from"] < MAX_JOBS:
        rs = _json(c, "POST", f"{origin}/widgets", json=body, headers={"Origin": origin, "Referer": origin + "/"}).get("refineSearch") or {}
        batch = (rs.get("data") or {}).get("jobs") or []
        for j in batch:
            loc = " / ".join(j.get("multi_location") or [j.get("location") or ""])
            if in_india(f"{loc} {j.get('country', '')}"):
                url = f"{origin}/{country}/{lang}/job/{j['jobId']}"
                jobs.append(Job(j["jobId"], j["title"], url, loc, parse_dt(j.get("postedDate") or j.get("dateCreated")), j.get("descriptionTeaser") or ""))
        body["from"] += len(batch)
        if not batch or body["from"] >= rs.get("totalHits", 0):
            break
    return jobs


_SF_SEARCH = {"locale": "en_US", "sortBy": "date", "keywords": "", "location": "", "facetFilters": {}, "brand": "", "skills": [],
              "categoryId": 0, "alertId": "", "rcmCandidateId": ""}


def successfactors(c, key, since):
    """SAP SuccessFactors career sites. key: the site root, or a /go/<category>/<id> list page.
    Newer sites answer a JSON search API (newest first); older ones only render HTML pages of results."""
    p = urlsplit(key)
    origin = f"{p.scheme}://{p.netloc}"
    if "/go/" not in p.path:
        r = c.post(f"{origin}/services/recruiting/v1/jobs", json={**_SF_SEARCH, "pageNumber": 0})
        if r.status_code == 200 and "jobSearchResult" in r.text:  # older sites answer 401
            return _sf_api(c, origin, r.json(), since)
    return _sf_html(c, key, origin, since)


def _sf_location(j):
    """Location fields differ per site (jobLocationShort, primLocation, region, ...): join every one there is."""
    vals = []
    for k, v in j.items():
        if re.search(r"location|country|region", k, re.I) and "Coordinates" not in k:
            vals += [re.sub(r"<[^>]+>", "", x).strip() for x in (v if isinstance(v, list) else [v]) if isinstance(x, str)]
    return " / ".join(dict.fromkeys(v for v in vals if v))


def _sf_api(c, origin, d, since):
    jobs, page, seen = [], 0, 0
    while True:
        batch, dates = [x.get("response") or {} for x in d.get("jobSearchResult") or []], []
        for j in batch:
            try:
                posted = datetime.strptime(j.get("unifiedStandardStart") or "", "%m/%d/%y").replace(tzinfo=timezone.utc)  # en_US: 10/23/26
                dates.append(posted)
            except ValueError:
                posted = None
            loc = _sf_location(j)
            if in_india(loc):
                title = j.get("unifiedStandardTitle") or j.get("urlTitle") or ""
                url = f"{origin}/job/{j.get('unifiedUrlTitle') or j.get('urlTitle')}/{j['id']}-en_US/"
                jobs.append(Job(str(j["id"]), html.unescape(title), url, loc, posted))
        seen, page = seen + len(batch), page + 1
        # "newest first" is only roughly so: stop once a whole page is older than since
        if not batch or seen >= min(d.get("totalJobs", 0), MAX_JOBS) or (dates and _older(max(dates), since)):
            return jobs
        d = _json(c, "POST", f"{origin}/services/recruiting/v1/jobs", json={**_SF_SEARCH, "pageNumber": page})


_SF_LINK = re.compile(r"<a\b([^>]*\bjobTitle-link\b[^>]*)>(.*?)</a>", re.I | re.S)


def _sf_html(c, key, origin, since):
    """Result pages are a table (tr.data-row) or tiles; each job's title link appears twice (desktop + phone), and its
    location/date sit between its link and the next job's."""
    jobs, start, ids = [], 0, set()
    while start < MAX_JOBS:
        url = (f"{key.rstrip('/')}/{start}/?q=&sortColumn=referencedate&sortDirection=desc" if "/go/" in key
               else f"{origin}/search/?q=&sortColumn=referencedate&sortDirection=desc&startrow={start}")
        r = c.get(url)
        r.raise_for_status()
        links = [(m.start(), html.unescape(re.search(r'href="([^"]+)"', m[1])[1]), m[2]) for m in _SF_LINK.finditer(r.text) if 'href="' in m[1]]
        firsts = [i for i, ln in enumerate(links) if i == 0 or ln[1] != links[i - 1][1]]
        new, dates = 0, []
        for n, i in enumerate(firsts):
            pos, href, inner = links[i]
            block = r.text[pos: links[firsts[n + 1]][0] if n + 1 < len(firsts) else len(r.text)]
            jid = (re.search(r"/(\d+)/?$", href.split("?")[0]) or [None, href])[1]
            if jid in ids:
                continue
            ids.add(jid)
            new += 1
            loc = " / ".join(dict.fromkeys(" ".join(x.split()) for x in re.findall(
                r'(?:class="jobLocation"|\bid="[^"]*-section-(?:location|city|state|country)-value")[^>]*>\s*([^<]+)', block)))
            date = re.search(r'(?:class="jobDate"|\bid="[^"]*-section-date-value")[^>]*>\s*([^<]+)', block)
            posted = parse_dt(date[1].strip()) if date else None
            dates += [posted] if posted else []
            if in_india(loc):
                title = " ".join(html.unescape(re.sub(r"<[^>]+>", "", inner)).split())
                jobs.append(Job(jid, title, urljoin(origin, href), loc, posted))
        total = re.search(r"Results\s*<b>[^<]*</b>\s*of\s*<b>\s*([\d,]+)", r.text)
        start += new
        if not new or (total and start >= int(total[1].replace(",", ""))) or (dates and _older(max(dates), since)):
            return jobs
    return jobs


_GET_JSON = "async (url) => { const r = await fetch(url); return [r.status, await r.text()]; }"


def icims(c, key, since):
    """iCIMS career sites (the 'careers-home' search, ex-Jibe). key: the site's job list page.
    Some sites give plain HTTP clients an empty list and serve only real browsers: those are read from inside one."""
    p = urlsplit(key)
    origin = f"{p.scheme}://{p.netloc}"
    if _json(c, "GET", f"{origin}/api/jobs?internal=false&limit=1").get("totalCount"):
        return _icims_jobs(origin, lambda url: _json(c, "GET", url))
    with browser_page(key, wait="load") as (pg, _):
        def fetch(url):
            status, text = pg.evaluate(_GET_JSON, url)
            if status != 200:
                raise RuntimeError(f"icims jobs HTTP {status}")
            return json.loads(text)
        return _icims_jobs(origin, fetch)


def _icims_jobs(origin, fetch):
    api = f"{origin}/api/jobs?internal=false&location=India&limit=100"
    try:  # newest first matters past MAX_JOBS; some sites 503 on it, so then take their default order
        fetch(api + "&sortBy=posted_date&descending=true&page=1")
        api += "&sortBy=posted_date&descending=true"
    except (httpx.HTTPStatusError, RuntimeError):
        pass
    jobs, page = [], 1
    while len(jobs) < MAX_JOBS:
        d = fetch(f"{api}&page={page}")
        batch = [x.get("data") or {} for x in d.get("jobs") or []]
        for j in batch:
            loc = j.get("full_location") or ", ".join(filter(None, [j.get("city"), j.get("state"), j.get("country")]))
            if in_india(loc, j.get("country_code")):
                url = (j.get("meta_data") or {}).get("canonical_url") or f"{origin}/jobs/{j['slug']}"
                jobs.append(Job(str(j.get("req_id") or j["slug"]), j["title"], url, loc, parse_dt(j.get("posted_date")), j.get("description") or ""))
        if not batch or page * 100 >= d.get("totalCount", 0):
            break
        page += 1
    return jobs


ADAPTERS = {f.__name__: f for f in (
    workday, greenhouse, lever, ashby, smartrecruiters, workable, keka, darwinbox, kula, zoho, freshteam,
    pyjamahr, turbohire, trakstar, oraclehcm, eightfold, phenom, successfactors, icims,
)}


# ---------------------------------------------------------------- detection

# (ats, regex[, key format]). List order is the preference when one page points at several boards.
PATTERNS = [
    ("workday", r"(?<![\w-])(([\w-]+)\.wd\d+\.myworkdayjobs\.com)/(?:wday/cxs/[\w-]+/)?(?:[a-z]{2}-[a-z]{2}/)?([\w-]+)"),
    ("workday", r"(?<![\w-])((?:wd\d+\.)?myworkdaysite\.com)/(?:[a-z]{2}-[a-z]{2}/)?recruiting/([\w-]+)/([\w-]+)"),
    ("greenhouse", r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([\w-]+)"),
    ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([\w-]+)"),
    ("lever", r"(?<![\w.])(?:jobs\.(?:eu\.)?lever\.co|api\.lever\.co/v0/postings)/([\w-]+(?:\.[a-z]+)?)"),  # slugs like oaknorth.ai
    ("ashby", r"(?:jobs\.ashbyhq\.com|api\.ashbyhq\.com/posting-api/job-board)/([\w.-]+)"),
    ("smartrecruiters", r"(?:careers|jobs|join)\.smartrecruiters\.com/(?:my-applications/)?([a-z][\w-]*)"),
    ("workable", r"apply\.workable\.com/(?:api/v\d/(?:widget/)?accounts/)?([\w-]+)"),
    ("keka", r"(?<![\w-])([\w-]+)\.keka\.com/careers/api/embedjobs/(?:js/)?(?:default/active/)?(" + UUID + ")"),
    ("keka", r"(?<![\w-])([\w-]+)\.keka\.com/careers"),
    ("darwinbox", r"(?<![\w-])([\w-]+\.darwinbox\.(?:in|com))/ms/candidate"),
    ("kula", r"careers\.kula\.ai/([\w-]+)"),
    ("zoho", r"(?<![\w-])([\w-]+\.zohorecruit\.(?:in|com|eu))/jobs/careers"),
    ("zoho", r"(?<![\w.-])((?:[\w-]+\.)+[a-z]{2,})/jobs/Careers\b"),  # Zoho Recruit on the company's own domain (capital C is Zoho's)
    ("freshteam", r"(?<![\w-])([\w-]+\.freshteam\.com)/(?:jobs|hire)"),
    ("pyjamahr", r"pyjamahr\.com/[^\s\"'<>]*?company_uuid=(\w+)", "company_uuid={}"),
    ("pyjamahr", r"jobs\.pyjamahr\.com/([\w-]+)", "company_slug={}"),
    ("turbohire", r"(?<![\w-])([\w-]+\.turbohire\.co)/dashboardv2\?orgId=([\w-]+)"),
    ("trakstar", r"(?<![\w-])([\w-]+)\.hire\.trakstar\.com"),
    ("trakstar", r"(?<![\w.-])([\w-]+)\.recruiterbox\.com"),
    ("oraclehcm", r"(?<![\w-])([\w-]+\.fa\.[\w-]+\.oraclecloud\.com)(?::443)?/hcmUI/CandidateExperience/[\w-]+/sites/([\w-]+)"),
    ("oraclehcm", r"(?<![\w-])([\w-]+\.fa\.[\w-]+\.oraclecloud\.com)"),  # site number looked up below
    ("eightfold", r"(?<![\w-])([\w-]+\.eightfold\.ai)"),  # domain looked up below
    ("eightfold", r"(?<![\w.-])((?:[\w-]+\.)+[a-z]{2,})/careers\?(?:[^\s\"'<>]*&(?:amp;)?)?pid=\d"),  # on the company's own domain
    ("phenom", r"cdn\.phenompeople\.com/CareerConnectResources/(?:pp/)?\w+/([a-z]{2}_[a-z]+)/"),  # origin = the page
    ("successfactors", r"rmkcdn\.successfactors\.com|jobs2web\.com"),  # SuccessFactors career site builder; key = the page
    ("icims", r"app\.jibecdn\.com"),  # iCIMS careers-home; key = the page
]
JUNK = {
    "wday", "login", "assets", "embed", "cdn", "api", "j", "app", "static", "www", "w", "jsapi", "careerhub",
    "my-applications", "vs-errors", "trust", "community", "docs", "cdn-cgi", "hp-sandbox",
}


def detect(final_url, page):
    """Known job boards referenced by a page -> [(ats, key)], best first: the page's own URL, then PATTERNS order, then frequency."""
    hay = html.unescape(final_url + " " + page).replace("\\/", "/")
    found = Counter()
    for rank, (ats, pat, *fmt) in enumerate(PATTERNS):
        for m in re.finditer(pat, hay, re.I):
            g = m.groups()
            if any(x.split(".")[0].lower() in JUNK or re.fullmatch(r"[a-z]{2}-[a-z]{2}", x, re.I) for x in g):
                continue
            key = fmt[0].format(*g) if fmt else "|".join(g)
            found[(m.group(0).lower() not in final_url.lower(), rank, ats, key)] += 1
    out = []
    for (_, _, ats, key), _n in sorted(found.items(), key=lambda kv: (kv[0][0], kv[0][1], -kv[1])):
        if ats == "eightfold":
            dom = re.search(re.escape(key) + r"/[^\s\"'<>]*?[?&]domain=([\w-]+(?:\.[\w-]+)+)", hay, re.I)
            default = key.split(".")[0] + ".com" if key.endswith(".eightfold.ai") else ".".join(key.split(".")[-2:])
            key = f"{key}|{dom[1] if dom else default}"
        elif ats == "oraclehcm" and "|" not in key:
            site = re.search(r"siteNumber[\"'=:\s]+([\w-]+)", hay)
            key = f"{key}|{site[1] if site else 'CX_1'}"
        elif ats == "phenom":  # site root = everything before /{country}/{lang}, e.g. virtusa.com/careers/job-search
            p, (lang, country) = urlsplit(final_url), key.lower().split("_", 1)
            i = p.path.lower().find(f"/{country}/{lang}")
            key = f"{p.scheme}://{p.netloc}{p.path[:i] if i > 0 else ''}|{key.lower()}"
        elif ats == "successfactors":  # the site root, or the /go/<category>/<id> list the source points at
            p = urlsplit(final_url)
            go = re.match(r"(?:/[^/]+)?/go/[^/]+/\d+", p.path)
            key = f"{p.scheme}://{p.netloc}{go[0] if go else ''}"
        elif ats == "icims":
            p = urlsplit(final_url)
            key = f"{p.scheme}://{p.netloc}{p.path}"
        if (ats, key) not in out:
            out.append((ats, key))
    return out

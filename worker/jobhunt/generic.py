"""Jobs from career pages that aren't on a known board.

Order: JSON-LD JobPosting -> decision model classifies the page's links (or a link heuristic without a key).
The page fingerprint is the cursor: an unchanged page isn't re-extracted, so the model only runs when the page changes,
and then only on links it hasn't answered for before (link_answers).
"""
import hashlib
import html
import json
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from jobhunt import ats, decide
from jobhunt.ats import Job, in_india, parse_dt
from jobhunt.db import connect, now

TRACKING = re.compile(
    r"^(utm_|gclid$|fbclid$|_gl$|_ga$|mc_|ref$|refid$|source$|src$|trk|trackingid$|sessionid$|jsessionid$|gh_src$|lever-)", re.I
)
# never anchor a source to these: the user wants company boards, and their pages mix in other companies' jobs.
# Social sites too: a job card's "share on X" button is not a job.
AGGREGATOR = re.compile(
    r"(^|\.)(linkedin|naukri|indeed|glassdoor|wellfound|angel|instahyre|cutshort|iimjobs|foundit|monster|hirist|internshala"
    r"|facebook|instagram|twitter|x|youtube|whatsapp|wa|t|pinterest|reddit)\.(com|me|in|co)$", re.I
)
JOBISH = re.compile(
    r"open (positions|roles)|openings|vacanc|view (all )?(jobs|roles|openings|positions)|see (all )?(jobs|openings|roles)"
    r"|job (openings|board|listings)|search jobs|explore (jobs|roles|careers|opportunities)|current opportunities"
    r"|join (us|our team)|we.?re hiring|^(jobs|careers|apply now)$",
    re.I,
)
JOBPATH = re.compile(r"/(jobs?|careers?|positions?|openings?|vacanc\w*|requisitions?|opportunit\w*|roles?|job-?details?)/[^/]+", re.I)
ROLE = re.compile(
    r"\b(engineer|developer|programmer|architect|scientist|analyst|manager|lead|head|director|designer|intern|internship"
    r"|trainee|associate|executive|specialist|consultant|officer|coordinator|administrator|representative|technician"
    r"|sde|sre|devops|qa|tester|writer|editor|accountant|recruiter|strategist|researcher|fellow|apprentice)\b",
    re.I,
)
NAV = re.compile(
    r"^(home|about|careers?|jobs|life at|our |benefits|apply|view|see |learn|read|know more|contact|log ?in|sign|privacy"
    r"|terms|blog|news|faq|explore|join|meet|why )",
    re.I,
)


# link texts that say nothing about the job; the card's heading just before the link is used instead
GENERIC_LINK = re.compile(
    r"^(apply( now| here| today)?|view( details| job| role| position| opening| more)?|know more|learn more|read more"
    r"|more details|details|see details|see more|click here|open|>+|→)$",
    re.I,
)


class Page(HTMLParser):
    """One pass over the HTML: absolute links with their text, visible text, JSON-LD blobs."""

    def __init__(self, base):
        super().__init__()
        self.base, self.links, self.text, self.jsonld = base, [], [], []
        self.row_link = {}  # text in a table row -> the row's only link (job tables put the link on a code, the title beside it)
        self._href, self._atext, self._skip, self._script, self._recent, self._row = None, [], None, None, [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a":
            self._href, self._atext = a.get("href"), []
        elif tag == "tr":
            self._row = ([], [])
        elif tag in ("script", "style", "noscript", "svg", "template"):
            self._skip = tag
            self._script = [] if a.get("type") == "application/ld+json" else None

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            href = self._href.strip()
            text = " ".join(" ".join(self._atext).split())
            if len(text.split()) > 12:  # the whole card is the link: its first line is the title
                text = next(" ".join(t.split()) for t in self._atext if t.strip())
            if (not text or GENERIC_LINK.match(text)) and self._recent:  # "Apply" under a "Backend Engineer" heading
                text = next((t for t in reversed(self._recent) if ROLE.search(t)), self._recent[-1])
            # /cdn-cgi/ is Cloudflare's own (email protection, challenges): a fresh token on every load would change the fingerprint
            if href and not href.startswith(("#", "mailto:", "tel:", "javascript:")) and "/cdn-cgi/" not in href:
                url = canon(urljoin(self.base, href))
                if not ROLE.search(text):  # the text near a bare "Apply" was a date, city or "Full Time": the URL often names the role
                    text = _slug_title(url) or text
                self.links.append((url, text))
                if self._row is not None:
                    self._row[0].append(url)
            self._href = None
        elif tag == "tr" and self._row is not None:
            links, texts = self._row
            if len(set(links)) == 1:
                for t in texts:
                    self.row_link.setdefault(t, links[0])
            self._row = None
        elif tag == self._skip:
            if self._script is not None:
                self.jsonld.append("".join(self._script))
            self._skip = self._script = None

    def handle_data(self, data):
        if self._skip:
            if self._script is not None:
                self._script.append(data)
            return
        self.text.append(data)
        if self._href is not None:
            self._atext.append(data)
        elif data.strip():
            self._recent = (self._recent + [" ".join(data.split())])[-3:]  # title / location / team above a link
        if self._row is not None and data.strip():
            self._row[1].append(" ".join(data.split()))


def _slug_title(url):
    """'/jobs/senior-backend-engineer-4521' -> 'Senior Backend Engineer', only when that reads like a role."""
    words = [w for w in re.split(r"[-_+]", unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]).split(".")[0])
             if w and not re.fullmatch(r"\d+|[0-9a-f]{8,}|[a-z]{0,2}\d+\w*", w, re.I)]
    title = " ".join(w if w.isupper() else w.capitalize() for w in words)
    return title if len(words) >= 2 and ROLE.search(title) else None


def parse(base, page_html, into=None):
    p = Page(base)
    p.feed(page_html)
    p.close()
    if into:  # merge an iframe's page into its parent's
        into.links += p.links
        into.text += p.text
        into.jsonld += p.jsonld
        into.row_link = {**p.row_link, **into.row_link}
        return into
    return p


def canon(url):
    """Drop tracking params and plain #anchors (keep #/hash-routes), so the same job always has the same id."""
    p = urlsplit(url)
    q = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k)])
    if p.netloc.endswith("linkedin.com"):  # job id is in the path; every query param is per-visit tracking
        q = ""
    frag = p.fragment if p.fragment.startswith(("/", "!")) else ""
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/") or "/", q, frag))


def job_links(p, limit=3):
    """Links that look like they lead to the job list ("View open positions", /jobs/...)."""
    out = []
    for url, text in p.links:
        if url == canon(p.base) or url in out or not url.startswith("http") or AGGREGATOR.search(urlsplit(url).netloc):
            continue
        if JOBISH.search(text) or re.search(r"/(jobs|openings|positions|vacancies|job-search|search-jobs)(/|$|\?)", urlsplit(url).path, re.I):
            out.append(url)
    return out[:limit]


def _lines(p):
    return [" ".join(t.split()) for t in p.text if t.strip()]


def fingerprint(p):
    roles = sorted({line for line in _lines(p) if ROLE.search(line) and len(line.split()) <= 12})  # catches link-less job lists
    return hashlib.sha1(json.dumps([sorted(set(p.links)), roles, p.jsonld]).encode()).hexdigest()


def _id(base, url, title):
    return url if url != canon(base) else f"{canon(base)}#{re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')}"


def jsonld_jobs(p):
    jobs = []

    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            kind = o.get("@type")
            if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                title = html.unescape(str(o.get("title") or o.get("name") or "")).strip()
                url = canon(urljoin(p.base, str(o.get("url") or p.base)))
                places = o.get("jobLocation") or []
                addrs = [(x.get("address") or {}) for x in (places if isinstance(places, list) else [places]) if isinstance(x, dict)]
                loc = " / ".join(
                    ", ".join(str(a.get(k)) for k in ("addressLocality", "addressRegion", "addressCountry") if isinstance(a, dict) and a.get(k))
                    for a in addrs
                )
                if title:
                    jobs.append(Job(_id(p.base, url, title), title, url, loc, parse_dt(o.get("datePosted")), html.unescape(str(o.get("description") or ""))))
            for v in o.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    for blob in p.jsonld:
        try:
            walk(json.loads(blob))
        except ValueError:
            pass
    return jobs


def _domain(url):
    return ".".join(urlsplit(url).netloc.split(".")[-2:])


def heuristic_jobs(p):
    """No-LLM fallback: links whose text reads like a role title and that lead to a job page
    (a job-ish path or id on this site, or any page on another site, i.e. an external board)."""
    jobs = {}
    for url, text in p.links:
        if not url.startswith("http") or NAV.match(text) or not 2 <= len(text.split()) <= 14 or not ROLE.search(text):
            continue
        path = urlsplit(url).path
        if _domain(url) != _domain(p.base) or JOBPATH.search(path) or re.search(r"\d{4,}|[0-9a-f]{8}-", path):
            jobs.setdefault(url, Job(url, text, url))
    return list(jobs.values())


ITEM_Q = "Is item {i} an individual job opening: one specific role someone can apply for (not a team, location, category, testimonial or 'all jobs' page)?"


def decided_jobs(p):
    """Ask the decision model which items on the page are job openings. Items are the page's links (titled by
    their text, or the heading above a bare "Apply") plus short role-like text lines that have no link of their own."""
    host = urlsplit(p.base).netloc
    items = [
        (u, t) for u, t in dict.fromkeys(p.links)
        if t and len(t.split()) <= 16 and not NAV.match(t) and not AGGREGATOR.search(urlsplit(u).netloc) and "{{" not in t  # unrendered template
    ][:300]
    titled = {t.lower() for _, t in items}
    items += [
        (None, line) for line in dict.fromkeys(_lines(p))
        if ROLE.search(line) and 2 <= len(line.split()) <= 12 and not NAV.match(line) and line.lower() not in titled
    ][:100]
    # a page changes when one job is added, so remember every link's answer and only ask about the links that are new
    keys = [hashlib.sha1(f"{p.base}\n{u}\n{t}".encode()).hexdigest()[:16] for u, t in items]
    conn = connect()
    probs = dict(conn.execute(f"SELECT key, p FROM link_answers WHERE key IN ({', '.join('?' * len(keys))})", keys)) if keys else {}
    todo = [(k, it) for k, it in dict(zip(keys, items)).items() if k not in probs]
    try:
        for start in range(0, len(todo), 100):  # 100 questions per request keeps well inside the context limit
            batch = todo[start:start + 100]
            shown = "\n".join(
                f"{i}. {t[:100]} " + (
                    "(text on the page, no link)" if u is None else f"-> {(u.split(host, 1)[1] or '/') if urlsplit(u).netloc == host else u}"
                )
                for i, (_, (u, t)) in enumerate(batch)
            )
            answers = decide.decide("extract", {"careers_page": p.base, "items": shown}, {f"i{i}": ITEM_Q.format(i=i) for i in range(len(batch))})
            probs.update({k: answers[f"i{i}"] for i, (k, _) in enumerate(batch) if answers.get(f"i{i}") is not None})  # a refusal is asked again
    finally:  # what was answered before a failed batch is kept
        with conn:
            conn.executemany("INSERT OR REPLACE INTO link_answers VALUES (?, ?, ?)", [(k, probs[k], now()) for k in dict.fromkeys(keys) if k in probs])
        conn.close()
    jobs = []
    for k, (u, t) in zip(keys, items):
        if probs.get(k, 0) >= 0.6 and not re.search(r"\bjobs$", t, re.I):  # 0.5 is the model shrugging; "DevOps Jobs" is a category
            url = u or p.row_link.get(t) or canon(p.base)
            dated = ats.AGO.search(t)  # a card's "Posted 2 years ago" is often part of its link text
            jobs.append(Job(_id(p.base, url, t), t, url, posted_at=parse_dt(dated[0]) if dated else None))
    base, linked = canon(p.base), {}
    for j in jobs:  # a row's code link ("J0926-2010") and its title line can both be picked: keep the one that reads like a role
        if j.url != base and (j.url not in linked or (ROLE.search(j.title) and not ROLE.search(linked[j.url].title))):
            linked[j.url] = j
    titles = {j.title.lower() for j in linked.values()}  # same role as text and as a link: keep the link
    return list(linked.values()) + [j for j in jobs if j.url == base and j.title.lower() not in titles]


def fetch(url, js=False):
    """-> (Page, used_browser). Plain HTTP unless the page is a JS shell or blocks plain clients."""
    plain = None
    if not js:
        try:
            with ats.client() as c:
                r = c.get(url)
            if r.status_code < 400:
                plain = parse(str(r.url), r.text)
                if len(" ".join(_lines(plain)).split()) >= 150 or len(plain.links) >= 25:
                    return plain, False
        except httpx.HTTPError:  # e.g. a server missing its intermediate cert: browsers cope, Python's ssl doesn't
            pass
    try:
        with ats.browser_page(url) as (pg, _):
            try:  # some pages never fire "load" (a request that never ends): don't fail on it, just wait a bit
                pg.wait_for_load_state("load", timeout=15_000)
            except Exception:
                pass
            pg.wait_for_timeout(2500)
            pg.mouse.wheel(0, 30000)  # lazy-loaded lists
            pg.wait_for_timeout(1500)
            p = None
            for f in pg.frames:  # job boards are often iframes
                try:
                    p = parse(f.url, f.content(), into=p) if p else parse(pg.url, f.content())
                except Exception:  # frame detached mid-read
                    pass
    except Exception:
        if plain:  # bot protection that lets plain HTTP through but not a browser (Akamai at ITC Infotech): use what we got
            return plain, False
        raise
    return p or Page(url), True


def crawl(url, js=False, prev_hash=None, follow=True):
    """-> (jobs, hash, source updates). jobs is None when the page is unchanged since prev_hash.

    The hash is '<extractor>:<page fingerprint>', so switching extractor (e.g. adding a decision-model key) re-extracts
    once, and the worker treats that crawl as a fresh baseline instead of flooding the judge with 'new' jobs.
    """
    p, js = fetch(url, js)
    jobs, mode = jsonld_jobs(p), "jsonld"
    if not jobs:
        mode = "decide2" if decide.enabled() else "heuristic"  # bump the name when extraction changes: next crawl re-baselines
    h = f"{mode}:{fingerprint(p)}"
    if h == prev_hash:
        return None, h, {}
    if not jobs:
        jobs = heuristic_jobs(p) if mode == "heuristic" else decided_jobs(p)
    updates = {"ats": "generic_js" if js else "generic"}
    if not jobs and not js:  # a page with enough text to pass as loaded, whose job list only appears once scripts run
        try:
            rendered = crawl(url, True, prev_hash, follow=False)
        except Exception:  # bot protection that lets plain HTTP through but not a browser (Persistent, ITC Infotech)
            rendered = (None,)
        if rendered[0]:  # from now on the source is read in the browser
            return rendered
    if not jobs and follow:  # a landing page: find the real job list once and remember it as the source's key
        for link in job_links(p, 2):
            try:
                sub_jobs, sub_hash, sub_updates = crawl(link, False, None, follow=False)
            except Exception:  # a dead "View openings" link (Ola's points at an internal server) mustn't fail the page itself
                continue
            if sub_jobs:
                return sub_jobs, sub_hash, {**sub_updates, "ats_key": link}
    return [j for j in jobs if in_india(j.location)], h, updates

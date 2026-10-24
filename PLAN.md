# Job Hunt Agent — Plan

Watch ~400 career pages, catch new postings within the hour, have an LLM judge each one against your resume, and put the good ones in a queue you work through by hand. Fetching and judging are fully automated; applying is you.

## Your rules (non-negotiable)

- Poll every 1–2 h. Each company has its own cursor, so the LLM never judges the same job twice.
- No LLM call for postings older than **3 days** (setting).
- Purge jobs/queue older than **7 days**.
- Resume and company list are updatable at any time (CSV / Excel import).
- **Jeevrit is production and stays untouched.** The job hunter runs on your laptop for now. If it moves to the Oracle box later, it goes in as a separate, outbound-only stack (see *Box isolation*).
- Out of scope for now: mass HR emails, auto-submit.

## What your spreadsheet actually contains (probed 2026-10-24)

`bengaluru_hyderabad_official_careers_found (1).csv`: 532 rows (359 Bengaluru, 173 Hyderabad) but only **412 unique URLs** (e.g. ABB + ABB India share one). The `job_board_platform` column says 482 are "company careers / other". Fetching each URL once tells a different story:

| Outcome | URLs | How we watch it |
|---|---|---|
| Fronts a known ATS (fingerprint in redirect/HTML) | **210** | ATS JSON API: structured, dated, cheap |
| — Workday | 57 | biggest single win |
| — Greenhouse 11, Lever 9, SmartRecruiters 8, Ashby 6, Workable 4 | 38 | public JSON APIs |
| — SuccessFactors 14, Darwinbox 14, Eightfold 12, Keka 10, iCIMS 9, Oracle HCM 9, Phenom 8, Zoho 8, Kula 6, Freshteam 5, others | ~115 | adapter if the ATS has JSON, else generic |
| Plain HTML, no ATS found | 114 | generic extractor |
| JS shell (< 150 words of HTML) | 34 | Playwright render, then generic |
| JSON-LD `JobPosting` on landing page | 5 | parsed directly |
| Failed (32× 403, 18 Cloudflare challenges, 4× 429, 4× 404, timeouts) | 53 | retry with Playwright; else shown as "blocked" in UI for you to fix the link |

Many `careers_url`s are landing pages, not the job list. The one-time classifier follows them to the real list or ATS board.

## Architecture (laptop, since 2026-10-24)

```
 Your laptop (Windows) ───────────────────────────────────────────────────────────
 │  Task Scheduler "Job Hunt": at login + every 10 min (ignored while running)  │
 │   └─ pythonw -m jobhunt.app   (one process, below-normal priority)           │
 │        ├─ watcher thread ──► career pages / board APIs      (outbound)       │
 │        │                └──► TypeSafe Jev / OpenAI Decisions (outbound)      │
 │        └─ http://127.0.0.1:8000  queue page + JSON API (this laptop only)    │
 │   ~/.jobhunt/  jobhunt.db (SQLite, WAL) · backups/ · jobhunt.log             │
 ──────────────────────────────────────────────────────────────────────────────────
   Phone (optional): Tailscale on laptop + phone, then `tailscale serve --bg 8000`
```

**Data location:** the data lives in `~/.jobhunt`, outside the project folder.
- Not in the project folder: it syncs to Dropbox, which corrupts a live SQLite file.
- Not under AppData: Store Python silently redirects writes there into its own package folder.

**Later:** moving to the Oracle box is the same code in Docker, as described in *Box isolation*.

## Stack

| Piece | Choice | Why |
|---|---|---|
| Everything | Python 3.12 (venv in `worker/.venv`) | best scraping ecosystem |
| HTTP | `httpx` + a thread pool | ATS APIs + static pages; plain sync code, no asyncio |
| HTML | stdlib `html.parser` | link/text extraction; JSON-LD via stdlib `json` |
| JS pages | `playwright`, headless Chromium shell only | only for sources flagged JS/blocked; max 2 at once; images, fonts and video blocked |
| Judge | TypeSafe Jev / OpenAI Decisions API over `httpx` | yes/no probabilities, no text LLM |
| DB | `sqlite3` (stdlib), WAL | single user, ~10k rows; no ORM, no Redis, no Celery |
| Scheduler | plain loop + thread pool | `next_check_at` per source *is* the schedule |
| API + UI | stdlib `http.server` + one HTML file, vanilla JS | no new dependencies, no build step |
| Resume | `pypdf` | PDF → text |
| Import | `csv` (stdlib) + `openpyxl` | CSV or Excel uploads |

Not used: LLM "web fetch/search" for polling. Plain HTTP is free and faster. Web search is reserved for later, to find careers URLs for the ~970 companies that don't have one yet.

## Data model

```sql
sources  (id, url UNIQUE, companies, city, ats, ats_key, active, interval_min,
          next_check_at, last_ok_at, fail_count, last_error, list_hash, bootstrapped_at)
jobs     (id, source_id, external_id, UNIQUE(source_id, external_id),
          url, title, location, posted_at, first_seen_at, last_seen_at,
          description, status, score, verdict_json, resume_id,
          apply_status, applied_at)
resumes  (id, uploaded_at, filename, pdf BLOB, text, profile_json, active)
settings (key PRIMARY KEY, value_json)        -- thresholds, keywords, intervals
llm_calls(id, at, kind, model, in_tokens, out_tokens)   -- cost visibility
```

`jobs.status`: `baseline | filtered | stale | pending_eval | matched | rejected | error | expired`
`jobs.apply_status`: `NULL | queued | opened | applied | skipped`

## The watcher

**The per-company cursor has three parts:**
1. `list_hash`: hash of the normalized job list (sorted ATS ids, or the sorted link set for generic pages). If it hasn't changed, the source is done: no diff, no LLM.
2. `jobs(source_id, external_id)`: the seen set. A changed list is diffed against it, and only unseen ids continue.
3. `bootstrapped_at`: on the first crawl of a source, every current job is stored as `baseline` without an LLM call, except jobs whose ATS date is within 3 days. Without this, day one would send ~5k open jobs to the LLM.

**Adapters** each return `JobStub(external_id, title, url, location, posted_at, description?)`. They're built in order of how many sources they cover:

| ATS | URLs | Endpoint | Posted date |
|---|---|---|---|
| Workday | 57 | `POST {host}/wday/cxs/{tenant}/{site}/jobs` | `postedOn` ("Posted 2 Days Ago") |
| Greenhouse | 11 | `GET boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | `first_published` / `updated_at` |
| Lever | 9 | `GET api.lever.co/v0/postings/{co}?mode=json` | `createdAt` |
| SmartRecruiters | 8 | `GET api.smartrecruiters.com/v1/companies/{co}/postings` | `releasedDate` |
| Ashby | 6 | `GET api.ashbyhq.com/posting-api/job-board/{org}` | `publishedAt` |
| Workable | 4 | `GET apply.workable.com/api/v1/widget/accounts/{co}` | `published_on` |
| Eightfold, Oracle HCM | 21 | public JSON endpoints (verify in Phase 2) | yes |
| Darwinbox, Keka, Zoho, SuccessFactors, … | ~70 | inspect; adapter only if it has JSON | — |

Big global employers (Workday, Eightfold, …) list thousands of jobs worldwide, so adapters filter to India/Bengaluru/Hyderabad/Remote **before** storing, and stop paging once postings are older than the window.

**Generic pages:** httpx fetch → JSON-LD `JobPosting` if present → otherwise the link set. If `list_hash` changed, a cheap LLM call extracts `[{title, url, location}]` from the links. JS shells go through Playwright first.

**Posted date:** the ATS/JSON-LD date when available. Otherwise `first_seen_at`, which is accurate to within one poll once the source is bootstrapped.

**Schedule:** the loop ticks every 60 s and crawls sources where `next_check_at <= now`. Interval: 60 min by default, 120 for Playwright sources, ±10% jitter. Concurrency: 8 overall, 1 per host. Failures back off ×2 up to 24 h, and after 5 in a row the source shows as `broken` in the UI. "Check now" in the UI sets `next_check_at = now`.

## LLM judging

New jobs pass through gates, cheapest first. Only the survivors cost money:

0. **Location** (at fetch time, not a gate): India only, anywhere in India. No city filter (your call, 2026-10-24).
1. **Age**: `posted_at` > 3 days → `stale`
2. **Title**: include/exclude keywords (suggested from your resume, editable). "Member of Technical Staff"/MTS passes; senior/lead/manager/principal titles don't.
3. **Dedupe**: same company + normalized title already judged this week
4. **Fetch JD** (`judge.describe`, trimmed to ~8k chars). A list's text counts only if it's ≥ 400 chars; Oracle HCM and Phenom lists are always summaries. Then the board's detail API (Workday, SmartRecruiters, Oracle HCM), else the job page (Phenom's embedded job data, JSON-LD `JobPosting`, page text), rendered in a browser if it's JS-only.
5. **Verdict**: one **decision-model** call (`jobhunt/decide.py`). No text-generating LLM anywhere.

   - **Provider:** TypeSafe **Jev** (`POST api.typesafe.ai/v1/systemone`, model `jev-latest`) by default. OpenAI **Decisions API** (`POST /v1/decisions`, `gpt-6-luna`) is the fallback via `DECISION_PROVIDER=openai`.
   - **Input:** `state` = rubric + resume, then the job (title, company, location, description).
   - **Questions:** six yes/no ("noul") questions, each returning a probability:
     - `fit`: credible applicant, **leaving aside experience asks of up to 3 years** (your rule, 2026-10-24).
     - `skills`, `experience` and `role` are the ✓/✗ reasons shown on the queue card.
     - `experience`: "open to someone with under 4 years?" Measured on real JDs: asks of 4+ years ≤ 0.07; 0–3 years, internships and unstated ≥ 0.77.
     - `india` (based in India or remote-open-to-India?) drops foreign jobs from company pages that show no location. Measured: abroad ≤ 0.31, India ≥ 0.94.
     - `posting` (one specific job, not a list/category page?) drops category pages that read like roles. Measured: category ≤ 0.11, real postings ≥ 0.55.
   - **Queue rule** (`judge.wanted`): `role ≥ 50%`, `experience ≥ 50%`, `india ≥ 50%`, `posting ≥ 30%`, and (`fit ≥ min_score` (35%) or `skills ≥ 80%`) → `matched`, `apply_status = queued`.
   - **Settings are live:** each pass re-applies the queue rule to stored probabilities and reopens filtered jobs the gates now allow. Changing the cutoff or keyword lists costs no model calls.
   - **Stored:** all four probabilities in `jobs.verdict`.
   - **OpenAI only:** the resume is sent as a separate developer message, which gets cache hits (~1,040 cached tokens per call). Jev has no cache, and doesn't need one at its price.

**Company-built pages:** the same decision model classifies a changed page's links (titled by their text, or the heading above a bare "Apply"), plus short role-like text lines: "is item N an individual job opening?" (≥ 0.6). Each link's answer is kept in `link_answers`, so a changed page only asks about its new links. Before that, re-asking every link on each change was ~80% of the bill: about 5% of company-page checks see a change, and a 400-link page took 4 requests.

**Resume:** `python -m jobhunt.resume <pdf>` (Settings upload later) → text via pypdf → active resume. Jobs judged in the last `max_age_days` and not applied or skipped are re-judged against the new resume.

**Cost (measured):** about 1,600–2,300 input tokens per verdict (~$0.0001 on Jev). The whole first day cost **$0.12**: the 1,387-job backlog plus extraction on 207 company pages, twice. Per-call usage is in `llm_calls`.

## Retention (daily, first worker pass after midnight IST)

- Jobs older than 7 days: description + verdict deleted, status `expired`, removed from the queue. While the posting is still live, a tiny row (id/url/title) stays. It's the cursor; without it the job would look new next hour and be re-judged and re-billed.
- Jobs not seen on their page for 7 days: row deleted.
- `applied` rows are kept (tiny) so a repost doesn't land in your queue again. Say if you'd rather purge these too.
- `VACUUM INTO /data/backups/jobhunt-YYYYMMDD.db`, last 7 kept.

## Box isolation (Jeevrit-safe)

- Own dir `~/jobhunt`, `docker compose -p jobhunt`, own network. No `ports:`, and no Jeevrit network, volume, image or env file.
- Limits: worker `mem_limit 1536m, cpus 1.0`; api `256m, 0.5`; tailscale `128m`. Jeevrit always keeps ≥ 1 core.
- Log rotation `json-file max-size 10m × 3`, so logs can't fill the shared disk.
- **Never** on this box: `docker system|image|volume prune` (ballast runs an untagged image), `docker compose down` without `-p jobhunt`, iptables edits, anything under `~/Jeevrit`.
- Crawl politely (1 req/host at a time, back off on 429). The box's IP is shared with Jeevrit, so it must not get blocklisted.
- The deploy script snapshots Jeevrit's `docker ps` before and after and aborts if anything changed.
- The ballast container needs no change; our extra RAM only helps with Oracle's idle-reclaim check.

## Phases

### Phase 1: Foundation & import (local)
- Repo: `worker/jobhunt/` (Python package), `web/` (Next.js later), `schema.sql`, `git init`.
- SQLite schema + tiny db helper. Default settings.
- Importer for CSV/XLSX (`company_name`, `city`, `careers_url`; other columns ignored): one source per unique URL, companies sharing a URL grouped, upsert so re-import is safe.
- **Done when:** `python -m jobhunt.importer <csv>` loads 412 sources, and running it twice changes nothing.

### Phase 2: Classifier + ATS adapters
- Classifier: fetch each source once, follow redirects, fingerprint the ATS, extract tenant/slug → `ats`, `ats_key`. Re-run automatically when a source breaks.
- Adapters in order: Workday → Greenhouse, Lever, SmartRecruiters, Ashby, Workable → Eightfold, Oracle HCM → whichever Indian ATS (Darwinbox/Keka/Zoho) expose JSON.
- Location filter + date parsing per adapter.
- **Done when:** `python -m jobhunt.crawl --source <id>` prints normalized, dated, India-filtered jobs for at least one real company per adapter, and a coverage report shows sources per bucket.
- **Status (2026-10-24): done.** 17 adapters: workday, greenhouse, lever, ashby, smartrecruiters, workable, eightfold (v2 + PCSX), oraclehcm, phenom, keka, darwinbox, kula, zoho, freshteam, pyjamahr, turbohire, trakstar. Classified: **201 sources on a known board**, 207 generic (→ Phase 3), 4 dead links. `crawl --all`: ~7,200 India jobs, ~1,700 posted ≤ 3 days. Darwinbox needs a real browser (Cloudflare blocks plain HTTP); Zoho, PyjamaHR and Trakstar don't publish posting dates (first-seen stands in). Not adapted (they go generic): SuccessFactors, iCIMS, TalentBrew, Avature, Taleo, JazzHR, Zebra's Cloudflare-fronted Workday.

### Phase 3: Generic pages, Playwright, cursor & scheduler
- Generic extractor (JSON-LD → link set → LLM extraction on hash change), Playwright for JS/blocked pages.
- Scheduler loop, per-host limits, backoff, `broken` state, bootstrap baseline, diff → insert.
- **Done when:** a full local cycle over all sources completes; the second cycle right after finds ~0 new jobs (proves the cursor); report shows ok / blocked / zero-jobs counts.
- **Status (2026-10-24): done.** `python -m jobhunt.worker` (forever) / `--once [--all]`. A full cycle takes ~8 min locally; a back-to-back repeat found **0 new jobs** across 404 working sources. DB: 8,054 jobs = 6,667 baseline + 1,387 new (posted ≤ 3 days, waiting for Phase 4). Board sources: 186/201 currently list India jobs. Generic: 64/207 yield jobs with the no-key link heuristic, 139 return 0 → **needs `LLM_API_KEY`** for AI extraction. Extractor switch (heuristic → LLM) re-baselines instead of flooding the judge. Aggregator links (LinkedIn, Naukri, …) are never followed. 8 sources fail: 4 dead/expired-cert links, McKinsey + ITC Infotech bot-block, MakeMyTrip times out, Kinara TLS error.

### Phase 4: Resume, verdicts, guardrails
- Resume ingest + profile, gates 1–5, Verdict call, queueing, `llm_calls` logging, nightly purge + backup.
- Tuning pass: run ~30 real jobs, you mark each verdict right/wrong, adjust rubric/threshold/keywords.
- **Done when:** new jobs flow to `matched` with reasons; jobs > 3 days never hit the LLM (asserted); purge keeps the cursor rows; the self-check script passes.
- **Status (2026-10-24): done.**
  - **Backlog:** 1,387 jobs judged in 40 s. With the first settings, free gates took 1,196 (title 734, location 444, age 18), 28 were duplicates, 163 went to Jev, and 4 matched at 70%.
  - **Tuning (your labels):** you'd apply to every near-miss at fit 31–47. The changes:
    - cutoff 70 → **35**, plus the skills/role rule;
    - city filter dropped (India-wide);
    - "staff" no longer excluded;
    - "X Jobs" category links dropped.
  - **Result:** queue = **12 jobs**, plus 2 EPAM category pages marked skipped.
  - **Company pages via Jev:** pages yielding jobs rose from 64 to **115/207**. Overall **301/412 sources = 393/532 companies (74%)**.
  - **Daily purge + backup:** verified.
  - **Remaining zero-job company pages (89):** mostly no openings visible, JS-loaded lists, or email-only. SuccessFactors/iCIMS adapters would add ~14 more.
  - **Self-checks:** 16 pass.
  - **Cost:** $0.126 so far (2.99M input tokens over 913 calls). Jev's usage dashboard lags. At 13:00 UTC it showed $0.0268 / 235 requests, which matches the first 235 calls in `llm_calls` exactly.

### Phase 5 + 6: Laptop app (queue page included)
- **Status (2026-10-24): done and running.**
- **Install / remove:** `powershell -ExecutionPolicy Bypass -File worker/install-windows.ps1 [-Uninstall]`. It sets up:
  - a Task Scheduler task "Job Hunt", run at login and relaunched every 10 min (ignored while running), so a crash heals itself;
  - no time limit, runs on battery, below-normal priority;
  - "Job Hunt" shortcuts (see Phase 7).
- **Run by hand instead:** `cd worker && .venv\Scripts\python -m jobhunt.app`. A second copy refuses to start (port 8000 is taken).
- **Queue page** at `http://127.0.0.1:8000`:
  - **Tabs:** Queue / Opened / Applied / Skipped, with Open ↗ (marks opened), Applied, Skip, Undo, and keys `j k o a s u`.
  - **Sources:** health per page, filter, check now, pause.
  - **Settings:** resume upload, CSV/XLSX import, cutoff, age window, retention, interval, keyword lists.
  - **Status line:** coverage, last check, new postings / matches in 24 h, failing pages, 30-day spend.
  - **Warnings:** no key, no resume, nothing checked for 3 h.
- **Safety:**
  - Bound to 127.0.0.1.
  - Rejects unknown `Host` headers (DNS rebinding).
  - POSTs need an `X-Jobhunt` header, so other websites can't drive it.
  - Scraped titles and links are escaped, and only http(s) links are opened.
- **Laptop-specific:** a pass is skipped while offline (after sleep), instead of failing every source and backing off 2 h. Log: `~/.jobhunt/jobhunt.log`, rolled over at 5 MB.
- **Self-checks:** 17 pass (`test_app.py` covers the API guards, actions, settings validation and import).
- **Verified live:**
  - skip → restore and open → undo from the page;
  - settings save;
  - killing the process: back within 10 min;
  - stopping the task: both processes gone.

### Phase 7: Two more platforms, notifications, one-click start (2026-10-24, done)
- **SuccessFactors adapter** (`ats.successfactors`):
  - newer sites use the JSON search API, read newest first until a whole page is older than the age window;
  - older sites use the HTML results (table rows or tiles), 25 per page.
  - Detected by `rmkcdn` / `jobs2web` on the page.
  - Switched 13 sources, e.g. Wipro 370 recent India jobs, Birlasoft 209, Ingersoll Rand 75, Biocon 60, Firstsource 49, SAP 44 (several were 0 or capped at 25 before).
- **iCIMS adapter** (`ats.icims`, the "careers-home" search):
  - Plain HTTP where it works. Sites that give plain clients an empty list (AMD, GitHub, Paychex, ZS) are read from inside a real browser page.
  - Newest first where the site allows it.
  - Detected by `app.jibecdn.com`.
  - Switched 6 sources: Schneider Electric 589, AMD 210, ZS 52, Aon 31.
- **India detection** now accepts country codes:
  - accepted: `Hoskote, IN, 562114`, `Nashik, MH, IN`, `…, IND`;
  - a bare `Indianapolis, IN` still doesn't count.
- **Notifications:**
  - the watcher compares the queue before and after each pass, and new matches raise a Windows toast ("Job Hunt"); clicking it opens the queue;
  - shown via PowerShell's built-in toast API, text passed through an env var (no injection);
  - the AUMID is registered by the installer.
- **Shortcuts** "Job Hunt" (desktop + Start menu) run `pythonw -m jobhunt.app --open`:
  - if the app isn't answering, start the login task and wait for it; else open the page;
  - with no task installed, the shortcut's own process becomes the app.
  - Tested: stopped service → shortcut → running in 5 s, no second copy.

### Phase 8: Audit of every page + expansion (2026-10-24, done)
- **What reaches Jev:**
  - **Problem:** Oracle HCM jobs were judged on a median **39-char** summary (34/51 under 300 chars); Phenom on ~400-char teasers; JS-only company pages on near-empty text.
  - **Fix:** `describe()` now fetches the real JD (Oracle HCM details API, Phenom page data, JSON-LD, browser render).
  - **Re-judge:** 70 jobs. E.g. Zensar Python Developer 45% → 5% (asks 6+ yrs Python, 2+ GenAI), Honeywell Software Engr II 38% → 2%.
- **Category pages:** EPAM's "Software Developer Jobs" (a category page) passed both Jev steps. Added the `posting` question; it was backfilled on all matched jobs.
- **Experience rule (your call):** up to 3 years OK, 4+ blocked. 333 jobs from the last 7 days re-judged ($0.04). Queue: 9 → 24, every one under 4 years.
- **Company-built pages with jobs (174), judged title by title.** Fixed:
  - titles taken from dates, cities or "Full Time" next to a bare "Apply" now come from the job URL's slug;
  - "share on X" links are no longer jobs;
  - `{{record.Posting_Title}}` template text is dropped;
  - Zoho Recruit on the company's own domain is detected (HealthPlix, Yellow.ai, Zoho).
- **Pages listing 0 jobs (153):**
  - 24 are boards with no India jobs right now (fine).
  - Of the 129 company pages, an automated sweep found and verified a better route for **19**: Databricks, Datadog, Crunchyroll (Greenhouse); Saviynt (Lever); OakNorth, Redis (Ashby); Lam Research, Autodesk, TriNet, HP, Amdocs (Eightfold on their own domains, now auto-detected); Publicis (iCIMS); Providence (SuccessFactors); Vymo (Darwinbox); Postman (Workday); Tiger Analytics, Livspace, Chai Point, Aereo (company pages).
  - Most of the rest have no openings, are pharma/hospital/brand pages, or are bot-blocked.
- **Expansion:**
  - 199 companies from your `new_careers_found` CSV (183/190 pages work).
  - 110 watchlist companies that had a seed URL (Adobe, Accenture, Cisco, Flipkart, Freshworks…).
  - **257 startups** whose careers page was found automatically: directory website → careers link → verified.
  - **Not found:** 401 startups. 232 have no careers page, 103 have no website listed, 66 sites were down or blocking.

### Oracle move (later, optional)
- **Hosting check (2026-10-24, measured):** Oracle vs your laptop, same requests to every crawl target.
  - **Box headroom:** 7.8 GB RAM available, 32 GB disk free, load 0.07.
  - **Workday API:** works from both for all 71 tenants.
  - **Blocked from Oracle only:** 15 of 207 company-built pages block Oracle's datacenter IP but work from home (Cognizant, Nutanix, Dropbox, Pinterest, Coupa, Swiss Re, Gartner, ADP, UST, Confluent, KFin, Innominds, YourStory, Arista, Synaptics). That's ~4% of sources.
  - **Recommendation:** Oracle, for 24/7 fetching.
  - **Decision: laptop first** (your call; the laptop is open most of the time). Oracle stays the later upgrade.
  - **Laptop footprint (measured):**
    - **Disk:** ~350 MB: venv 134 MB, headless Chromium 203 MB (the full 343 MB Chromium is never launched, so it can go), DB + 7 backups ≤ ~100 MB with retention. 39 GB free.
    - **RAM:** idle 80–140 MB. Full all-sources cycle (476 s): median 605 MB, p95 1.17 GB, peak 1.48 GB (2 browsers max). Hourly due-sources cycle (95 s): median 95 MB, peak 0.9 GB.
    - **CPU:** 0.6 of a core on average during a full cycle (Ryzen 5 5600H, 12 threads).
    - **Network:** ≤ 426 MB per full cycle, measured laptop-wide (includes Dropbox re-uploading the DB).
    - **After the laptop setup** (images/fonts/video blocked, DB out of Dropbox, full Chromium + unused packages removed):
      - full cycle 518 s;
      - RAM median 492 MB, p95 1.05 GB, peak 1.25 GB;
      - CPU 0.6 core;
      - network **280 MB**, laptop-wide;
      - idle app 53 MB;
      - disk: venv 124 MB + headless Chromium 203 MB + data.
    - **Daily network:** roughly 2–5 GB/day at the 60-min interval. Fine on home Wi-Fi, heavy on a phone hotspot. `interval_min` = 120 halves it.
- **Getting the 15 blocked pages working on Oracle later:**
  1. **Crawl the job platform's API instead of the page (free).** Done for Pinterest and Dropbox (Greenhouse; both have 0 India jobs, so the 40 the scraper took were foreign) and Gartner (Workday, works from Oracle). Swiss Re = SuccessFactors (loads from Oracle; needs that adapter). Synaptics = Jobvite, Coupa = Gem, ADP/UST maybe Workday: not yet confirmed.
  2. **Route only the leftover pages through the laptop's home connection** over Tailscale, when it's on.
  3. **Paid residential proxy:** not worth it for ~10 small pages.
- **The move itself:**
  - The same `jobhunt.app` goes in a container (Playwright arm64 base) as compose project `jobhunt`, with `JOBHUNT_DB=/data/jobhunt.db`, limits and log rotation.
  - The page is reached through Tailscale (no Vercel needed, since the app serves its own page).
  - `deploy.sh` checks Jeevrit is unchanged before/after.

## Later (skipped on purpose)
- HR mass email (your future scope).
- Careers-URL discovery for the ~970 companies without one (LLM web search, one-time per company).
- Adapters for the unconfirmed platforms (Synaptics/Jobvite, Coupa/Gem) and legacy SuccessFactors portals (Swiss Re works through the generic reader already).

## Optional from you
- **Phone access:** install Tailscale on the laptop and phone (same account), then run `tailscale serve --bg 8000` once on the laptop.
- **Jev credit:** expires 2026-11-19. Before then, set `DECISION_PROVIDER=openai` in `worker/.env` or top up.

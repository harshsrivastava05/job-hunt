# Job Hunt

A personal job watcher. It checks a few hundred company career pages every hour, finds postings that weren't there before, asks a decision model whether each one fits my resume, and puts the good ones in a queue. I apply by hand. It runs on a Windows laptop as a background app with a small local web page.

## How it works

1. **Companies in.** A CSV/XLSX of `company_name, careers_url, city` is imported. Each unique careers URL becomes a *source*.
2. **Find the job board.** The first check works out what each page runs on, and reads it the cheapest way:
   - **19 known platforms** have adapters that read their public JSON or HTML: Workday, Greenhouse, Lever, Ashby, SmartRecruiters, Workable, SuccessFactors, iCIMS, Eightfold, Oracle HCM, Phenom, Darwinbox, Keka, Zoho Recruit, Freshteam, Kula, PyjamaHR, TurboHire and Trakstar. Amazon and Capgemini have their own (their careers pages only show the first page of results).
   - **Anything else** goes to a generic reader: JSON-LD first, else the decision model picks out which links on the page are job openings. A page that shows no jobs as plain HTML is rendered once in headless Chromium, and kept that way if its jobs only appear after scripts run.
   - **India only.** Every source is filtered to jobs in India.
3. **Cursor.** Every job a source has listed is remembered, so a job is processed once. The first check of a source is a baseline: jobs already open aren't judged unless they were posted in the last 2 days. The first time a page lists anything also counts as a baseline, since an empty first check is often a page that didn't render. An unchanged page costs nothing.
4. **Free filters, then the model.** New jobs are dropped for free if older than 2 days (the list's date, else a date on the job's own page, else when it was first seen), if the title doesn't match the include/exclude keywords, or if it's a repost. The rest go to [TypeSafe Jev](https://typesafe.ai) (or OpenAI's Decisions API) with the resume. It returns six yes/no probabilities: `fit`, `skills`, `experience` (open to under 4 years?), `role` (hands-on software role), `india` and `posting` (one real job, not a category page).
5. **Queue.** A job is queued when it's a single software posting in India that asks for under 4 years of experience, and either fit ≥ the cutoff (35% by default) or the skills match is ≥ 80%. The full job description is always fetched first: summaries and teasers from job lists don't count. A Windows notification goes out for new matches.
6. **Housekeeping.** Once a day the app removes jobs older than 7 days and keeps the last 7 daily backups.

## Setup (Windows)

```powershell
cd worker
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install --only-shell chromium
copy .env.example .env        # then put JEV_API_KEY (or OPENAI_API_KEY) in it
.venv\Scripts\python -m jobhunt.importer path\to\companies.csv
.venv\Scripts\python -m jobhunt.resume path\to\resume.pdf
powershell -ExecutionPolicy Bypass -File install-windows.ps1
```

The installer does three things:
- registers a Task Scheduler task that starts the app at login (hidden, low priority, relaunched within 10 minutes if it dies);
- adds a **Job Hunt** shortcut to the desktop and Start menu;
- starts the app.

To remove it, run `install-windows.ps1 -Uninstall`.

## Using it

- **Queue page:** open the **Job Hunt** shortcut, or `http://127.0.0.1:8000`. The shortcut starts the app first if it isn't running.
  - **Tabs:** Queue / Opened / Applied / Skipped, with keys `j k o a s u`.
  - **Sources:** health of every career page, check now, pause.
  - **Settings:** resume upload, CSV/XLSX import, cutoff, keyword lists, intervals.
- **Data:** everything lives in `~/.jobhunt`: `jobhunt.db` (SQLite), `backups/` and `jobhunt.log`. It's kept outside the project folder on purpose.
- **Command line** (from `worker/`):
  - `python -m jobhunt.worker --once [--all]`: one pass now, with a report. Stop the task first, so two watchers don't overlap.
  - `python -m jobhunt.crawl --source ID`: what one source lists right now.
  - `python -m jobhunt.judge --sample 30`: a spread of verdicts.
- **Self-checks:** `python test_ats.py`, `test_worker.py`, `test_judge.py`, `test_importer.py` and `test_app.py`. They run offline.

The page is bound to 127.0.0.1. It also refuses unknown `Host` headers and cross-site POSTs. For phone access, use Tailscale (`tailscale serve --bg 8000`) rather than opening a port.

## Repo layout

```
PLAN.md                    design notes, measurements and phase-by-phase status
worker/install-windows.ps1 login task + shortcuts
worker/jobhunt/
  app.py        local web server + background watcher (the thing that runs)
  worker.py     scheduler loop, cursor, offline/sleep handling, notifications
  ats.py        job-board detection + one adapter per platform
  generic.py    company-built pages: JSON-LD, decision-model link extraction, headless browser
  classify.py   works out which board a source uses
  judge.py      free gates, decision-model verdicts, queue rule, daily purge + backup
  decide.py     TypeSafe Jev / OpenAI Decisions API client
  notify.py     Windows notifications
  importer.py   CSV/XLSX import;  resume.py  resume upload;  schema.sql  the database
  index.html    the queue page (vanilla JS, no build step)
```

"""Write the watched career pages and the matched jobs to an Excel file to share.

Usage: python -m jobhunt.export <out.xlsx> [days]

Public links only: no resume, application status or settings. Sheets:
- "Company career pages": every watched company, biggest cities first.
- "Job boards & remote": job boards and contract platforms.
- "Matched jobs": jobs the judge matched in the last <days> days (default 21, the app's retention), newest first.
"""
import sqlite3
import sys
from collections import Counter

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from jobhunt.db import DB_PATH

NAMES = {"generic": "Company website", "generic_js": "Company website", None: "Company website", "workday": "Workday",
         "keka": "Keka", "greenhouse": "Greenhouse", "darwinbox": "Darwinbox", "zoho": "Zoho Recruit",
         "successfactors": "SAP SuccessFactors", "smartrecruiters": "SmartRecruiters", "oraclehcm": "Oracle HCM",
         "phenom": "Phenom", "ashby": "Ashby", "lever": "Lever", "freshteam": "Freshteam", "eightfold": "Eightfold",
         "icims": "iCIMS", "pyjamahr": "PyjamaHR", "turbohire": "TurboHire", "workable": "Workable"}
BOARD_ATS = {"instahyre", "jobcode", "mercor", "micro1", "remoterocketship", "wellfound", "weworkremotely", "ycombinator"}
BOARD_NAMES = {"javascript.jobs", "Meridial (Invisible)", "Turing", "Handshake", "AnyoneAI", "Mindrift (Toloka)"}
REMOTE = ("remote", "work from home", "wfh", "anywhere")  # same idea as the page's Remote / WFH tab


def platform(ats):
    return NAMES.get(ats, (ats or "").title())


def link(url):
    return url[:-4] if url.endswith(".rss") else url  # We Work Remotely feeds: give the human page


def sheet(wb, title, header, rows, widths):
    ws = wb.create_sheet(title)
    ws.append(header)
    for cell in ws[1]:
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F4E78")
    col = header.index("Link") + 1
    for r in rows:
        ws.append(r)
        cell = ws.cell(ws.max_row, col)
        cell.hyperlink, cell.style = cell.value, "Hyperlink"
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(65 + i)].width = w
    ws.freeze_panes, ws.auto_filter.ref = "A2", ws.dimensions
    return len(rows)


def export(conn, out, days=21):
    rows = conn.execute("SELECT c.name, c.city, s.url, s.ats FROM companies c JOIN sources s ON s.id = c.source_id").fetchall()
    is_board = lambda r: r[3] in BOARD_ATS or r[0] in BOARD_NAMES  # noqa: E731
    size = Counter(r[1] for r in rows if not is_board(r))
    companies = sorted((r for r in rows if not is_board(r)), key=lambda r: (-size[r[1]], r[1], r[0].lower()))
    boards = sorted((r for r in rows if is_board(r)), key=lambda r: (r[0].lower(), r[1]))
    jobs = conn.execute(
        "SELECT j.title, (SELECT group_concat(DISTINCT name) FROM companies WHERE source_id = j.source_id), s.ats,"
        " j.location, substr(coalesce(j.posted_at, j.first_seen_at), 1, 10) AS day, j.url"
        " FROM jobs j JOIN sources s ON s.id = j.source_id"
        " WHERE j.status = 'matched' AND j.first_seen_at >= datetime('now', ?) ORDER BY day DESC, j.score DESC",
        (f"-{days} days",),
    ).fetchall()

    def job_row(title, company, ats, location, day, url):
        if ats in BOARD_ATS and " at " in title:  # boards list "<role> at <company>": show the employer, not the board
            title, company = title.rsplit(" at ", 1)
        remote = "Yes" if any(w in f"{location} {title}".lower() for w in REMOTE) else ""
        return title, (company or "").split(",")[0], location or "", remote, day, url

    wb = Workbook()
    wb.remove(wb.active)
    counts = (
        sheet(wb, "Company career pages", ["Company", "City", "Link", "Platform"],
              [(n, city or "India", link(u), platform(a)) for n, city, u, a in companies], (42, 16, 80, 20)),
        sheet(wb, "Job boards & remote", ["Site", "Focus", "Link", "Platform"],
              [(n, city or "India", link(u), platform(a)) for n, city, u, a in boards], (28, 30, 80, 20)),
        sheet(wb, "Matched jobs", ["Job", "Company", "Location", "Remote", "Posted", "Link"],
              [job_row(*j) for j in jobs], (55, 30, 30, 9, 12, 80)),
    )
    wb.save(out)
    return counts


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    print("companies %d, boards %d, matched jobs %d" % export(conn, sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 21))

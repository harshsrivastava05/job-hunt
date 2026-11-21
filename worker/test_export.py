"""Run: python test_export.py (or pytest)"""
import tempfile
from pathlib import Path

from openpyxl import load_workbook

from jobhunt.db import connect
from jobhunt.export import export


def test_export_sheets():
    conn = connect(":memory:")
    with conn:
        conn.execute("INSERT INTO sources (id, url, ats) VALUES (1, 'https://jobs.lever.co/acme', 'lever'),"
                     " (2, 'https://www.instahyre.com/search-jobs', 'instahyre')")
        conn.execute("INSERT INTO companies (name, city, source_id) VALUES ('Acme', 'Pune', 1), ('Instahyre', 'India', 2)")
        conn.execute("INSERT INTO jobs (source_id, external_id, url, title, location, first_seen_at, last_seen_at, status, apply_status) VALUES"
                     " (1, 'a', 'https://jobs.lever.co/acme/1', 'Backend Engineer', 'India (remote)', datetime('now'), datetime('now'), 'matched', 'applied'),"
                     " (2, 'b', 'https://www.instahyre.com/job-1', 'SDE at Foo Labs', 'Bangalore', datetime('now'), datetime('now'), 'matched', NULL),"
                     " (1, 'c', 'https://jobs.lever.co/acme/2', 'Sales Lead', 'Pune', datetime('now'), datetime('now'), 'rejected', NULL)")
    with tempfile.TemporaryDirectory() as d:
        out = Path(d, "x.xlsx")
        assert export(conn, out) == (1, 1, 2)  # the rejected job stays out
        wb = load_workbook(out)
        jobs = {r[0]: r for r in wb["Matched jobs"].iter_rows(min_row=2, values_only=True)}
        assert jobs["SDE"][1] == "Foo Labs"  # a board's "<role> at <company>" shows the employer
        assert jobs["Backend Engineer"][3] == "Yes" and jobs["SDE"][3] is None
        assert "applied" not in str(list(wb["Matched jobs"].values))  # never his application status
        assert wb["Company career pages"]["A2"].value == "Acme" and wb["Job boards & remote"]["A2"].value == "Instahyre"


if __name__ == "__main__":
    test_export_sheets()
    print("ok test_export_sheets")

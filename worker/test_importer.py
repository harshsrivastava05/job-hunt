"""Run: python test_importer.py (or pytest)"""
import tempfile
from pathlib import Path

from openpyxl import Workbook

from jobhunt.db import connect
from jobhunt.importer import import_file, normalize_url

HEADER = "company_name,city,careers_url\n"


def test_normalize_url():
    assert normalize_url(" HTTPS://Careers.ABB/global/en/ ") == "https://careers.abb/global/en"
    assert normalize_url("www.x.com/careers") == "https://www.x.com/careers"
    assert normalize_url("https://x.com/jobs?for=acme#/open") == "https://x.com/jobs?for=acme#/open"
    for bad in ("", "not a url", "ftp://x.com/jobs", "n/a"):
        assert normalize_url(bad) is None, bad


def test_import_groups_and_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d, "c.csv")
        f.write_text(
            HEADER
            + "ABB,Bengaluru,https://careers.abb/global/en\n"
            + "ABB India,Bengaluru,https://careers.abb/global/en/\n"  # same source, trailing slash
            + "Akamai,Hyderabad,\n",  # no URL
            encoding="utf-8-sig",
        )
        conn = connect(":memory:")
        first = import_file(conn, f)
        assert (first["new_sources"], first["new_companies"], first["skipped"]) == (1, 2, 1), first
        again = import_file(conn, f)
        assert (again["new_sources"], again["new_companies"], again["moved"], again["unchanged"]) == (0, 0, 0, 2), again

        f.write_text(HEADER + "ABB,Bengaluru,https://abb.wd3.myworkdayjobs.com/External\n", encoding="utf-8-sig")
        moved = import_file(conn, f)
        assert (moved["new_sources"], moved["moved"]) == (1, 1), moved
        assert conn.execute("SELECT count(*) FROM companies").fetchone()[0] == 2


def test_import_xlsx():
    with tempfile.TemporaryDirectory() as d:
        wb = Workbook()
        wb.active.append(["Company_Name", "City", "Careers_URL"])  # header case shouldn't matter
        wb.active.append(["Acceldata", "Bengaluru", "https://www.acceldata.io/careers"])
        wb.active.append(["Blank", None, None])
        wb.save(Path(d, "c.xlsx"))
        stats = import_file(connect(":memory:"), Path(d, "c.xlsx"))
        assert (stats["new_companies"], stats["skipped"]) == (1, 1), stats


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)

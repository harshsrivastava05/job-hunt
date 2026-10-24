"""Make a resume the one jobs are judged against. Recent jobs are re-judged against it.

Usage: python -m jobhunt.resume <resume.pdf | resume.txt>
"""
import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jobhunt.db import connect, now


def add(conn, filename, data: bytes):
    """Store it, make it active, and send recent judged-but-not-acted-on jobs back to 'new'. -> (resume id, re-queued)"""
    is_pdf = filename.lower().endswith(".pdf")
    if is_pdf:
        from pypdf import PdfReader

        text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
    else:
        text = data.decode("utf-8", "replace")
    text = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
    if len(text.split()) < 100:
        raise ValueError(f"only {len(text.split())} words of text in {filename}: a scanned PDF? export it with selectable text")
    max_age = int(conn.execute("SELECT value FROM settings WHERE key = 'max_age_days'").fetchone()[0])
    with conn:
        conn.execute("UPDATE resumes SET active = 0")
        rid = conn.execute("INSERT INTO resumes (filename, pdf, text) VALUES (?, ?, ?)", (filename, data if is_pdf else None, text)).lastrowid
        requeued = conn.execute(
            "UPDATE jobs SET status = 'new', score = NULL, verdict = NULL, apply_status = NULL"
            " WHERE status IN ('matched', 'rejected', 'duplicate', 'error') AND apply_status IS NOT 'applied'"
            " AND apply_status IS NOT 'skipped' AND first_seen_at >= ?",
            (now(datetime.now(timezone.utc) - timedelta(days=max_age)),),
        ).rowcount
    return rid, requeued


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    path = Path(sys.argv[1])
    rid, requeued = add(connect(), path.name, path.read_bytes())
    print(f"resume #{rid} active ({path.name}); {requeued} recent jobs will be re-judged")

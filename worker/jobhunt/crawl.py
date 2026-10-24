"""Run sources' adapters and show what they return.

Usage: python -m jobhunt.crawl --source ID | --ats NAME | --all   [--days 7]
"""
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from jobhunt import ats, generic
from jobhunt.db import connect


def fetch(source, since):
    if source["ats"] in ("generic", "generic_js"):
        return generic.crawl(source["ats_key"], source["ats"] == "generic_js")[0]
    with ats.client() as c:
        return ats.ADAPTERS[source["ats"]](c, source["ats_key"], since)


def main():
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 7
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    conn = connect()
    q = "SELECT s.*, (SELECT group_concat(name, ', ') FROM companies WHERE source_id = s.id) AS companies FROM sources s"
    if "--all" in sys.argv:
        rows = conn.execute(q + " WHERE ats IN (%s)" % ",".join("?" * len(ats.ADAPTERS)), list(ats.ADAPTERS)).fetchall()
        report(rows, since, now)
        return
    if "--source" in sys.argv:
        row = conn.execute(q + " WHERE id = ?", (int(sys.argv[sys.argv.index("--source") + 1]),)).fetchone()
    else:
        row = conn.execute(q + " WHERE ats = ? LIMIT 1", (sys.argv[sys.argv.index("--ats") + 1],)).fetchone()
    if not row or row["ats"] not in (*ats.ADAPTERS, "generic", "generic_js"):
        sys.exit(f"no adapter for that source ({row and row['ats']})")
    jobs = sorted(fetch(row, since), key=lambda j: j.posted_at or now, reverse=True)
    print(f"[{row['id']}] {row['companies']}: {row['ats']} {row['ats_key']} -> {len(jobs)} India jobs")
    for j in jobs:
        age = f"{(now - j.posted_at).days}d" if j.posted_at else "?"
        print(f"  {age:>4}  {j.title[:60]:60}  {j.location[:35]:35}  {j.url}")


def report(rows, since, now):
    def run(row):
        try:
            return row, fetch(row, since), None
        except Exception as e:
            return row, [], f"{type(e).__name__}: {str(e)[:100]}"

    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(run, rows))
    stats = defaultdict(lambda: [0, 0, 0, 0, 0])  # sources, failed, jobs, dated, posted <= 3 days
    for row, jobs, err in results:
        s = stats[row["ats"]]
        s[0] += 1
        s[1] += bool(err)
        s[2] += len(jobs)
        s[3] += sum(j.posted_at is not None for j in jobs)
        s[4] += sum(j.posted_at is not None and now - j.posted_at <= timedelta(days=3) for j in jobs)
    print(f"{'ats':16}{'sources':>8}{'failed':>8}{'jobs':>7}{'dated':>7}{'<=3d':>7}")
    for name, (n, failed, jobs, dated, fresh) in sorted(stats.items(), key=lambda kv: -kv[1][0]):
        print(f"{name:16}{n:8}{failed:8}{jobs:7}{dated:7}{fresh:7}")
    for row, _, err in results:
        if err:
            print(f"  FAILED [{row['id']}] {row['companies']} {row['ats']} {row['ats_key']}: {err}")


if __name__ == "__main__":
    main()

"""The watcher: crawl every due source, store jobs it hasn't seen before, repeat.

Usage: python -m jobhunt.worker                 # forever; looks for due sources every minute
       python -m jobhunt.worker --once          # one pass over what's due, then a report
       python -m jobhunt.worker --once --all    # treat every source as due (cursor check)

Cursor per source: (source_id, external_id) rows are the seen-set, list_hash skips unchanged lists,
bootstrapped_at makes the first crawl a baseline (only jobs posted inside max_age_days count as new).
"""
import hashlib
import json
import random
import sys
import threading
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

from jobhunt import ats, generic, judge, notify
from jobhunt.classify import classify
from jobhunt.db import connect, now

GENERIC = ("generic", "generic_js")
_LIMITS = defaultdict(lambda: threading.Semaphore(3))  # per board API or site: one pass can't hammer a host


def run(src, since):
    """Network half of one source, in a worker thread. Returns what save() needs; never raises."""
    s, out = dict(src), {}
    try:
        if not s["ats"]:  # new import, or a link that failed before
            name, key, _, note = classify(s["url"])
            if not name:
                raise RuntimeError(note or "unreachable")
            s["ats"], s["ats_key"] = out["ats"], out["ats_key"] = name, key
        with _LIMITS[urlsplit(s["ats_key"]).netloc if s["ats"] in GENERIC else s["ats"]]:
            if s["ats"] in GENERIC:
                jobs, out["hash"], updates = generic.crawl(s["ats_key"], s["ats"] == "generic_js", s["list_hash"])
                out.update(updates)
            else:
                with ats.client() as c:
                    jobs = ats.ADAPTERS[s["ats"]](c, s["ats_key"], since)
                out["hash"] = "api:" + hashlib.sha1("\n".join(sorted(j.external_id for j in jobs)).encode()).hexdigest()
        out["jobs"] = jobs
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}"
    return out


def save(conn, src, out, ts, cfg):
    """DB half (main thread). Returns ('failed'|'unchanged'|'changed', new_jobs)."""
    sid, utc = src["id"], datetime.now(timezone.utc)
    if "error" in out:
        fails = src["fail_count"] + 1
        delay = min(cfg["interval_min"] * 2**fails, 24 * 60)  # 2h, 4h, ... 24h; fail_count >= 5 shows as broken
        conn.execute(
            "UPDATE sources SET fail_count = ?, last_error = ?, next_check_at = ? WHERE id = ?",
            (fails, out["error"], now(utc + timedelta(minutes=delay)), sid),
        )
        return "failed", 0
    if "ats" in out:
        conn.execute("UPDATE sources SET ats = ? WHERE id = ?", (out["ats"], sid))
    if "ats_key" in out:
        conn.execute("UPDATE sources SET ats_key = ? WHERE id = ?", (out["ats_key"], sid))

    jobs, result, new = out["jobs"], "unchanged", 0
    if jobs is None or out["hash"] == src["list_hash"]:
        # same list as last time: everything listed then is still listed now
        conn.execute("UPDATE jobs SET last_seen_at = ? WHERE source_id = ? AND last_seen_at = ?", (ts, sid, src["last_ok_at"]))
    else:
        result = "changed"
        seen = {r[0] for r in conn.execute("SELECT external_id FROM jobs WHERE source_id = ?", (sid,))}
        fresh_after = utc - timedelta(days=cfg["max_age_days"])
        # first crawl, or a different extractor (heuristic -> decide, a board adapter replacing generic): baseline again.
        # Also the first time a page lists anything: an empty first crawl is often a page that didn't render, and when it
        # does, everything on it would look new (Translab: "Posted 2 years ago"). Undated first jobs of a new board are missed.
        mode = lambda h: (h or "").split(":")[0]  # noqa: E731
        baseline = src["bootstrapped_at"] is None or mode(src["list_hash"]) != mode(out["hash"]) or not seen
        rows = {}
        for j in jobs:
            if j.external_id in seen or j.external_id in rows:
                continue
            is_new = not baseline or (j.posted_at is not None and j.posted_at >= fresh_after)
            rows[j.external_id] = (
                sid, j.external_id, j.url, j.title[:300], j.location[:300], j.posted_at and now(j.posted_at),
                ts, ts, (j.description or None) if is_new else None, "new" if is_new else "baseline",
            )
        conn.executemany(
            "INSERT INTO jobs (source_id, external_id, url, title, location, posted_at, first_seen_at, last_seen_at,"
            " description, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows.values(),
        )
        conn.executemany(
            "UPDATE jobs SET last_seen_at = ? WHERE source_id = ? AND external_id = ?",
            [(ts, sid, e) for e in {j.external_id for j in jobs} & seen],
        )
        new = sum(r[-1] == "new" for r in rows.values())

    minutes = (src["interval_min"] or cfg["interval_min"]) * (2 if out.get("ats", src["ats"]) == "generic_js" else 1)
    conn.execute(
        "UPDATE sources SET list_hash = ?, last_ok_at = ?, fail_count = 0, last_error = NULL,"
        " bootstrapped_at = coalesce(bootstrapped_at, ?), next_check_at = ? WHERE id = ?",
        (out["hash"], ts, ts, now(utc + timedelta(minutes=minutes * random.uniform(0.9, 1.1))), sid),
    )
    return result, new


def online():
    try:
        httpx.head("https://www.gstatic.com/generate_204", timeout=10)
        return True
    except httpx.HTTPError:
        return False


def tick(conn, everything=False, verbose=False):
    """Crawl all due sources once. Returns a Counter of outcomes."""
    ts = now()
    cfg = {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM settings")}
    q = "SELECT * FROM sources s WHERE active = 1 AND EXISTS (SELECT 1 FROM companies c WHERE c.source_id = s.id)"
    srcs = conn.execute(q + ("" if everything else " AND (next_check_at IS NULL OR next_check_at <= ?)") + " ORDER BY next_check_at",
                        () if everything else (ts,)).fetchall()
    since = datetime.now(timezone.utc) - timedelta(days=cfg["max_age_days"] + 1)  # boards sorted newest-first stop paging here
    stats = Counter()
    if srcs and not online():  # a laptop just out of sleep: don't fail (and back off) every source for want of Wi-Fi
        print(f"{ts} offline: {len(srcs)} sources wait for the next pass", flush=True)
        return stats
    with ThreadPoolExecutor(8) as ex:
        futures = {ex.submit(run, s, since): s for s in srcs}
        for f in as_completed(futures):
            src, out = futures[f], f.result()
            with conn:
                result, new = save(conn, src, out, ts, cfg)
            stats[result] += 1
            stats["new_jobs"] += new
            if verbose and (new or result == "failed"):
                print(f"  [{src['id']}] {out.get('ats', src['ats'])}: {result} +{new} {out.get('error', '')}", flush=True)
    return stats


def report(conn):
    q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731
    print("\njobs by status:", dict(q("SELECT status, count(*) FROM jobs GROUP BY status")))
    rows = q(
        "SELECT coalesce(ats, '-') a, count(*), sum(fail_count = 0 AND last_ok_at IS NOT NULL), sum(fail_count > 0),"
        " sum(last_ok_at IS NOT NULL AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.source_id = s.id AND j.last_seen_at = s.last_ok_at))"
        " FROM sources s GROUP BY a ORDER BY 2 DESC"
    )
    print(f"{'ats':14}{'sources':>8}{'ok':>6}{'failing':>8}{'0 jobs':>8}")
    for a, n, ok, failing, empty in rows:
        print(f"{a:14}{n:8}{ok or 0:6}{failing or 0:8}{empty or 0:8}")


def loop():
    """Forever: crawl what's due, judge what's new, notify about new matches, daily purge; idle a minute when there's nothing to do."""
    conn = connect()
    queued = lambda: {r[0] for r in conn.execute("SELECT id FROM jobs WHERE apply_status = 'queued'")}  # noqa: E731
    while True:
        try:
            before = queued()
            stats, judged = tick(conn), judge.judge_pending(conn)
            if stats or judged:
                print(f"{now()} checked {dict(stats)}, judged {judged}", flush=True)
            if fresh := queued() - before:
                notify.new_matches(conn, fresh)
            busy = sum(stats.values()) + judged
            judge.daily(conn)
        except Exception:  # one bad pass (a bug in an adapter, a locked DB) must not stop the watcher
            traceback.print_exc()
            busy = 0
        if not busy:
            time.sleep(60)


def main():
    if "--once" not in sys.argv:
        return loop()
    conn = connect()
    started = time.time()
    stats = tick(conn, everything="--all" in sys.argv, verbose=True)
    stats["judged"] = judge.judge_pending(conn, limit=5000, workers=8)
    print(f"\n{dict(stats)} in {time.time() - started:.0f}s")
    report(conn)


if __name__ == "__main__":
    main()

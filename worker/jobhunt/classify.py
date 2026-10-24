"""Work out which job board each source uses; store it as sources.ats / sources.ats_key.

Usage: python -m jobhunt.classify [--all | --source ID]   (default: sources not classified yet)

ats = an adapter name, 'generic' (fetchable, no known board), or NULL with last_error (blocked/dead).
The worker also calls classify() for newly imported sources, so this CLI is only for re-checking.
"""
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx

from jobhunt import ats, generic
from jobhunt.db import connect


def classify(url):
    """-> (ats, key, jobs_found, note)"""
    since = datetime.now(timezone.utc) - timedelta(days=7)
    notes, final, loaded = [], url, False
    with ats.client() as c:
        cands = []
        try:
            r = c.get(url)
            final = str(r.url)
            if r.status_code >= 400:
                notes.append(f"HTTP {r.status_code}")
            else:
                loaded = True
                cands = ats.detect(final, r.text)
                for link in [] if cands else generic.job_links(generic.parse(final, r.text)):
                    try:
                        r2 = c.get(link)
                        if cands := ats.detect(str(r2.url), r2.text):
                            break
                    except httpx.HTTPError:
                        pass
        except httpx.HTTPError as e:
            notes.append(type(e).__name__)
        if not cands:  # JS-only page, or blocked for plain HTTP: let a real browser load it
            try:
                with ats.browser_page(url, wait="load") as (pg, requested):
                    pg.wait_for_timeout(3000)
                    final, loaded = pg.url, True
                    cands = ats.detect(final, pg.content() + " " + " ".join(requested))
            except Exception as e:
                notes.append(f"browser: {type(e).__name__}: {str(e).splitlines()[0][:80]}")
        for name, key in cands:
            try:
                return name, key, len(ats.ADAPTERS[name](c, key, since)), ""
            except Exception as e:
                notes.append(f"{name}:{key} {type(e).__name__}: {str(e)[:80]}")
    if loaded:  # page works but no usable board: the generic extractor takes it
        return "generic", final, None, "; ".join(notes)
    return None, None, None, "; ".join(notes)


def main():
    conn = connect()
    if "--source" in sys.argv:
        rows = conn.execute("SELECT id, url FROM sources WHERE id = ?", (int(sys.argv[sys.argv.index("--source") + 1]),)).fetchall()
    elif "--all" in sys.argv:
        rows = conn.execute("SELECT id, url FROM sources").fetchall()
    else:
        rows = conn.execute("SELECT id, url FROM sources WHERE ats IS NULL").fetchall()
    print(f"classifying {len(rows)} sources…", flush=True)
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda r: (r["id"], r["url"], *classify(r["url"])), rows))
    with conn:
        for sid, _, name, key, _, note in results:
            conn.execute("UPDATE sources SET ats = ?, ats_key = ?, last_error = ? WHERE id = ?", (name, key, note or None, sid))
    by_ats = Counter(name or "FAILED" for _, _, name, *_ in results)
    print("\n".join(f"{n:4}  {a}" for a, n in by_ats.most_common()))
    for sid, url, name, key, n, note in results:
        if len(rows) <= 5 or name is None:
            print(f"  [{sid}] {url} -> {name} {key or ''} jobs={n} {note}")


if __name__ == "__main__":
    main()

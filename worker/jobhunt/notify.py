"""Windows notification when the watcher queues new matches; clicking it opens the queue page.

Shown through PowerShell's built-in access to Windows toasts, so no extra package. The text travels in an environment
variable, never inside the script, so a job title can't inject PowerShell. No-op on other systems.
"""
import os
import subprocess
from xml.sax.saxutils import escape

PAGE = f"http://127.0.0.1:{os.environ.get('JOBHUNT_PORT', '8000')}/"
AUMID = "JobHunt.Watcher"  # registered with the name "Job Hunt" by install-windows.ps1
_SHOW = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null;"
    "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml($env:JOBHUNT_TOAST);"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:JOBHUNT_AUMID).Show([Windows.UI.Notifications.ToastNotification]::new($x))"
)


def toast(title, body):
    if os.name != "nt":
        return
    xml = (f'<toast activationType="protocol" launch="{escape(PAGE)}"><visual><binding template="ToastGeneric">'
           f"<text>{escape(title)}</text><text>{escape(body)}</text></binding></visual></toast>")
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _SHOW],
        env={**os.environ, "JOBHUNT_TOAST": xml, "JOBHUNT_AUMID": AUMID},
        creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, text=True, timeout=60,
    )
    if r.returncode:
        print(f"notification failed: {r.stderr.strip()[:300]}", flush=True)


def new_matches(conn, ids):
    rows = conn.execute(
        "SELECT j.title, j.score, (SELECT group_concat(DISTINCT name) FROM companies WHERE source_id = j.source_id) AS company"
        f" FROM jobs j WHERE j.id IN ({','.join('?' * len(ids))}) ORDER BY j.score DESC", list(ids),
    ).fetchall()
    if len(rows) == 1:
        r = rows[0]
        toast(f"New match: {r['title']}", f"{(r['company'] or '').split(',')[0]} · fit {r['score']}%")
    elif rows:
        more = f"\n+{len(rows) - 3} more" if len(rows) > 3 else ""
        toast(f"{len(rows)} new job matches", "\n".join(f"{r['title']} ({(r['company'] or '').split(',')[0]}, {r['score']}%)" for r in rows[:3]) + more)

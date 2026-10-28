"""Data-volume history of the vehicle SIM: stores hourly readings, renders a printable month page.

stdlib only. Readings arrive from fuekw-watch via POST /api/reading (Bearer token).
"""
import calendar
import hmac
import html
import json
import os
import sqlite3
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember"]


def connect(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS readings (ts TEXT NOT NULL, period TEXT NOT NULL,"
               " remaining_gb REAL NOT NULL, total_gb REAL NOT NULL)")
    return db


def add_reading(db, period, remaining_gb, total_gb, now=None):
    now = now or datetime.now(TZ)
    db.execute("INSERT INTO readings VALUES (?,?,?,?)",
               (now.isoformat(timespec="seconds"), period, float(remaining_gb), float(total_gb)))
    db.commit()


def periods(db):
    rows = db.execute("SELECT period, MIN(ts) FROM readings GROUP BY period ORDER BY 2 DESC").fetchall()
    return [r[0] for r in rows]


def month_of(period):
    """'Oktober 2026' -> (2026, 10); None if the label is unknown."""
    try:
        name, year = period.split()
        return int(year), MONTHS.index(name) + 1
    except ValueError:
        return None


def summarize(rows, period):
    """rows: [(ts_iso, remaining_gb, total_gb)] sorted by ts. Returns the numbers the page shows."""
    ym = month_of(period)
    points = [(datetime.fromisoformat(ts), tot - rem, tot) for ts, rem, tot in rows]
    t_last, used, total = points[-1]
    days_in_month = calendar.monthrange(*ym)[1] if ym else 31
    # last reading per day -> daily usage; total - remaining stays right when a pass adds volume
    per_day = {}
    for t, u, _ in points:
        per_day[t.day] = u
    # history starting mid-month: the first day only counts from its first reading
    prev = 0.0 if points[0][0].day == 1 else points[0][1]
    daily = {}
    for day in sorted(per_day):
        daily[day] = per_day[day] - prev
        prev = per_day[day]
    # Telekom reports usage since month start, so this average holds even for a history begun mid-month
    elapsed = t_last.day - 1 + (t_last.hour + t_last.minute / 60) / 24
    avg = used / elapsed if elapsed > 0 else 0.0
    month_end = datetime(t_last.year, t_last.month, days_in_month, 23, 59, tzinfo=t_last.tzinfo)
    rest_days = max((month_end - t_last).total_seconds() / 86400, 0)
    projected = used + avg * rest_days
    exhausted = next((t for t, u, tot in points if u >= tot), None)
    exhaust_day = None
    if exhausted is None and avg > 0 and projected > total:
        exhaust_day = t_last.day + (total - used) / avg
    return {"points": points, "used": used, "total": total, "avg": avg, "projected": projected,
            "daily": daily, "days": days_in_month, "exhausted": exhausted, "exhaust_day": exhaust_day,
            "last": t_last}


def _gb(x):
    return f"{x:.2f}".replace(".", ",")


def _x(day_frac, days, w, pad):
    return pad + (day_frac - 1) / days * (w - 2 * pad) if days else pad


def chart_cumulative(s, w=760, h=280, pad=40):
    days, total = s["days"], s["total"]
    top = max(total, s["projected"], s["used"]) * 1.1 or 1
    y = lambda v: h - pad - v / top * (h - 2 * pad)
    fx = lambda t: _x(t.day + (t.hour + t.minute / 60) / 24, days, w, pad)
    pts = " ".join(f"{fx(t):.1f},{y(u):.1f}" for t, u, _ in s["points"])
    last = s["last"]
    parts = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Verbrauch kumuliert">',
             f'<line class="axis" x1="{pad}" y1="{h-pad}" x2="{w-pad}" y2="{h-pad}"/>',
             f'<line class="quota" x1="{pad}" y1="{y(total):.1f}" x2="{w-pad}" y2="{y(total):.1f}"/>',
             f'<text class="lbl" x="{w-pad}" y="{y(total)-6:.1f}" text-anchor="end">Kontingent {_gb(total)} GB</text>',
             f'<line class="proj" x1="{fx(last):.1f}" y1="{y(s["used"]):.1f}" x2="{_x(days + 1, days, w, pad):.1f}" y2="{y(s["projected"]):.1f}"/>',
             f'<polyline class="used" points="{pts}"/>']
    for d in range(1, days + 1, 5):
        parts.append(f'<text class="lbl" x="{_x(d, days, w, pad):.1f}" y="{h-pad+16}" text-anchor="middle">{d}.</text>')
    parts.append(f'<text class="lbl" x="{pad-6}" y="{y(0):.1f}" text-anchor="end">0</text></svg>')
    return "".join(parts)


def chart_daily(s, w=760, h=200, pad=40):
    days, daily = s["days"], s["daily"]
    top = max([*daily.values(), s["avg"], 0.1]) * 1.15
    y = lambda v: h - pad - v / top * (h - 2 * pad)
    bw = (w - 2 * pad) / days * 0.7
    parts = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Verbrauch pro Tag">',
             f'<line class="axis" x1="{pad}" y1="{h-pad}" x2="{w-pad}" y2="{h-pad}"/>']
    for d, v in daily.items():
        x = _x(d + 0.15, days, w, pad)
        parts.append(f'<rect class="bar" x="{x:.1f}" y="{y(max(v, 0)):.1f}" width="{bw:.1f}" height="{h-pad-y(max(v, 0)):.1f}"><title>{d}.: {_gb(v)} GB</title></rect>')
    if s["avg"] > 0:
        parts.append(f'<line class="quota" x1="{pad}" y1="{y(s["avg"]):.1f}" x2="{w-pad}" y2="{y(s["avg"]):.1f}"/>'
                     f'<text class="lbl" x="{w-pad}" y="{y(s["avg"])-6:.1f}" text-anchor="end">Ø {_gb(s["avg"])} GB/Tag</text>')
    for d in range(1, days + 1, 5):
        parts.append(f'<text class="lbl" x="{_x(d + 0.5, days, w, pad):.1f}" y="{h-pad+16}" text-anchor="middle">{d}.</text>')
    return "".join(parts) + "</svg>"


CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#d2d2d7;--accent:#c8102e;--bar:#0a6cbd}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--fg:#f2f2f7;--muted:#a1a1a6;--line:#3a3a3c;--bar:#4ea1f0}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:820px;margin:0 auto;padding:16px}
nav a{margin-right:12px}a{color:var(--bar)}h1{font-size:1.4rem;margin:.6rem 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px;margin:12px 0}
.kpis div{border:1px solid var(--line);border-radius:8px;padding:8px 10px}.kpis b{display:block;font-size:1.3rem}
.kpis span,.lbl,small{color:var(--muted);fill:var(--muted);font-size:12px}
svg{width:100%;height:auto}.axis{stroke:var(--line)}.quota{stroke:var(--accent);stroke-dasharray:6 4}
.used{fill:none;stroke:var(--fg);stroke-width:2}.proj{stroke:var(--fg);stroke-dasharray:3 4;opacity:.6}.bar{fill:var(--bar)}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left}
@media print{nav,.noprint{display:none}body{max-width:none}}
"""


def render(db, period, beszel_url):
    all_p = periods(db)
    period = period if period in all_p else (all_p[0] if all_p else None)
    head = (f'<!doctype html><html lang="de"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>FüKw Datenvolumen</title><style>{CSS}</style></head><body>'
            f'<nav><a href="{html.escape(beszel_url)}">← Server-Monitoring</a>'
            + "".join(f'<a href="?p={html.escape(p)}">{html.escape(p)}</a>' for p in all_p) + "</nav>")
    if period is None:
        return head + "<h1>FüKw Datenvolumen</h1><p>Noch keine Messwerte.</p></body></html>"
    rows = db.execute("SELECT ts, remaining_gb, total_gb FROM readings WHERE period=? ORDER BY ts", (period,)).fetchall()
    s = summarize(rows, period)
    if s["exhausted"]:
        verdict = f'aufgebraucht am {s["exhausted"]:%d.%m. um %H:%M}'
    elif s["exhaust_day"]:
        verdict = f'reicht bei diesem Tempo bis etwa zum {int(s["exhaust_day"])}.'
    else:
        verdict = "reicht bei diesem Tempo bis Monatsende"
    summary = []
    for p in all_p:
        r = db.execute("SELECT ts, remaining_gb, total_gb FROM readings WHERE period=? ORDER BY ts", (p,)).fetchall()
        m = summarize(r, p)
        end = f'{m["exhausted"]:%d.%m.}' if m["exhausted"] else "–"
        summary.append(f"<tr><td>{html.escape(p)}</td><td>{_gb(m['used'])} / {_gb(m['total'])} GB</td>"
                       f"<td>{_gb(m['avg'])} GB</td><td>{end}</td></tr>")
    return (head + f"<h1>FüKw Datenvolumen · {html.escape(period)}</h1>"
            f'<div class="kpis"><div><span>verbraucht</span><b>{_gb(s["used"])} / {_gb(s["total"])} GB</b></div>'
            f'<div><span>Ø pro Tag</span><b>{_gb(s["avg"])} GB</b></div>'
            f'<div><span>Prognose Monatsende</span><b>{_gb(s["projected"])} GB</b></div>'
            f'<div><span>Kontingent</span><b style="font-size:1rem">{verdict}</b></div></div>'
            f"<h2>Verbrauch im Monat</h2>{chart_cumulative(s)}"
            f"<small>Linie: verbraucht · rot gestrichelt: Kontingent · grau gestrichelt: Prognose bei gleichem Tempo</small>"
            f"<h2>Verbrauch pro Tag</h2>{chart_daily(s)}"
            f"<h2>Alle Monate</h2><table><tr><th>Monat</th><th>verbraucht</th><th>Ø pro Tag</th><th>aufgebraucht am</th></tr>"
            + "".join(summary) + "</table>"
            f'<p><small>Stand {s["last"]:%d.%m.%Y %H:%M} · Quelle: pass.telekom.de, stündlich vom FüKw-Server · '
            f'<a class="noprint" href="api/readings.csv?p={html.escape(period)}">CSV</a></small></p></body></html>')


def make_handler(db_path, token, beszel_url):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            p = parse_qs(u.query).get("p", [None])[0]
            db = connect(db_path)
            try:
                if u.path == "/":
                    self._send(200, render(db, p, beszel_url))
                elif u.path == "/api/readings.csv":
                    rows = db.execute("SELECT ts, period, remaining_gb, total_gb FROM readings"
                                      " WHERE ? IS NULL OR period=? ORDER BY ts", (p, p)).fetchall()
                    self._send(200, "ts,period,remaining_gb,total_gb\n" + "".join(",".join(map(str, r)) + "\n" for r in rows),
                               "text/csv; charset=utf-8")
                else:
                    self._send(404, "not found", "text/plain")
            finally:
                db.close()

        def do_POST(self):
            if urlparse(self.path).path != "/api/reading":
                return self._send(404, "not found", "text/plain")
            auth = self.headers.get("Authorization", "")
            if not token or not hmac.compare_digest(auth, f"Bearer {token}"):
                return self._send(401, "unauthorized", "text/plain")
            try:
                n = int(self.headers.get("Content-Length", 0))
                if not 0 < n <= 4096:
                    raise ValueError("size")
                r = json.loads(self.rfile.read(n))
                period = str(r["period"])[:40]
                rem, tot = float(r["remaining_gb"]), float(r["total_gb"])
                if not (0 <= rem <= 10_000 and 0 < tot <= 10_000):
                    raise ValueError("range")
            except (ValueError, KeyError, TypeError):
                return self._send(400, "bad request", "text/plain")
            db = connect(db_path)
            try:
                add_reading(db, period, rem, tot)
            finally:
                db.close()
            self._send(204, "")

        def log_message(self, fmt, *args):
            pass

    return Handler


if __name__ == "__main__":
    handler = make_handler(os.environ.get("DB_PATH", "/data/volumen.db"),
                           os.environ["INGEST_TOKEN"], os.environ.get("BESZEL_URL", "/"))
    ThreadingHTTPServer(("0.0.0.0", 8091), handler).serve_forever()

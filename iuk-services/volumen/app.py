"""Data-volume history of the vehicle SIM: stores hourly readings, renders a printable month page.

stdlib only. Readings arrive from fuekw-watch via POST /api/reading (Bearer token).
"""
import calendar
import hmac
import html
import json
import math
import os
import sqlite3
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
STALE_AFTER_H = 3  # readings arrive hourly
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
    days_in_month = calendar.monthrange(*(ym or (t_last.year, t_last.month)))[1]
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
    # the period's own month: a late reading still labelled with last month must not build "31 November"
    month_end = datetime(*(ym or (t_last.year, t_last.month)), days_in_month, 23, 59, tzinfo=t_last.tzinfo)
    rest_days = max((month_end - t_last).total_seconds() / 86400, 0)
    projected = used + avg * rest_days
    exhausted = next((t for t, u, tot in points if u >= tot), None)
    exhaust_day = None
    if exhausted is None and avg > 0 and projected > total:
        exhaust_day = t_last.day + (total - used) / avg
    return {"points": points, "used": used, "total": total, "avg": avg, "projected": projected,
            "daily": daily, "days": days_in_month, "exhausted": exhausted, "exhaust_day": exhaust_day,
            "last": t_last}


WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

# chart frame in viewBox units; the SVG scales to the page width
W, PAD_L, PAD_R, PAD_T, PAD_B = 760, 44, 12, 16, 28


def _gb(x):
    return f"{x:.2f}".replace(".", ",")


def _num(x):
    """Axis label: no trailing ',0'."""
    return f"{x:g}".replace(".", ",")


def _x(day_frac, days, w=W):
    """Day 1.0 (start of the 1st) .. days+1 (end of the month) onto the plot width."""
    return PAD_L + (day_frac - 1) / days * (w - PAD_L - PAD_R) if days else PAD_L


def nice_ticks(top, target=4):
    """Round steps (1, 2, 2.5, 5 × 10^k) from 0 up to top."""
    if top <= 0:
        return [0]
    raw = top / target
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    n = int(top // step)
    return [round(i * step, 6) for i in range(n + 1)]


def _weekday(ym, day):
    try:
        return WEEKDAYS[date(ym[0], ym[1], day).weekday()]
    except (TypeError, ValueError):
        return ""


def _grid(ticks, y, w=W):
    out = []
    for t in ticks:
        out.append(f'<line class="grid" x1="{PAD_L}" y1="{y(t):.1f}" x2="{w-PAD_R}" y2="{y(t):.1f}"/>'
                   f'<text class="lbl" x="{PAD_L-8}" y="{y(t)+4:.1f}" text-anchor="end">{_num(t)}</text>')
    return "".join(out)


def _day_labels(days, offset=0.0, h=0, w=W):
    return "".join(f'<text class="lbl" x="{_x(d + offset, days, w):.1f}" y="{h-8}" text-anchor="middle">{d}.</text>'
                   for d in (1, 5, 10, 15, 20, 25, days))


def _figure(svg, pts, label):
    data = html.escape(json.dumps(pts, ensure_ascii=False, separators=(",", ":")))
    return (f'<figure class="chart" tabindex="0" aria-label="{label}" data-pts="{data}">{svg}'
            f'<div class="tip" aria-hidden="true"></div></figure>')


def chart_cumulative(s, period=None, w=W, h=280):
    days, total = s["days"], s["total"]
    ym = month_of(period) if period else None
    top = max(total, s["projected"], s["used"]) * 1.08 or 1
    y = lambda v: h - PAD_B - v / top * (h - PAD_T - PAD_B)
    fx = lambda t: _x(t.day + (t.hour + t.minute / 60) / 24, days, w)
    xy = [(fx(t), y(u)) for t, u, _ in s["points"]]
    line = " ".join(f"{a:.1f},{b:.1f}" for a, b in xy)
    area = f"{xy[0][0]:.1f},{y(0):.1f} {line} {xy[-1][0]:.1f},{y(0):.1f}"
    last = s["last"]
    pts = [[round(a, 1), round(b, 1), f"{_weekday(ym, t.day)} {t:%d.%m., %H:%M}".strip(),
            f"{_gb(u)} GB", f"{_gb(max(tot - u, 0))} GB übrig"]
           for (a, b), (t, u, tot) in zip(xy, s["points"])]
    svg = (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Verbrauch kumuliert">'
           f'<defs><linearGradient id="flaeche{w}" x1="0" y1="0" x2="0" y2="1">'
           '<stop offset="0" stop-color="currentColor" stop-opacity=".14"/>'
           '<stop offset="1" stop-color="currentColor" stop-opacity="0"/></linearGradient></defs>'
           + _grid(nice_ticks(top), y, w)
           + f'<line class="quota" x1="{PAD_L}" y1="{y(total):.1f}" x2="{w-PAD_R}" y2="{y(total):.1f}"/>'
           f'<text class="lbl quota-lbl" x="{w-PAD_R}" y="{y(total)-7:.1f}" text-anchor="end">Kontingent {_gb(total)} GB</text>'
           f'<line class="proj" x1="{fx(last):.1f}" y1="{y(s["used"]):.1f}" x2="{_x(days + 1, days, w):.1f}" y2="{y(s["projected"]):.1f}"/>'
           f'<polygon class="area" fill="url(#flaeche{w})" points="{area}"/>'
           f'<polyline class="used" points="{line}"/>'
           f'<circle class="now" cx="{xy[-1][0]:.1f}" cy="{xy[-1][1]:.1f}" r="4"/>'
           f'<line class="guide" x1="0" y1="{PAD_T}" x2="0" y2="{h-PAD_B}"/>'
           f'<circle class="dot" cx="0" cy="0" r="5"/>'
           + _day_labels(days, h=h, w=w) + "</svg>")
    return _figure(svg, pts, "Verbrauch im Monat")


def chart_daily(s, period=None, w=W, h=220):
    days, daily, avg = s["days"], s["daily"], s["avg"]
    ym = month_of(period) if period else None
    top = max([*daily.values(), avg, 0.1]) * 1.12
    y = lambda v: h - PAD_B - v / top * (h - PAD_T - PAD_B)
    slot = (w - PAD_L - PAD_R) / days
    bw = max(slot * 0.62, 2)
    last = s["last"]
    bars, pts = [], []
    for i, (d, v) in enumerate(sorted(daily.items())):
        cx = _x(d + 0.5, days, w)
        yt = y(max(v, 0))
        partial = d == last.day and (last.hour, last.minute) < (23, 0)
        if partial:
            sub = f"bis {last:%H:%M}"
        elif avg > 0:
            diff = v - avg
            sub = f"{'+' if diff >= 0 else '−'}{_gb(abs(diff))} GB {'über' if diff >= 0 else 'unter'} Ø"
        else:
            sub = ""
        cls = "bar heute" if partial else "bar"
        bars.append(f'<rect class="{cls}" data-i="{i}" style="--i:{i}" x="{cx - bw/2:.1f}" y="{yt:.1f}"'
                    f' width="{bw:.1f}" height="{max(y(0) - yt, 0):.1f}" rx="{min(bw/2, 3):.1f}"/>')
        pts.append([round(cx, 1), round(yt, 1), f"{_weekday(ym, d)} {d}.".strip(), f"{_gb(v)} GB", sub])
    avg_line = ""
    if avg > 0:
        avg_line = (f'<line class="avg" x1="{PAD_L}" y1="{y(avg):.1f}" x2="{w-PAD_R}" y2="{y(avg):.1f}"/>'
                    f'<text class="lbl" x="{w-PAD_R}" y="{y(avg)-7:.1f}" text-anchor="end">Ø {_gb(avg)} GB</text>')
    svg = (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Verbrauch pro Tag">'
           + _grid(nice_ticks(top), y, w) + "".join(bars) + avg_line
           + _day_labels(days, offset=0.5, h=h, w=w) + "</svg>")
    return _figure(svg, pts, "Verbrauch pro Tag")


CSS = """
:root{color-scheme:light;--bg:#fbfbfa;--panel:#fff;--fg:#141414;--muted:#77777a;--faint:#a5a5a8;
--line:#e6e6e3;--grid:#efefec;--bar:#c4c4c6;--accent:#c8102e;
--spring:cubic-bezier(.34,1.56,.64,1);--ease:cubic-bezier(.2,.7,.2,1)}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#0f0f10;--panel:#161618;--fg:#ececee;
--muted:#8e8e93;--faint:#5c5c60;--line:#2a2a2d;--grid:#1f1f22;--bar:#48484c;--accent:#ff4d5e}}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);margin:0;font:15px/1.5 "Geist","Inter",system-ui,-apple-system,"Segoe UI",sans-serif;
-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}
main{max-width:860px;margin:0 auto;padding:20px 16px 40px}
a{color:inherit}
.back{display:inline-flex;gap:6px;align-items:center;color:var(--muted);text-decoration:none;font-size:13px;
transition:color .2s,transform .3s var(--spring)}
.back:hover{color:var(--fg);transform:translateX(-2px)}
.kicker{display:block;color:var(--muted);font-size:13px;letter-spacing:.02em;margin-top:18px}
h1{font-size:clamp(1.6rem,5vw,2.1rem);font-weight:600;letter-spacing:-.025em;line-height:1.1;margin:2px 0 14px}
h2{font-size:13px;font-weight:500;color:var(--muted);letter-spacing:.02em;margin:0 0 10px}
.months{display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;margin:0 -16px;padding:2px 16px 6px}
.months::-webkit-scrollbar{display:none}
.months a{flex:none;text-decoration:none;font-size:13px;color:var(--muted);border:1px solid var(--line);
border-radius:999px;padding:5px 12px;transition:color .2s,border-color .2s,background .2s,transform .3s var(--spring)}
.months a:hover{color:var(--fg);border-color:var(--faint);transform:translateY(-1px)}
.months a[aria-current]{background:var(--fg);border-color:var(--fg);color:var(--bg)}
.stale{border:1px solid var(--accent);border-radius:10px;padding:10px 12px;color:var(--accent);margin:14px 0 0}
section{border-top:1px solid var(--line);padding:20px 0 6px;margin-top:18px}
.hero{display:grid;gap:12px}
.big{margin:0;font-size:clamp(2rem,9vw,3.2rem);font-weight:600;letter-spacing:-.04em;line-height:1}
.big span{font-size:.42em;font-weight:500;letter-spacing:-.01em;color:var(--muted)}
.meter{position:relative;height:8px;border-radius:999px;background:var(--grid);overflow:hidden}
.meter i{position:absolute;inset:0 auto 0 0;border-radius:inherit;width:var(--p);transform-origin:left;
animation:fuellen 1s var(--spring) both}
.meter .fill{background:var(--fg)}
.meter .ahead{background:repeating-linear-gradient(-45deg,var(--faint) 0 2px,transparent 2px 5px);opacity:.8}
.meter.voll .fill{background:var(--accent)}
.verdict{margin:0;color:var(--muted)}.verdict.leer{color:var(--accent)}
.kpis{display:grid;grid-template-columns:repeat(3,1fr);margin:0}
.kpis div{padding:4px 14px;border-left:1px solid var(--line);min-width:0}.kpis div:first-child{border-left:0;padding-left:0}
.kpis dt{font-size:12px;color:var(--muted)}.kpis dd{margin:0;font-size:1.15rem;font-weight:600;letter-spacing:-.01em;white-space:nowrap}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:12px;color:var(--muted);margin:2px 0 0}
.legend span{display:inline-flex;align-items:center;gap:6px}
.legend i{width:16px;height:0;border-top:2px solid var(--fg)}
.legend .k{border-top:2px dashed var(--accent)}.legend .p{border-top:2px dashed var(--muted)}
figure.chart{position:relative;margin:0;color:var(--fg);outline:none;touch-action:pan-y}
figure.chart:focus-visible{box-shadow:0 0 0 2px var(--bg),0 0 0 3px var(--faint);border-radius:6px}
svg{display:block;width:100%;height:auto;overflow:visible}
svg line,svg polyline{vector-effect:non-scaling-stroke}
.lbl{fill:var(--faint);font-size:12px}
.grid{stroke:var(--grid)}
.quota{stroke:var(--accent);stroke-dasharray:5 4;opacity:.85}.quota-lbl{fill:var(--accent);opacity:.85}
.avg{stroke:var(--muted);stroke-dasharray:3 4}
.proj{stroke:var(--muted);stroke-dasharray:3 4}
.area{animation:auftauchen .8s .4s var(--ease) both}
.used{fill:none;stroke:var(--fg);stroke-width:1.75;stroke-linejoin:round;stroke-linecap:round;
animation:aufdecken 1.1s var(--ease) both}
.now{fill:var(--fg);stroke:var(--bg);stroke-width:2;animation:auftauchen .4s 1s both}
.guide{stroke:var(--faint);opacity:0;transition:opacity .15s}
.dot{fill:var(--bg);stroke:var(--fg);stroke-width:2;opacity:0;transition:opacity .15s}
.chart.aktiv .guide,.chart.aktiv .dot{opacity:1}
.bar{fill:var(--bar);transform-box:fill-box;transform-origin:bottom;
animation:wachsen .7s var(--spring) both;animation-delay:calc(var(--i) * 18ms);
transition:fill .2s,opacity .2s}
.bar.heute{opacity:.55}
.chart.aktiv .bar{opacity:.4}
.chart.aktiv .bar.on{fill:var(--fg);opacity:1}
.tip{position:absolute;left:0;top:0;pointer-events:none;z-index:2;min-width:96px;padding:7px 10px;border-radius:10px;
background:var(--panel);border:1px solid var(--line);box-shadow:0 6px 24px rgb(0 0 0 / .10);
font-size:12px;line-height:1.35;white-space:nowrap;opacity:0;
transform:translate(-50%,calc(-100% - 14px)) scale(.94);transform-origin:50% 100%;
transition:opacity .15s,transform .35s var(--spring),left .25s var(--spring),top .25s var(--spring)}
.tip.zeigen{opacity:1;transform:translate(-50%,calc(-100% - 10px)) scale(1)}
.tip b{display:block;font-size:15px;font-weight:600;letter-spacing:-.01em}
.tip span,.tip small{display:block;color:var(--muted);font-size:12px}
table{border-collapse:collapse;width:100%;font-size:14px}
td,th{border-bottom:1px solid var(--line);padding:9px 8px 9px 0;text-align:left;white-space:nowrap}
th{font-size:12px;font-weight:500;color:var(--muted)}
td:not(:first-child),th:not(:first-child){text-align:right;padding-right:0;padding-left:8px}
tr.aktuell td:first-child{font-weight:600}
tbody tr{transition:background .2s}tbody tr:hover{background:var(--grid)}
.tabelle{overflow-x:auto}
footer{margin-top:22px;color:var(--muted);font-size:12px}
footer a{color:var(--muted)}footer a:hover{color:var(--fg)}
@keyframes wachsen{from{transform:scaleY(0)}}
@keyframes fuellen{from{transform:scaleX(0)}}
@keyframes aufdecken{from{clip-path:inset(-10px 100% -10px 0)}to{clip-path:inset(-10px 0 -10px 0)}}
@keyframes auftauchen{from{opacity:0}}
.schmal{display:none}
@media (max-width:560px){.breit{display:none}.schmal{display:block}
thead{display:none}tbody tr{display:grid;grid-template-columns:1fr auto;column-gap:12px;border-bottom:1px solid var(--line);padding:8px 0}
tbody td{border:0;padding:0!important;text-align:left!important}tbody td:nth-child(even){text-align:right!important}
tbody td:nth-child(n+3){font-size:12px;color:var(--muted)}tbody td:nth-child(n+3)::before{content:attr(data-l) " "}td.ohne{visibility:hidden}
.kpis{grid-template-columns:1fr 1fr;row-gap:12px}
.kpis div:nth-child(3){border-left:0;padding-left:0}.kpis dd{font-size:1.05rem}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
@media print{.back,.months,.noprint,.tip{display:none}main{max-width:none}*{animation:none!important}}
"""

JS = """
(()=>{const figs=[...document.querySelectorAll('figure.chart')];
const hideAll=except=>figs.forEach(f=>f!==except&&f._hide&&f._hide());
figs.forEach(fig=>{const svg=fig.querySelector('svg'),tip=fig.querySelector('.tip'),pts=JSON.parse(fig.dataset.pts);
const guide=svg.querySelector('.guide'),dot=svg.querySelector('.dot'),bars=[...svg.querySelectorAll('.bar')];let cur=-1;
if(!pts.length)return;
const show=i=>{if(i===cur)return;cur=i;const p=pts[i];hideAll(fig);fig.classList.add('aktiv');
bars.forEach(b=>b.classList.toggle('on',+b.dataset.i===i));
if(guide){guide.setAttribute('x1',p[0]);guide.setAttribute('x2',p[0])}
if(dot){dot.setAttribute('cx',p[0]);dot.setAttribute('cy',p[1])}
tip.replaceChildren(...[['span',p[2]],['b',p[3]],['small',p[4]]].filter(x=>x[1]).map(([t,v])=>{const e=document.createElement(t);e.textContent=v;return e}));
const m=svg.getScreenCTM(),r=fig.getBoundingClientRect(),q=new DOMPoint(p[0],p[1]).matrixTransform(m),w=tip.offsetWidth/2;
tip.style.left=Math.max(w,Math.min(r.width-w,q.x-r.left))+'px';tip.style.top=(q.y-r.top)+'px';tip.classList.add('zeigen')};
const hide=()=>{cur=-1;fig.classList.remove('aktiv');tip.classList.remove('zeigen');bars.forEach(b=>b.classList.remove('on'))};
fig._hide=hide;
const nearest=cx=>{const x=new DOMPoint(cx,0).matrixTransform(svg.getScreenCTM().inverse()).x;let b=0,d=1/0;
pts.forEach((p,i)=>{const e=Math.abs(p[0]-x);if(e<d){d=e;b=i}});return b};
svg.addEventListener('pointermove',e=>show(nearest(e.clientX)));
svg.addEventListener('pointerdown',e=>show(nearest(e.clientX)));
svg.addEventListener('pointerleave',e=>{if(e.pointerType==='mouse')hide()});
fig.addEventListener('keydown',e=>{const k={ArrowLeft:-1,ArrowRight:1}[e.key];
if(k){e.preventDefault();show(Math.max(0,Math.min(pts.length-1,cur<0?pts.length-1:cur+k)))}else if(e.key==='Escape')hide()});
fig.addEventListener('blur',hide)});
document.addEventListener('pointerdown',e=>{if(!e.target.closest('figure.chart'))hideAll()});
addEventListener('resize',()=>hideAll())})();
"""


def stale_hint(last, now):
    hours = (now - last).total_seconds() / 3600
    if hours < STALE_AFTER_H:
        return ""
    return f'<p class="stale">Veraltet: letzter Messwert vor {hours:.0f} Stunden. Die Zahlen unten zeigen nicht den aktuellen Stand.</p>'


def render(db, period, beszel_url, now=None):
    all_p = periods(db)
    period = period if period in all_p else (all_p[0] if all_p else None)
    months = "".join(f'<a href="?p={html.escape(p)}"{" aria-current=page" if p == period else ""}>{html.escape(p)}</a>'
                     for p in all_p)
    head = (f'<!doctype html><html lang="de"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>FüKw Datenvolumen</title><style>{CSS}</style></head><body><main>'
            f'<a class="back" href="{html.escape(beszel_url)}">← Server-Monitoring</a>'
            f'<span class="kicker">FüKw</span><h1>Datenvolumen</h1>'
            + (f'<nav class="months" aria-label="Monat">{months}</nav>' if all_p else ""))
    if period is None:
        return head + "<section><p>Noch keine Messwerte.</p></section></main></body></html>"
    rows = db.execute("SELECT ts, remaining_gb, total_gb FROM readings WHERE period=? ORDER BY ts", (period,)).fetchall()
    s = summarize(rows, period)
    if s["exhausted"]:
        verdict = f'aufgebraucht am {s["exhausted"]:%d.%m. um %H:%M}'
    elif s["exhaust_day"]:
        verdict = f'reicht bei diesem Tempo bis etwa zum {int(s["exhaust_day"])}.'
    else:
        verdict = "reicht bei diesem Tempo bis Monatsende"
    knapp = bool(s["exhausted"] or s["exhaust_day"])
    summary = []
    for p in all_p:
        r = db.execute("SELECT ts, remaining_gb, total_gb FROM readings WHERE period=? ORDER BY ts", (p,)).fetchall()
        m = summarize(r, p)
        end = f'{m["exhausted"]:%d.%m.}' if m["exhausted"] else "–"
        cls = ' class="aktuell"' if p == period else ""
        summary.append(f'<tr{cls}><td><a href="?p={html.escape(p)}">{html.escape(p)}</a></td>'
                       f"<td>{_gb(m['used'])} / {_gb(m['total'])} GB</td>"
                       f'<td data-l="Ø">{_gb(m["avg"])} GB</td><td data-l="aufgebraucht"{" class=ohne" if not m["exhausted"] else ""}>{end}</td></tr>')
    hint = stale_hint(s["last"], now or datetime.now(TZ)) if period == all_p[0] else ""
    total = s["total"] or 1
    p_used = min(s["used"] / total, 1) * 100
    p_proj = min(max(s["projected"], s["used"]) / total, 1) * 100
    rest = max(s["total"] - s["used"], 0)
    return (head + hint
            + f'<section class="hero"><p class="big">{_gb(s["used"])}<span> / {_gb(s["total"])} GB verbraucht</span></p>'
            f'<div class="meter{" voll" if s["exhausted"] else ""}" role="img" aria-label="{p_used:.0f} %">'
            f'<i class="ahead" style="--p:{p_proj:.1f}%"></i><i class="fill" style="--p:{p_used:.1f}%"></i></div>'
            f'<p class="verdict{" leer" if knapp else ""}">{verdict}</p>'
            f'<dl class="kpis"><div><dt>übrig</dt><dd>{_gb(rest)} GB</dd></div>'
            f'<div><dt>Ø pro Tag</dt><dd>{_gb(s["avg"])} GB</dd></div>'
            f'<div><dt>Prognose Monatsende</dt><dd>{_gb(s["projected"])} GB</dd></div></dl></section>'
            f'<section><h2>Verbrauch im Monat</h2><div class="breit">{chart_cumulative(s, period)}</div>'
            f'<div class="schmal">{chart_cumulative(s, period, w=360, h=230)}</div>'
            f'<p class="legend"><span><i></i>verbraucht</span><span><i class="k"></i>Kontingent</span>'
            f'<span><i class="p"></i>Prognose</span></p></section>'
            f'<section><h2>Verbrauch pro Tag</h2><div class="breit">{chart_daily(s, period)}</div>'
            f'<div class="schmal">{chart_daily(s, period, w=360, h=190)}</div></section>'
            f'<section><h2>Alle Monate</h2><div class="tabelle"><table><thead><tr><th>Monat</th><th>verbraucht</th>'
            f"<th>Ø pro Tag</th><th>aufgebraucht</th></tr></thead><tbody>" + "".join(summary) + "</tbody></table></div></section>"
            f'<footer>Stand {s["last"]:%d.%m.%Y %H:%M} · pass.telekom.de · '
            f'<a class="noprint" href="api/readings.csv?p={html.escape(period)}">CSV</a></footer>'
            f"</main><script>{JS}</script></body></html>")


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

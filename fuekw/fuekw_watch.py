#!/usr/bin/env python3
"""Hourly watcher: Telekom data-pass volume + Beszel hub reachability -> Telegram."""
import gzip
import http.client
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime

PASS_URL = "https://pass.telekom.de"
LOW_FRACTION = 0.2
FAILS_TO_ALERT = 2  # consecutive hourly runs; mobile uplink blips are normal
UNIT_GB = {"GB": 1.0, "MB": 1 / 1000, "KB": 1 / 1_000_000}
PREFIX = "🚒 FüKw:"


@dataclass
class Volume:
    remaining_gb: float
    total_gb: float
    period: str


def _num(s: str) -> float:
    return float(s.strip().replace(".", "").replace(",", "."))


def parse_pass(html: str) -> Volume | None:
    # Only the first match is used: the page repeats the volume block.
    try:
        rem = re.search(r'remaining-volume-value">\s*([\d.,]+)\s*<', html)
        tot = re.search(r'start-volume">\s*([\d.,]+)\s*<', html)
        unit = re.search(r'volume-unit">\s*([A-Za-z]+)\s*<', html)
        per = re.search(r"Abrechnungsmonat:\s*([^<\n]+)", html)
        if not (rem and tot and unit and per):
            return None
        f = UNIT_GB[unit.group(1).upper()]
        return Volume(_num(rem.group(1)) * f, _num(tot.group(1)) * f, per.group(1).strip())
    except (KeyError, ValueError):
        return None


def _fmt(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def decide(volume, hub_ok, state, now):
    st = dict(state)
    msgs = []

    if volume is None:
        st["pass_fails"] = st.get("pass_fails", 0) + 1
        if st["pass_fails"] >= FAILS_TO_ALERT and not st.get("pass_broken"):
            msgs.append(f"{PREFIX} Volumen-Abfrage kaputt – die Telekom-Seite liefert nichts Lesbares.")
            st["pass_broken"] = True
    else:
        st["pass_fails"] = 0
        if st.get("pass_broken"):
            msgs.append(f"{PREFIX} Entwarnung, die Volumen-Abfrage funktioniert wieder.")
            st["pass_broken"] = False
        if st.get("period") != volume.period:
            st["period"], st["alerted"] = volume.period, []
        alerted = list(st.get("alerted", []))
        left = f"{_fmt(volume.remaining_gb)} von {_fmt(volume.total_gb)} GB"
        if volume.remaining_gb < LOW_FRACTION * volume.total_gb and "low" not in alerted:
            msgs.append(f"{PREFIX} Datenvolumen unter 20 % ({left} übrig, {volume.period}).")
            alerted.append("low")
        if volume.remaining_gb <= 0 and "throttled" not in alerted:
            msgs.append(f"{PREFIX} Datenvolumen aufgebraucht, die SIM ist gedrosselt ({volume.period}).")
            alerted.append("throttled")
        st["alerted"] = alerted
        try:
            last_ok = (now.date() - date.fromisoformat(st["last_weekly"])).days <= 6
        except (KeyError, TypeError, ValueError):  # missing or malformed -> due
            last_ok = False
        if now.weekday() == 0 and now.hour >= 9 and not last_ok:
            msgs.append(f"FüKw-Volumen: {left} übrig ({volume.period})")
            st["last_weekly"] = now.date().isoformat()

    st["hub_fails"] = 0 if hub_ok else st.get("hub_fails", 0) + 1
    if st["hub_fails"] >= FAILS_TO_ALERT and not st.get("hub_down"):
        msgs.append(f"{PREFIX} Hub nicht erreichbar.")
        st["hub_down"] = True
    elif hub_ok and st.get("hub_down"):
        msgs.append(f"{PREFIX} Hub wieder erreichbar.")
        st["hub_down"] = False
    return msgs, st


def load_state(path) -> dict:
    try:
        with open(path) as f:
            state = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    if not isinstance(state, dict):
        return {}
    # drop wrongly typed values so decide() never sees them
    for k in ("period", "last_weekly"):
        if not isinstance(state.get(k), (str, type(None))):
            del state[k]
    for k in ("pass_fails", "hub_fails"):
        if k in state and (type(state[k]) is not int or state[k] < 0):
            del state[k]
    al = state.get("alerted")
    if "alerted" in state and not (isinstance(al, list) and all(isinstance(x, str) for x in al)):
        del state["alerted"]
    return state


def save_state(path, state) -> None:
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())  # vehicle power can drop any time
    os.replace(tmp, path)


def run_once(fetch_pass, check_hub, send, state_path, now) -> None:
    state = load_state(state_path)
    html = fetch_pass()
    msgs, new_state = decide(parse_pass(html) if html else None, check_hub(), state, now)
    for m in msgs:
        send(m)  # raises -> state stays unsaved, alert is retried next run
    save_state(state_path, new_state)


def _fetch_pass() -> str | None:
    req = urllib.request.Request(PASS_URL, headers={"Accept-Encoding": "gzip", "User-Agent": "fuekw-watch"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return body.decode("utf-8", "replace")
    except (OSError, EOFError, http.client.HTTPException, ValueError):
        return None


def _check_hub(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return r.status == 200
    except (OSError, http.client.HTTPException, ValueError):
        return False


def _send(token: str, chat_id: str, text: str) -> None:
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=20).close()


def main() -> None:
    if sys.argv[1:] != ["run"]:
        sys.exit("usage: fuekw_watch.py run")
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    hub = os.environ["HUB_HEALTH_URL"]
    state = os.environ.get("STATE_FILE", "/var/lib/fuekw-watch/state.json")
    run_once(_fetch_pass, lambda: _check_hub(hub), lambda t: _send(token, chat, t), state, datetime.now())


if __name__ == "__main__":
    main()

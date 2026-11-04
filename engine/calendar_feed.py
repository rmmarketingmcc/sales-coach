"""Your meetings, from calendar.json in the data folder.

The Sales Coach app writes calendar.json from the Mac's own Calendar (so Google, Outlook and iCloud
calendars all work, with no login here), and an add-on may supply events instead (addons.calendar).
One event:
    {"id", "title", "start", "end" (ISO 8601 with offset), "notes", "meet" (video link or null),
     "location", "status" ("confirmed" | "cancelled"), "updated",
     "attendees": [{"email", "name", "self": bool, "status": "accepted" | "declined" | "tentative" | "pending"}]}
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import addons

STALE_AFTER = timedelta(hours=6)   # an app that stopped writing: the calendar is unknown, not empty
MEET_LINK = re.compile(r"https://(?:meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}|[\w.-]*zoom\.us/j/\S+|"
                       r"teams\.microsoft\.com/l/meetup-join/\S+)", re.I)
FREEMAIL = {"gmail.com", "googlemail.com", "hotmail.com", "hotmail.dk", "outlook.com", "outlook.dk", "live.com", "live.dk",
            "icloud.com", "me.com", "mac.com", "yahoo.com", "yahoo.dk", "proton.me", "protonmail.com", "gmx.com", "gmx.net",
            "mail.dk", "jubii.dk", "aol.com"}


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _from_file(path: Path) -> list[dict] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    try:
        if datetime.now(timezone.utc) - parse_ts(data.get("updated") or "") > STALE_AFTER:
            return None
    except ValueError:
        return None
    return data.get("events") or []


def events(data_dir: Path, time_min: datetime, time_max: datetime) -> list[dict] | None:
    """Timed events overlapping [time_min, time_max] that you have not declined. None = unknown."""
    items = addons.calendar(time_min, time_max)
    if items is None:
        items = _from_file(data_dir / "calendar.json")
    if items is None:
        return None
    out = []
    for ev in items:
        try:
            s, e = parse_ts(ev["start"]), parse_ts(ev["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if e < time_min or s > time_max or ev.get("status") == "cancelled":
            continue
        if any(a.get("self") and a.get("status") == "declined" for a in ev.get("attendees") or []):
            continue
        if not ev.get("meet"):
            m = MEET_LINK.search(" ".join(str(ev.get(k) or "") for k in ("location", "notes")))
            ev = {**ev, "meet": m.group(0) if m else None}
        out.append(ev)
    return sorted(out, key=lambda ev: ev["start"])


def event_by_id(data_dir: Path, event_id: str) -> dict | None:
    now = datetime.now(timezone.utc)
    return next((ev for ev in events(data_dir, now - timedelta(days=2), now + timedelta(days=30)) or []
                 if ev.get("id") == event_id), None)


def guests(ev: dict) -> list[dict]:
    """The other people on the invite (not you, not rooms)."""
    return [a for a in ev.get("attendees") or [] if not a.get("self") and not a.get("resource")]


def is_call(ev: dict) -> bool:
    """A real meeting: other people or a video link, not an all-day block or your own focus time."""
    return bool(ev.get("start") and ev.get("end") and (guests(ev) or ev.get("meet")))


def own_domains(items: list[dict] | None) -> set[str]:
    """Your company's mail domain, from the address the calendar marks as you (not a free mail)."""
    doms = set()
    for ev in items or []:
        for a in ev.get("attendees") or []:
            dom = (a.get("email") or "").lower().rpartition("@")[2]
            if a.get("self") and dom and dom not in FREEMAIL:
                doms.add(dom)
    return doms


def write_schedule(data_dir: Path, recordings: Path) -> None:
    """The next 24 h of real meetings to .schedule.json: the recorder never cuts a calendar meeting
    short (its stop-after-10-quiet-minutes only applies outside them)."""
    now = datetime.now(timezone.utc)
    items = events(data_dir, now - timedelta(hours=2), now + timedelta(hours=24))
    if items is None:
        return   # keep the old file; the recorder falls back to a 30 min quiet limit once it is stale
    z = lambda v: parse_ts(v).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")   # noqa: E731
    body = {"updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "meetings": [{"title": ev.get("title") or "", "start": z(ev["start"]), "end": z(ev["end"])}
                         for ev in items if is_call(ev)]}
    tmp = recordings / ".schedule.json.tmp"
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1))
    tmp.replace(recordings / ".schedule.json")

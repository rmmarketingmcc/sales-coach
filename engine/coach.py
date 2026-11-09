#!/usr/bin/env python3
"""Sales Coach: a live talk meter, transcript and short cues during your online calls, with no bot in the call.

How: the recorder (Sales Coach Recorder) writes your microphone and the call audio (the other side) to
separate tracks under the recordings folder while a call runs. This reads both as they grow:
- talk meter: 100 ms frames, you vs them, against a target band for the call type
- live transcript: Whisper large-v3 on the Mac (speech.py), about 1.5 s behind speech
- a checklist and a brief for the call, from the invite, your notes, the last call with the same
  people and anything an add-on knows (addons.py); then one short cue at a time from Claude (ai.py)
- time and talk cues are computed here, not by the AI
- window: http://127.0.0.1:8765 (window.html), shown by the Sales Coach app next to the call
When the call ends: the coach's result within ~10 s, notes with next steps about a minute later, and
the call in your call history with its numbers ("Opkald" and "Bliv bedre" in the window).

Usage:
    python3 coach.py serve [--no-open]          # the background service
    python3 coach.py replay <folder> [--speed 1] [--from-min 0] [--minutes 10] [--event-json f] [--no-llm] [--debrief]
    python3 coach.py cues <folder>              # a recorded call through the coach as it is now
    python3 coach.py stats [N]                  # your talk share, last N calls
    python3 coach.py settings [--name ...] [--language da|en] [--ai auto|claude_code|api|off] ...
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import numpy as np

import addons
import ai
import calendar_feed as cal
import prompts as P
from audio import SR, PHONE_SPLIT, TalkMeter, Track
from speech import HALLUCINATIONS, Consolidator, Transcriber, names_from, prompt as speech_prompt

ENGINE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("SALES_COACH_ROOT") or Path.home() / "CallRecordings")          # the recorder's folder
DATA = Path(os.environ.get("SALES_COACH_DATA") or Path.home() / "Library" / "Application Support" / "Sales Coach")
HUD_FILE = ENGINE / "window.html"
# A phone call on speaker: the recorder records while this file exists (recorder/main.swift); the window's
# "Start telefonopkald" button and the Sales Coach menu create and delete it
PHONE_FLAG = ROOT / ".phone_call"
PORT = int(os.environ.get("SALES_COACH_PORT", "8765"))
VERSION = (ENGINE.parent / "VERSION").read_text().strip() if (ENGINE.parent / "VERSION").exists() else "dev"
SETTINGS_FILE = DATA / "settings.json"
FEEDBACK_FILE = DATA / "feedback.jsonl"
PREP_FILE = DATA / "prep.json"
JOINED_FILE = DATA / "joined.json"
GROWTH_FILE = DATA / "growth.json"
AGENDA_FILE = DATA / "agendas.json"   # your own agenda per calendar meeting, pasted before the call
MORNING_FILE = DATA / "morning"
LOCAL_TZ = datetime.now().astimezone().tzinfo

BACKLOG_S = 90                   # started mid-call: only the last 90 s of what came before is transcribed
CUE_MIN_GAP = 4                  # seconds between AI turns; a turn follows a locked-in line from them
CUE_MAX_GAP = 30                 # ...or any new line, at least this often
CUE_SESSION_TURNS = 30           # then a fresh conversation with a short recap, so each turn stays small
ITEM_COOLDOWN = 600              # call seconds before the same checklist item may be cued again
ITEM_CUES_MAX = 2                # ...and at most this often a call (once on a client or team meeting)
CUE_EVERY = 90                   # call seconds between new cues on a sales call: one at a time, time to act on it
CUE_EVERY_OTHER = 150            # ...on a client or team meeting, which wants a quiet assistant
HANGUP_S = 3                     # the call app let go of the mic this long ago = hung up

# Your share of talk time per call type: lower when the job is listening, higher when you present.
# "sales" types get booking cues and the standard questions; the others never do.
CALL_TYPES = {
    "discovery":   {"da": "Første møde",  "en": "First meeting",  "target": (30, 45), "stretch": 75,  "sales": True},
    "reconnect":   {"da": "Genoptag",     "en": "Follow-up",      "target": (30, 45), "stretch": 75,  "sales": True},
    "walkthrough": {"da": "Gennemgang",   "en": "Walkthrough",    "target": (45, 60), "stretch": 150, "sales": True},
    "close":       {"da": "Lukkemøde",    "en": "Closing call",   "target": (35, 50), "stretch": 90,  "sales": True},
    "partner":     {"da": "Partner",      "en": "Partner",        "target": (40, 55), "stretch": 90,  "sales": True},
    "negotiation": {"da": "Forhandling",  "en": "Negotiation",    "target": (35, 50), "stretch": 90,  "sales": True},
    "interview":   {"da": "Jobsamtale",   "en": "Job interview",  "target": (25, 40), "stretch": 90,  "sales": False},
    "one_on_one":  {"da": "1:1-samtale",  "en": "One-on-one",     "target": (30, 45), "stretch": 90,  "sales": False},
    "coaching":    {"da": "Sparring",     "en": "Advice session", "target": (35, 55), "stretch": 120, "sales": False},
    "support":     {"da": "Kundeservice", "en": "Support call",   "target": (30, 50), "stretch": 90,  "sales": False},
    "presentation": {"da": "Oplæg",       "en": "Presentation",   "target": (70, 90), "stretch": 900, "sales": False},
    "review":      {"da": "Kundemøde",    "en": "Client meeting", "target": (40, 55), "stretch": 120, "sales": False},
    "internal":    {"da": "Team",         "en": "Team",           "target": None,     "stretch": 150, "sales": False},
    "other":       {"da": "Andet",        "en": "Other",          "target": (40, 55), "stretch": 120, "sales": False},
}
# relative weights, scaled to the calendar length
DEFAULT_AGENDA = {
    "discovery": [("Åbning og dagsorden", "Opening and agenda", 2), ("Deres situation: spørgsmålene", "Their situation: the questions", 5),
                  ("Hvad jeg ville gøre", "What I would do", 2), ("Næste skridt og dato", "Next step and date", 1)],
    "reconnect": [("Hvad er ændret siden sidst", "What changed since last time", 3), ("Passer det i dag", "Is it a fit today", 4),
                  ("Næste skridt", "Next step", 2)],
    "walkthrough": [("Åbning, deres spørgsmål først", "Opening, their questions first", 2), ("Gennemgangen", "The walkthrough", 5),
                    ("Åbne spørgsmål", "Open questions", 2), ("Næste skridt og dato", "Next step and date", 1)],
    "close": [("Åbning", "Opening", 1), ("Deres åbne spørgsmål", "Their open questions", 3), ("Tilbuddet", "The offer", 3),
              ("Beslutning og startdato", "Decision and start date", 3)],
    "partner": [("Deres kunder og opsætning", "Their clients and setup", 4), ("Sådan kan vi arbejde sammen", "How we could work together", 3),
                ("Pris og næste skridt", "Price and next step", 3)],
    "review": [("Åbning", "Opening", 1), ("Resultater siden sidst", "Results since last time", 5),
               ("Åbne spørgsmål og beslutninger", "Open questions and decisions", 5), ("Næste skridt og dato", "Next step and date", 1)],
    "negotiation": [("Åbning og rammer", "Opening and frame", 1), ("Deres krav", "Their requirements", 3),
                    ("Vores forslag", "Our proposal", 3), ("Aftale og næste skridt", "Agreement and next step", 3)],
    "interview": [("Velkomst og rammer", "Welcome and frame", 1), ("Kandidatens erfaring", "The candidate's experience", 5),
                  ("Kandidatens spørgsmål", "The candidate's questions", 2), ("Næste skridt", "Next step", 1)],
    "one_on_one": [("Hvordan går det", "How are things", 2), ("Deres punkter", "Their points", 4), ("Mine punkter", "My points", 2),
                   ("Aftaler", "Agreements", 1)],
    "coaching": [("Hvad skal du have ud af i dag", "What you want from today", 2), ("Situationen", "The situation", 4),
                 ("Muligheder", "Options", 3), ("Første skridt", "First step", 1)],
    "support": [("Problemet", "The problem", 3), ("Løsningen", "The fix", 4), ("Er det løst", "Is it solved", 1)],
    "presentation": [("Åbning", "Opening", 1), ("Oplægget", "The talk", 7), ("Spørgsmål", "Questions", 2)],
}
TITLE_TYPES = [
    (r"jobsamtale|ansættelsessamtale|job interview|interview", "interview"),
    (r"webinar|oplæg|foredrag|keynote", "presentation"),
    (r"forhandling|negotiation", "negotiation"),
    (r"medarbejdersamtale|\bmus\b|1-on-1|one-on-one|\b1:1\b", "one_on_one"),
    (r"gennemgang|walkthrough|præsentation|presentation|demo", "walkthrough"),
    (r"tilbud|offer|proposal|closing", "close"),
    (r"intro|kemi|discovery|første|first call", "discovery"),
    (r"månedlig|monthly|biweekly|bi-weekly|ugentlig|weekly|status|review|kvartal|quarterly", "review"),
    (r"\bsupport\b|kundeservice|helpdesk", "support"),
    (r"\bsparring\b|\bcoaching\b|konsultation", "coaching"),
    (r"standup|team", "internal"),
]
BUILT_IN_TYPES = set(CALL_TYPES)


def apply_my_types(settings: dict) -> None:
    """Your own meeting types (settings "my_types": [{"name", "target": [from, to]}]) next to the built-in ones."""
    for k in [k for k in CALL_TYPES if k.startswith("my_") and k not in BUILT_IN_TYPES]:
        del CALL_TYPES[k]
    for t in settings.get("my_types") or []:
        name = str(t.get("name") or "").strip()[:40]
        try:
            lo, hi = sorted(int(x) for x in t.get("target") or [])[:2]
        except (TypeError, ValueError):
            continue
        if name:
            key = "my_" + (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "type")
            CALL_TYPES[key] = {"da": name, "en": name, "target": (max(0, lo), min(100, hi)), "stretch": 120,
                               "sales": bool(t.get("sales"))}

T = {
    "da": {"untitled": "Uplanlagt opkald", "loading": "Henter...", "me": "Dig", "them": "Dem",
           "src": {"default": "standard", "title": "fra titlen", "team attendees": "teamet inviteret",
                   "set by you": "valgt af dig", "add-on": "tilføjelse"},
           "stretch_team": "{t} i træk: til pointen, eller tilbage til dagsordenen",
           "stretch": "{t} i træk: stop og stil et spørgsmål", "share5": "Dig: {p}% de sidste 5 min. Spørg, og vær så stille",
           "share5_low": "Dig: {p}% de sidste 5 min. Tag styringen: opsummér, og før mødet videre",
           "pause": "Efter dit spørgsmål: vent 3-5 sekunder, og lad dem svare",
           "over": "Over tid +{t}: rund af nu", "left_book": "{t} tilbage: book næste skridt nu", "left": "{t} tilbage: rund af",
           "open_items": "{n} punkter åbne, {t} tilbage: prioritér",
           "auto_open": {"all": "Ved hvert opkald", "calendar": "Kun kalendermøder", "never": "Aldrig, jeg åbner selv"},
           "website": "Hjemmeside", "join": "Deltag i mødet",
           "days": ["man", "tir", "ons", "tor", "fre", "lør", "søn"],
           "months": ["jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec"],
           "day": "{wd} {d}. {mon}", "date": "{d}. {mon}",
           "notes_ready": "Noter klar", "notes_after": "{title} ({s} sek. efter opkaldet)",
           "result": "Resultat", "std_this": "Standardspørgsmål, dette opkald", "next_steps": "Næste skridt",
           "ask_next": "Spørg næste gang", "coach": "Coach", "practise": "Øv", "vague": "vagt", "none": "ingen",
           "goals": {"on_time": ("Slut til tiden", "slut senest 1 min efter kalenderens sluttid"),
                     "talk": ("Taletid i mål", "din andel af taletiden højst målet for mødetypen"),
                     "my_mono": ("Ingen lange monologer", "din længste tale under grænsen for mødetypen")},
           "g_over": "{m:+d} min", "g_talk": "{p}%, højst {hi}%", "g_mono": "længste {a}, grænse {b}",
           "book_title": "Møde: {names} og {me}", "book_topic": "Opfølgning", "book_unclear": "Dag eller tid uklar ({d} {t}): book selv.",
           "book_missing": "Kun {emails} har en mail i invitationen: tilføj de andre selv.",
           "book_practice": "Øvesamtale: intet bliver booket.", "book_opened": "Åbnet i din kalender: tjek og send invitationen derfra.",
           "notice": "Hej! Jeg optager vores samtale lokalt på min computer, så jeg kan skrive noter bagefter. Den deles ikke{ai}, og lyden slettes {when}. Sig endelig til, hvis du hellere vil have, at jeg ikke optager.",
           "notice_ai": ", men en AI-assistent (Claude) hjælper mig med noterne",
           "notice_now": "lige efter samtalen", "notice_days": "efter {d} dage",
           "busy": "optaget", "them_word": "dem", "and": "og"},
    "en": {"untitled": "Unscheduled call", "loading": "Loading...", "me": "You", "them": "Them",
           "src": {"default": "default", "title": "from the title", "team attendees": "team invited",
                   "set by you": "set by you", "add-on": "add-on"},
           "stretch_team": "{t} in a row: get to the point, or back to the agenda",
           "stretch": "{t} in a row: stop and ask a question", "share5": "You: {p}% the last 5 min. Ask, then stay quiet",
           "share5_low": "You: {p}% the last 5 min. Take the lead: sum up and move the meeting on",
           "pause": "After your question: wait 3-5 seconds and let them answer",
           "over": "Over time +{t}: wrap up now", "left_book": "{t} left: book the next step now", "left": "{t} left: wrap up",
           "open_items": "{n} items open, {t} left: prioritise",
           "auto_open": {"all": "On every call", "calendar": "Calendar meetings only", "never": "Never, I open it myself"},
           "website": "Website", "join": "Join the meeting",
           "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
           "months": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
           "day": "{wd} {d} {mon}", "date": "{d} {mon}",
           "notes_ready": "Notes ready", "notes_after": "{title} ({s} s after the call)",
           "result": "Result", "std_this": "Standard questions, this call", "next_steps": "Next steps",
           "ask_next": "Ask next time", "coach": "Coach", "practise": "Practise", "vague": "vague", "none": "none",
           "goals": {"on_time": ("End on time", "end no later than 1 min after the calendar end"),
                     "talk": ("Talk share on target", "your share of the talking at most the call type's target"),
                     "my_mono": ("No long monologues", "your longest stretch under the call type's limit")},
           "g_over": "{m:+d} min", "g_talk": "{p}%, max {hi}%", "g_mono": "longest {a}, limit {b}",
           "book_title": "Meeting: {names} and {me}", "book_topic": "Follow-up", "book_unclear": "Day or time unclear ({d} {t}): book it yourself.",
           "book_missing": "Only {emails} has an address on the invite: add the others yourself.",
           "book_practice": "Practice call: nothing gets booked.", "book_opened": "Opened in your calendar: check it and send the invite from there.",
           "notice": "Hi! I'm recording our conversation locally on my computer so I can write notes afterwards. It isn't shared{ai}, and the audio is deleted {when}. Just say if you'd rather I didn't record.",
           "notice_ai": ", though an AI assistant (Claude) helps me with the notes",
           "notice_now": "right after the call", "notice_days": "after {d} days",
           "busy": "busy", "them_word": "them", "and": "and"},
}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def mmss(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(max(0, seconds))
    return f"{s // 60}:{s % 60:02d}"


def stamp(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# --------------------------------------------------------------------------- settings

DEFAULTS = {"name": "", "company": "", "about": "", "language": "da", "enabled": True, "auto_open": "all",
            "auto_join": True, "ai": "auto", "team_domains": [], "words": [], "addons": [], "targets": {},
            "keep_audio_days": 7, "keep_notes_days": 365, "notice": True, "notice_auto": True, "notice_text": "",
            "my_types": [], "focus": [], "focus_text": ""}


def load_settings() -> dict:
    s = json.loads(json.dumps(DEFAULTS))
    try:
        s.update(json.loads(SETTINGS_FILE.read_text()))
    except (OSError, ValueError):
        pass
    return s


def save_settings(changes: dict) -> dict:
    s = load_settings()
    for k in ("enabled", "auto_join"):
        if k in changes:
            s[k] = bool(changes[k])
    for k in ("name", "company", "about"):
        if k in changes:
            s[k] = str(changes[k] or "").strip()[:200]
    if changes.get("auto_open") in T["da"]["auto_open"]:
        s["auto_open"] = changes["auto_open"]
    if changes.get("language") in T:
        s["language"] = changes["language"]
    if changes.get("ai") in ("auto", "claude_code", "api", "off"):
        s["ai"] = changes["ai"]
    for k in ("words", "team_domains", "addons"):
        if k in changes:
            v = changes[k]
            s[k] = [x.strip() for x in (v.split(",") if isinstance(v, str) else v or []) if str(x).strip()]
    if "keep_audio_days" in changes:
        try:
            s["keep_audio_days"] = max(0, int(changes["keep_audio_days"]))
        except (TypeError, ValueError):
            pass
    if "keep_notes_days" in changes:   # None / "never" / 0: kept until you delete them
        v = changes["keep_notes_days"]
        try:
            s["keep_notes_days"] = None if v in (None, "", "never") or int(v) <= 0 else max(7, int(v))
        except (TypeError, ValueError):
            pass
    if "notice" in changes:
        s["notice"] = bool(changes["notice"])
    if "notice_auto" in changes:
        s["notice_auto"] = bool(changes["notice_auto"])
    if isinstance(changes.get("my_types"), list):
        s["my_types"] = [{"name": str(t.get("name") or "")[:40], "target": list(t.get("target") or [])[:2],
                          "sales": bool(t.get("sales"))} for t in changes["my_types"][:20] if isinstance(t, dict)]
    if isinstance(changes.get("focus"), list):
        s["focus"] = [str(f).strip()[:120] for f in changes["focus"][:10] if str(f).strip()]
    if "focus_text" in changes:
        s["focus_text"] = str(changes["focus_text"] or "").strip()[:800]
    if "notice_text" in changes:
        s["notice_text"] = str(changes["notice_text"] or "").strip()[:600]
    if changes.get("api_key"):   # never in the settings file: into the Keychain
        ai.save_api_key(str(changes["api_key"]).strip())
    DATA.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=1))
    apply_my_types(s)
    log(f"settings: { {k: v for k, v in s.items() if k not in ('about', 'focus_text')} }")
    return s


def notice_text(ai_on: bool) -> str:
    """The line that tells the other side you record: your own text, or the standard one with your settings in it."""
    s, t = load_settings(), tr()
    days = int(s.get("keep_audio_days", 7))
    return (s.get("notice_text") or t["notice"]).format(ai=t["notice_ai"] if ai_on else "",
                                                        when=t["notice_now"] if days == 0 else t["notice_days"].format(d=days))


_BULLET = re.compile(r"^\s*(?:[-*•–]|\d{1,2}[.)])\s+(.+)$")


def agenda_points(text: str, bullets_only: bool = False) -> list[str]:
    """Your agenda, one point per line ("- ", "• ", "1. " or, pasted, plain lines); at most 12. From an invite
    only list lines count (bullets_only), so its prose is never read as an agenda. Invites may carry HTML."""
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text or "")
    text = re.sub(r"(?i)<br\s*/?>|</?p[^>]*>|</div>|</li>|</?[uo]l[^>]*>", "\n", text)
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", text))
    out: list[str] = []
    for line in text.splitlines():
        m = _BULLET.match(line)
        if bullets_only and not m:
            continue
        point = re.sub(r"\s+", " ", (m.group(1) if m else line)).strip(" -:")
        if 3 <= len(point) <= 160 and point.lower() not in {o.lower() for o in out}:
            out.append(point)
    return out[:12]


def load_agendas() -> dict:
    try:
        return json.loads(AGENDA_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_agenda(event_id: str, points: list[str]) -> None:
    data = {k: v for k, v in load_agendas().items()
            if datetime.now(timezone.utc) - datetime.fromisoformat(v["at"]) < timedelta(days=30)}
    if points:
        data[event_id] = {"points": points, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    else:
        data.pop(event_id, None)
    AGENDA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1))


def forget_call(folder: Path) -> None:
    """Everything of one call: the recording folder (audio, transcript, notes, result) and your feedback rows on it.
    The recorder is told to stop first when it is still recording it (.stop: it deletes the recording itself
    and waits until the call app lets go of the mic)."""
    meta = read_meta(folder) or {}
    if meta.get("status") == "recording":
        (ROOT / ".stop").write_text(folder.name)
        for _ in range(20):   # the recorder checks every second
            if not folder.exists():
                break
            time.sleep(0.5)
        (ROOT / ".stop").unlink(missing_ok=True)   # never left for the next call's recording
    shutil.rmtree(folder, ignore_errors=True)
    try:
        rows = [l for l in FEEDBACK_FILE.read_text().splitlines() if l.strip()]
        keep = [l for l in rows if (json.loads(l).get("started") or "") != (meta.get("started") or "-")]
        if len(keep) != len(rows):
            FEEDBACK_FILE.write_text("".join(l + "\n" for l in keep))
    except (OSError, ValueError):
        pass
    log(f"call deleted on request: {folder.name}")


def tr() -> dict:
    return T.get(load_settings().get("language")) or T["en"]


def me_first() -> str:
    return (load_settings().get("name") or "").split(" ")[0] or tr()["me"]


def type_label(kind: str) -> str:
    return (CALL_TYPES.get(kind) or CALL_TYPES["other"]).get(load_settings().get("language")) or CALL_TYPES["other"]["en"]


def target(kind: str):
    t = (load_settings().get("targets") or {}).get(kind)
    return tuple(t) if t else (CALL_TYPES.get(kind) or CALL_TYPES["other"])["target"]


def sales_kinds() -> set[str]:
    return {k for k, v in CALL_TYPES.items() if v.get("sales")}


def day_str(d: datetime, weekday: bool = True) -> str:
    t = tr()
    return t["day" if weekday else "date"].format(wd=t["days"][d.weekday()], d=d.day, mon=t["months"][d.month - 1])


# --------------------------------------------------------------------------- the call's context

_OWN_SEEN: set[str] = set()   # your company's domain, as the calendar last showed it


def team_domains(items: list[dict] | None = None) -> set[str]:
    """Your team's mail domains: the setting, else the domain the calendar marks as yours."""
    s = load_settings()
    if s.get("team_domains"):
        return {d.lower() for d in s["team_domains"]}
    _OWN_SEEN.update(cal.own_domains(items))
    return set(_OWN_SEEN)


def read_meta(folder: Path) -> dict | None:
    try:
        return json.loads((folder / "meta.json").read_text())
    except (OSError, ValueError):
        return None


def _section(text: str, heading: str, cap: int = 1500) -> str:
    m = re.search(rf"^##+ {re.escape(heading)}\s*$(.*?)(?=^##? |\Z)", text, re.M | re.S | re.I)
    return m.group(1).strip()[:cap] if m else ""


def _summary_of(folder: Path) -> str:
    tx = folder / "transcript.md"
    if not tx.is_file():
        return ""
    head = tx.read_text(errors="ignore").split("## Transcript", 1)[0]
    return head[head.find("## Summary"):] if "## Summary" in head else ""


def last_call_with(emails: list[str], before: datetime, own: set[str]) -> tuple[str, str] | None:
    """The newest recorded call with any of these addresses before this one: its date, its notes'
    summary and the next steps the coach found."""
    want = {e.lower() for e in emails if e and e.lower().rpartition("@")[2] not in own}
    best = None
    for folder in ROOT.iterdir() if want and ROOT.exists() else []:
        meta = read_meta(folder) or {}
        ev = (meta.get("events") or [{}])[0] or {}
        seen = {e.lower() for e in ev.get("attendee_emails") or []}
        try:   # the coach's own record of who was invited
            seen |= {e.lower() for e in json.loads((folder / "copilot.json").read_text()).get("emails") or []}
        except (OSError, ValueError):
            pass
        if not want & seen or meta.get("status") == "practice":
            continue
        try:
            at = datetime.fromisoformat(meta["started"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if at < before - timedelta(minutes=30) and (best is None or at > best[0]):
            best = (at, folder)
    if not best:
        return None
    text = _summary_of(best[1])[:2500]
    if not text.strip() and (best[1] / "transcript.md").is_file():   # no summary: the end, where the next steps are said
        body = (best[1] / "transcript.md").read_text(errors="ignore").split("## Transcript", 1)[-1]
        text = "(No summary for this call; its last minutes:)\n" + body.strip()[-3000:]
    try:
        steps = ((json.loads((best[1] / "copilot.json").read_text()).get("debrief") or {}).get("next_steps") or [])
    except (OSError, ValueError):
        steps = []
    if steps:
        text += "\nNext steps the coach found: " + "; ".join(steps)
    return (best[0].astimezone(LOCAL_TZ).strftime("%Y-%m-%d"), text) if text.strip() else None


def _guest_names(guests: list[dict]) -> list[str]:
    names = []
    for g in guests:
        n = (g.get("name") or "").strip() or (g.get("email") or "").strip()
        if n and n not in names:
            names.append(n)
    return names


def build_context(event: dict | None, started: datetime) -> dict:
    """Title, call type, planned length, agenda, links and the prep text the checklist is built from."""
    t = tr()
    ctx = {"title": "Unscheduled call", "event_id": None, "type": "other", "type_source": "default",
           "planned_min": None, "agenda": [], "prep": "", "prep_sources": [], "who": "Them", "emails": [],
           "attendees": [], "history": "", "links": [], "mode": "other", "stage": "", "std": [], "words": [],
           "prep_parts": [], "history_parts": []}
    if event:
        ctx["title"] = (event.get("title") or "").strip() or ctx["title"]
        ctx["event_id"] = event.get("id")
        ctx["meet"] = event.get("meet")
        try:
            s, e = cal.parse_ts(event["start"]), cal.parse_ts(event["end"])
            ctx["planned_min"] = round((e - s).total_seconds() / 60)
            ctx["start"], ctx["end"] = s.astimezone(timezone.utc).isoformat(), e.astimezone(timezone.utc).isoformat()
        except (KeyError, ValueError):
            pass
        guests = cal.guests(event)
        ctx["emails"] = emails = [g["email"].lower() for g in guests if g.get("email")]
        ctx["attendees"] = _guest_names(guests)
        named = [g["name"] for g in guests if g.get("name")]
        if len(named) == 1 and len(guests) == 1:
            ctx["who"] = named[0].split()[0]
        elif len(emails) == 1:
            first = names_from(emails)
            ctx["who"] = first[0].split()[0] if first else ctx["who"]
        # the invite's own text: often the agenda
        desc = html_lib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", event.get("notes") or ""))).strip()
        if desc and not cal.MEET_LINK.fullmatch(desc):
            ctx["prep_parts"].append("THE INVITE'S TEXT (its agenda, if it has one):\n" + desc[:2000])
            ctx["prep_sources"].append("invite")
        own = team_domains([event])
        if emails and own and all(e.rpartition("@")[2] in own for e in emails):
            ctx["type"], ctx["type_source"] = "internal", "team attendees"
        else:
            low = ctx["title"].lower()
            kind = next((k for pattern, k in TITLE_TYPES if re.search(pattern, low)), None)
            if kind:
                ctx["type"], ctx["type_source"] = kind, "title"
        # their website, from a company address
        dom = next((e.rpartition("@")[2] for e in emails if e.rpartition("@")[2] not in cal.FREEMAIL | own), None)
        if dom:
            ctx["links"].append({"label": t["website"], "url": f"https://{dom}"})
        # where you left off: the last recorded call with them
        last = last_call_with(emails, started, own)
        if last:
            ctx["history_parts"].append(f"LAST CALL WITH THEM ({last[0]}, our recording):\n{last[1]}")
            ctx["prep_sources"].append("last recorded call")
            steps = _section(last[1], "Next steps", 2000)
            if steps:
                ctx["prep_parts"].append(f"NEXT STEPS FROM THE LAST CALL ({last[0]}):\n{steps}")
    addons.context(event, ctx)
    ctx["who"] = re.sub(r"[^\w-]", "", str(ctx.get("who") or "")) or "Them"   # calendar names can carry quotes
    if ctx["type"] not in CALL_TYPES:
        ctx["type"] = "other"
    for q in ctx.get("std") or []:   # the window reads "da" for the question text
        q.setdefault("da", q.get("q") or "")
        q.setdefault("short", q.get("id"))
    if not ctx["agenda"]:
        i = 0 if load_settings().get("language") == "da" else 1
        ctx["agenda"] = [(a[i], a[2]) for a in DEFAULT_AGENDA.get(ctx["type"]) or []]
        total = sum(m for _, m in ctx["agenda"])
        if ctx["agenda"] and ctx["planned_min"] and total:
            ctx["agenda"] = [(n, max(1, round(m * ctx["planned_min"] / total))) for n, m in ctx["agenda"]]
    if ctx.get("meet"):
        ctx["links"] = [{"label": t["join"], "url": ctx["meet"], "kind": "meet"}] + [l for l in ctx["links"] if l.get("url") != ctx["meet"]]
    seen, links = set(), []
    for l in ctx["links"]:
        if l.get("url") and l["url"] not in seen:
            seen.add(l["url"])
            links.append(l)
    ctx["links"] = links
    ctx["prep"] = "\n\n".join(ctx.pop("prep_parts"))
    ctx["history"] = "\n\n".join(ctx.pop("history_parts"))
    return ctx


# --------------------------------------------------------------------------- AI

def _fill(template: str, **extra) -> str:
    s = load_settings()
    return template.format(me=P.me_line(s), **P.lang(s.get("language")), **extra)


def how_ai() -> str:
    return ai.backend(load_settings().get("ai") or "auto")


def ask_json(system: str, user: str, schema: dict) -> dict:
    return ai.ask_json(system, user, schema, how_ai())


def feedback_block(limit: int = 8) -> str:
    """Your notes on earlier calls (the window's feedback box), newest last: they go into every call's
    prompts, so the coach changes with your feedback call by call."""
    try:
        rows = [json.loads(l) for l in FEEDBACK_FILE.read_text().splitlines() if l.strip()]
    except (OSError, ValueError):
        return ""
    notes = [f"- {r.get('at', '')[:10]} ({r.get('rating') or 'no rating'}, {r.get('title', '')[:40]}): {r['note']}"
             for r in rows if (r.get("note") or "").strip()][-limit:]
    return ("\n\nTHEIR FEEDBACK ON EARLIER CALLS (follow it; newer wins when two disagree):\n" + "\n".join(notes)) if notes else ""


def extra_rules(kind: str) -> str:
    notes = addons.prompt_notes(kind)
    out = f"\n\nMORE RULES FROM THEIR SETUP:\n{notes}" if notes else ""
    s = load_settings()
    focus = [f"- {f}" for f in s.get("focus") or []] + ([f"- In their own words: {s['focus_text']}"] if s.get("focus_text") else [])
    if focus and kind in ("plan", "cues", "growth"):
        out += ("\n\nWHAT THEY WANT TO GET BETTER AT AND WANT YOU TO KEEP TRACK OF (their own setting, in their words):\n"
                + "\n".join(focus) + "\nWhen a moment fits one of these, prefer the cue that helps with it; in the result after the "
                "call and in your read of their weeks, say how it went on these, with an example from the call.")
    return out


def playbook(call_type: str) -> list[dict]:
    return [{"id": i, "expert": "Coach", "claim": c, "action": ""} for i, c in P.RULES] + addons.playbook(call_type)


def playbook_text(pb: list[dict]) -> str:
    return ("\n\nPLAYBOOK (ground cues and the debrief in these):\n"
            + "\n".join(f"- [{p['id']}] {p['expert']}: {p['claim']}" + (f" Do: {p['action']}" if p.get("action") else "")
                        for p in pb)) if pb else ""


def make_plan(ctx: dict) -> dict:
    """Checklist + brief for one call (one Claude call)."""
    user = (f"Call: {ctx['title']}\nCall type: {type_label(ctx['type'])}\n"
            f"Planned length: {ctx['planned_min'] or 'unknown'} min\n\n"
            + (ctx["prep"] or "No prep sources found for this call. Use the standard items for the call type.")
            + (f"\n\nHISTORY WITH THEM:\n{ctx['history']}" if ctx.get("history") else ""))
    on_file = [q for q in ctx.get("std") or [] if q.get("on_file")]
    if on_file:
        user += "\n\nANSWERS ON FILE (standard questions):\n" + "\n".join(f"- {q['id']} | {str(q['on_file'])[:300]}" for q in on_file)
    plan = ask_json(_fill(P.PLAN_SYSTEM) + extra_rules("plan") + feedback_block(), user, P.PLAN_SCHEMA)
    plan["items"] = plan["items"][:7]
    facts = {f.get("id"): f.get("text") for f in plan.get("facts") or []}
    for q in on_file:
        if facts.get(q["id"]):
            q["on_file_da"] = facts[q["id"]]   # the window's field name
    return plan


def call_kind_note(kind: str) -> str:
    """Said right where the result is asked for (the system prompt alone was ignored)."""
    if kind in sales_kinds():
        return f"\n\nCALL TYPE: {type_label(kind)}, a sales call."
    return (f"\n\nCALL TYPE: {type_label(kind)}, not a sales call: the next meeting is in the calendar already, so do "
            "not mention booking it anywhere (verdict, moments, practise). There are no standard questions: ask_next is "
            "short questions or topics for next time, as text.")


def tidy_debrief(debrief: dict, plan_items: list[dict], std: list[dict]) -> dict:
    """Answers only to standard questions that exist (with none, the model filled in checklist numbers);
    ask_next as checklist numbers -> that item's line."""
    ids = {str(q.get("id")) for q in std or []}
    debrief["answers"] = [a for a in debrief.get("answers") or [] if str(a.get("id")) in ids]
    by_id = {str(it.get("id")): it.get("say") or it.get("label") for it in plan_items or []}
    if debrief.get("ask_next"):
        debrief["ask_next"] = [by_id.get(str(x), x) if str(x).isdigit() else x for x in debrief["ask_next"]]
    return debrief


def summarize(lines: list[str], title: str, others: list[str], words: list[str], my_notes: str = "") -> str | None:
    if len(lines) < 8:
        return None
    try:
        system = _fill(P.SUMMARY_SYSTEM, who=", ".join(others) or "the other side",
                       terms=", ".join([load_settings().get("name") or ""] + others + words) or "none")
        mine = f"\n\nTHEIR OWN NOTES DURING THE CALL (use them, they know what matters):\n{my_notes.strip()}" if my_notes.strip() else ""
        out = ask_json(system, f"Calendar title: {title}{mine}\n\nTRANSCRIPT:\n" + "\n".join(lines), P.SUMMARY_SCHEMA)
        return out.get("markdown") or None
    except Exception as e:  # noqa: BLE001  (the transcript matters, the summary is a bonus)
        log(f"summary failed: {e}")
        return None


# --------------------------------------------------------------------------- calls coming up

# Calls starting soon, prepared ahead, so the brief is there before the call: event id -> {"start", "title",
# "status", "ctx", "plan", "opened"}.
PREP: dict[str, dict] = {}
PREP_AHEAD = timedelta(minutes=10)      # the window's "next call" view
BRIEF_AHEAD = timedelta(hours=20)       # tomorrow's meetings are prepared without you asking
REFRESH_BEFORE = timedelta(minutes=20)  # prepared again just before, with what changed since
OPEN_BEFORE = timedelta(minutes=10)     # the window shows itself, with the countdown
UP_AHEAD = timedelta(minutes=15)        # the countdown between calls shows from here
JOIN_BEFORE = timedelta(minutes=2)      # the meeting link opens this long before the start...
JOIN_LATE = timedelta(minutes=10)       # ...or up to this long after it: on another call, or away from the Mac
JOINED: dict[str, dict] = {}
_join_asked: dict[str, float] = {}
_prep_lock = threading.Lock()
DAY_PLAN: dict = {}


def _save_prep() -> None:
    """PREP on disk, so a restart does not prepare the day again (one Claude call per call)."""
    try:
        data = {k: {**v, "start": v["start"].isoformat()} for k, v in PREP.items() if v.get("status") == "ready"}
        tmp = PREP_FILE.with_name(PREP_FILE.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, default=str))
        os.replace(tmp, PREP_FILE)
    except (OSError, TypeError, ValueError) as e:
        log(f"prep not saved: {e}")


def _load_prep() -> None:
    try:
        data = json.loads(PREP_FILE.read_text())
    except (OSError, ValueError):
        return
    now = datetime.now(timezone.utc)
    for k, v in data.items():
        v["start"] = datetime.fromisoformat(v["start"])
        if v["start"] > now - timedelta(hours=3):
            PREP[k] = v


def prep_upcoming() -> None:
    """Every minute: calendar calls starting within BRIEF_AHEAD get their context, checklist and brief; again
    when the event changes, and once more just before the call."""
    if not _prep_lock.acquire(blocking=False):
        return
    try:
        now = datetime.now(timezone.utc)
        changed = False
        items = cal.events(DATA, now - timedelta(minutes=5), now + BRIEF_AHEAD)
        use_ai = how_ai() != "off"
        for ev in items or []:
            if not cal.is_call(ev):
                continue
            start = cal.parse_ts(ev["start"])
            if not now - timedelta(minutes=5) <= start <= now + BRIEF_AHEAD:
                continue
            old = PREP.get(ev["id"]) or {}
            moved = old and (old.get("updated") != ev.get("updated") or old["start"] != start)
            late = (old.get("status") == "ready" and not old.get("refreshed") and start - now <= REFRESH_BEFORE
                    and old.get("prepared_at", "") < (start - REFRESH_BEFORE).isoformat())
            if old and not moved and not late:
                continue
            entry = {"start": start, "title": ev.get("title") or "", "status": "preparing", "updated": ev.get("updated"),
                     "opened": old.get("opened"), "refreshed": bool(late)}
            PREP[ev["id"]] = entry if not old else {**old, **{k: v for k, v in entry.items() if k != "status"}}
            log(f"preparing {'again' if old else 'ahead'}: {entry['title']}")
            try:
                ctx = build_context(ev, start)
                PREP[ev["id"]].update(ctx=ctx, plan=make_plan(ctx) if use_ai else None, status="ready",
                                      prepared_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
                changed = True
                log(f"prepared: {entry['title']}")
            except Exception as e:  # noqa: BLE001
                PREP[ev["id"]]["status"] = f"failed: {e}"
                log(f"prepare failed for {entry['title']}: {e}")
        for key in [k for k, v in PREP.items() if v["start"] < now - timedelta(hours=3)]:
            PREP.pop(key)
            changed = True
        # moved out of the window, cancelled or declined since it was prepared: gone here too (only on an
        # answer from the calendar; None = unknown, nothing is dropped then)
        if items is not None:
            live = {ev["id"] for ev in items if cal.is_call(ev)}
            for key in [k for k, v in PREP.items() if k not in live and v["start"] >= now - timedelta(minutes=5)]:
                log(f"no longer in the calendar: {PREP[key]['title']}")
                PREP.pop(key)
                changed = True
        if changed:
            _save_prep()
    finally:
        _prep_lock.release()


def day_plan() -> list[dict]:
    """The coming calls for the window's day list: an add-on's (with what is missing before each), else the
    prepared ones."""
    got = addons.day_plan()
    if got is not None:
        return got
    return [{"event_id": k, "start": v["start"].isoformat(), "title": v["title"],
             "type": (v.get("ctx") or {}).get("type") or "other", "items": []}
            for k, v in sorted(PREP.items(), key=lambda kv: kv[1]["start"])]


def briefs() -> dict:
    """The prepared calls of the coming day, for the window's call view."""
    out = {}
    for k, v in PREP.items():
        if v.get("status") != "ready" or not v.get("ctx"):
            continue
        ctx, plan = v["ctx"], v.get("plan") or {}
        out[k] = {"title": v["title"], "start": v["start"].isoformat(), "type_label": type_label(ctx["type"]),
                  "mode": ctx.get("mode") or "other", "stage": ctx.get("stage") or "", "goal": plan.get("goal") or "",
                  "brief": plan.get("brief"), "ask": [it["say"] for it in plan.get("items") or []],
                  "links": ctx.get("links") or [], "prepared_at": v.get("prepared_at"), "sessions": []}
    return out


def next_meeting(now: datetime, exclude: str | None = None) -> dict | None:
    soon = [(k, v) for k, v in PREP.items() if k != exclude and v["start"] > now - timedelta(minutes=2)]
    if not soon:
        return None
    k, v = min(soon, key=lambda kv: kv[1]["start"])
    ctx = v.get("ctx") or {}
    return {"title": v["title"], "start": v["start"].isoformat(), "start_dt": v["start"], "meet": ctx.get("meet"),
            "type_label": type_label(ctx.get("type") or "other")}


def _load_joined() -> None:
    try:
        JOINED.update(json.loads(JOINED_FILE.read_text()))
    except (OSError, ValueError):
        pass


def _mark_joined(key: str, how: str) -> None:
    """how: opened (the link was opened), attended (you were on it), skipped (moved, cancelled or declined),
    nolink (nothing to open)."""
    if (JOINED.get(key) or {}).get("how") == how:
        return
    now = datetime.now(timezone.utc)
    JOINED[key] = {"how": how, "at": now.isoformat(timespec="seconds")}
    for k in [k for k, v in JOINED.items() if v.get("at", "") < (now - timedelta(days=3)).isoformat()]:
        JOINED.pop(k)
    try:
        JOINED_FILE.write_text(json.dumps(JOINED, indent=1))
    except OSError as e:
        log(f"join state not saved: {e}")


def upcoming(now: datetime | None = None) -> dict | None:
    """The meeting to count down to between calls: the soonest prepared one in the next hour, or one that
    started under JOIN_LATE ago and is not over."""
    now = now or datetime.now(timezone.utc)
    soon = []
    for k, v in list(PREP.items()):
        if (JOINED.get(k) or {}).get("how") in ("attended", "skipped"):
            continue
        if not now - JOIN_LATE <= v["start"] <= now + timedelta(hours=1):
            continue
        end = (v.get("ctx") or {}).get("end")
        if end and datetime.fromisoformat(end) <= now:
            continue
        soon.append((k, v))
    if not soon:
        return None
    k, v = min(soon, key=lambda kv: kv[1]["start"])
    ctx = v.get("ctx") or {}
    return {"id": k, "title": v["title"], "start": v["start"].isoformat(),
            "in_s": round((v["start"] - now).total_seconds()), "meet": ctx.get("meet"),
            "type_label": type_label(ctx.get("type") or "other"), "mode": ctx.get("mode") or "other",
            "join_at": (v["start"] - JOIN_BEFORE).isoformat(), "opened": (JOINED.get(k) or {}).get("how") == "opened",
            "agenda": (load_agendas().get(k) or {}).get("points") or []}


def join_tick(app: "App", settings: dict) -> None:
    """Open the next meeting's link JOIN_BEFORE before it starts, once. Never over a call you are on (it opens
    when that call ends) and never while you are away from the Mac, up to JOIN_LATE after the start. The
    meeting's own screen comes up; you still click to join."""
    s = app.session
    if s and s.ctx.get("event_id"):
        _mark_joined(s.ctx["event_id"], "attended")
    if not settings["enabled"] or not settings.get("auto_join", True):
        return
    now = datetime.now(timezone.utc)
    u = upcoming(now)
    if not u or u["id"] in JOINED:
        return
    start = datetime.fromisoformat(u["start"])
    if not start - JOIN_BEFORE <= now <= start + JOIN_LATE:
        return
    if s and not s.finished and s.confirmed:
        return
    if time.time() - _join_asked.get(u["id"], 0) < 20 or idle_seconds() > 300:
        return
    _join_asked[u["id"]] = time.time()
    ev = cal.event_by_id(DATA, u["id"])
    if ev is None or abs((cal.parse_ts(ev["start"]) - start).total_seconds()) > 60:
        _mark_joined(u["id"], "skipped")
        log(f"meeting link not opened, the meeting changed in the calendar: {u['title']}")
        return
    if not ev.get("meet"):
        _mark_joined(u["id"], "nolink")
        return
    subprocess.Popen(["open", ev["meet"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _mark_joined(u["id"], "opened")
    lead = (start - now).total_seconds()
    log(f"opened the meeting {mmss(abs(lead))} {'before' if lead >= 0 else 'after'} the start: {u['title']}")


def next_call() -> dict | None:
    """The prepared call starting soonest (or just started), for the window between calls."""
    now = datetime.now(timezone.utc)
    soon = [(k, v) for k, v in PREP.items() if v.get("status") == "ready" and v.get("plan")
            and now - timedelta(minutes=5) <= v["start"] <= now + PREP_AHEAD]
    if not soon:
        return None
    key, v = min(soon, key=lambda kv: kv[1]["start"])
    ready = next((c.get("items") or [] for c in DAY_PLAN.get("calls") or [] if c.get("event_id") == key), [])
    return {"title": v["title"], "starts_in": round((v["start"] - now).total_seconds()), "ready": ready,
            "type_label": type_label(v["ctx"]["type"]), "brief": v["plan"].get("brief"),
            "links": v["ctx"].get("links") or [], "mode": v["ctx"].get("mode") or "other",
            "stage": v["ctx"].get("stage") or ""}


def _calendar_event(started: datetime, now: datetime) -> dict | None:
    """The calendar call this recording belongs to: overlaps now, nearest start."""
    best = None
    for ev in cal.events(DATA, started - timedelta(hours=3), now + timedelta(hours=3)) or []:
        if not cal.is_call(ev):
            continue
        s, e = cal.parse_ts(ev["start"]), cal.parse_ts(ev["end"])
        if s - timedelta(minutes=15) <= now <= e + timedelta(minutes=20):
            gap = abs((s - started).total_seconds())
            if best is None or gap < best[0]:
                best = (gap, ev)
    return best[1] if best else None


# ---- booking the next meeting: the debrief reads the agreed day and time; this drafts the event (clashes and a
# 30 min buffer checked) and the window's button opens it in your calendar (or an add-on books it). An
# invitation reaches the other side, so it is never sent without your click.

def calendar_clashes(start: datetime, end: datetime) -> list[str]:
    buffer = timedelta(minutes=30)
    out = []
    for ev in cal.events(DATA, start - timedelta(hours=3), end + timedelta(hours=3)) or []:
        s, e = cal.parse_ts(ev["start"]), cal.parse_ts(ev["end"])
        if s < end + buffer and e > start - buffer:
            out.append(f"{ev.get('title') or tr()['busy']}, {s.astimezone(LOCAL_TZ):%H:%M}")
    return out


def _join_names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" {tr()['and']} " + names[-1]


def draft_booking(b: dict, ctx: dict, demo: bool) -> dict:
    t = tr()
    try:
        start = datetime.strptime(f"{b.get('date')} {b.get('time')}", "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)
    except ValueError:
        return {"status": "unclear", "note": t["book_unclear"].format(d=b.get("date"), t=b.get("time"))}
    minutes = max(15, min(120, int(b.get("minutes") or ctx.get("planned_min") or 45)))
    end = start + timedelta(minutes=minutes)
    names = [n for n in (b.get("with") or [ctx.get("who") or t["them_word"]]) if n]
    emails = list(ctx.get("emails") or [])
    draft = {"status": "draft", "demo": demo, "start": start.isoformat(), "end": end.isoformat(), "minutes": minutes,
             "when": f"{day_str(start)} {start:%H:%M}", "title": t["book_title"].format(names=_join_names(names), me=me_first()),
             "description": b.get("topic") or t["book_topic"], "attendees": emails, "with": names,
             "clashes": [] if demo else calendar_clashes(start, end), "note": ""}
    if len(names) > len(emails):
        draft["note"] = t["book_practice"] if demo else t["book_missing"].format(emails=", ".join(emails) or "-")
    return draft


def _ics_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def create_booking(d: dict, folder: Path | None) -> str:
    """An add-on books it (with invitations), else an .ics opened in the Calendar app, where you send it."""
    link = addons.book(d)
    if link:
        return link
    z = lambda v: datetime.fromisoformat(v).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")   # noqa: E731
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Sales Coach//EN", "BEGIN:VEVENT",
             f"UID:sales-coach-{int(time.time())}@localhost", f"DTSTAMP:{z(datetime.now(timezone.utc).isoformat())}",
             f"DTSTART:{z(d['start'])}", f"DTEND:{z(d['end'])}", f"SUMMARY:{_ics_text(d['title'])}",
             f"DESCRIPTION:{_ics_text(d['description'])}"]
    lines += [f"ATTENDEE;RSVP=TRUE:mailto:{e}" for e in d.get("attendees") or []]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    path = (folder if folder and folder.exists() else DATA) / "next_meeting.ics"
    path.write_text("\r\n".join(lines) + "\r\n")
    subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return path.as_uri()


# --------------------------------------------------------------------------- one call

class Session:
    def __init__(self, folder: Path, started: datetime, *, replay: dict | None = None, use_llm: bool = True,
                 event: dict | None = None, whisper: bool = True, feed: list[dict] | None = None,
                 after: str | None = None):
        settings = load_settings()
        self.folder, self.started = folder, started
        # The recording's earlier call hung up and this one took the mic in the same recording: where it begins,
        # and the call before it (its calendar event id, "" for none). None: the recording's own call.
        self.after = after
        self.begun = round((datetime.now(timezone.utc) - started).total_seconds(), 1) \
            if after is not None and not replay else 0.0
        self.replay = replay
        self.how = how_ai() if use_llm else "off"
        self.use_llm = self.how != "off"
        self.forced_event = event
        self.feed = feed   # replay: a finished transcript's lines instead of Whisper
        self.transcribe_from = 0.0 if replay else max(0.0, self.elapsed() - BACKLOG_S)
        self.t_wall0 = time.time()
        self.lock = threading.RLock()
        self.mic, self.sys = Track("mic"), Track("system")
        self.meter = TalkMeter()
        self.phone = not replay and (read_meta(folder) or {}).get("trigger") == "phone"
        self.meter.phone = self.phone
        self.lines: list[dict] = []
        self.transcribed_until = 0.0
        self.default_lang = settings.get("language") or "da"
        self.lang: str | None = None
        self.me = me_first()
        self.ctx: dict = {"title": "Loading...", "type": "other", "type_source": "default", "agenda": [],
                          "planned_min": None, "who": "Them", "prep_sources": [], "attendees": [], "emails": [], "words": []}
        self.plan: dict | None = None
        self.status: dict[str, str] = {}
        self.phase: int | None = None
        self.ai_cues: list[dict] = []   # {"text", "source", "item", "at"}
        self.playbook: list[dict] = []
        self.cue_log: list[dict] = []
        self.notes: list[str] = []
        self.ai_state = "waiting for the plan" if self.use_llm else "off"
        self.mic_free_since: float | None = None
        self.opened = False
        self.last_cue_call = 0.0
        self.sent_t = {"me": -1.0, "them": -1.0}     # per side: the newest line Claude has seen
        self.sent_end = {"me": -1.0, "them": -1.0}   # ...and where it ended
        self.item_notes: dict[str, dict] = {}        # per checklist id: what they said, and what is still missing
        self.claude: ai.Conversation | None = None
        self.claude_turns = 0
        self.cue_busy = False
        self.partial: dict[str, dict] = {}           # the grey live text per side
        self.partials_shown = 0
        self.demo = None
        self.delays: list[float] = []                # end of speech -> line locked in, seconds
        self.finished = False
        self.notice: dict | None = None   # how they were told you record: {"how": "said"|"chat"|"chat_auto", "at": s}
        self.notice_fail = ""             # why the app could not write it in the meeting chat itself
        self.my_notes = ""                # "Tag noter her": your own notes during the call
        self._pauses: dict[float, dict] = {}   # your question's start -> the pause after it (settled ones)
        self.forgotten = False            # "Stop og slet": nothing of this call is kept
        self.transcriber = Transcriber(self) if whisper and feed is None else None
        self.consolidator = Consolidator(self) if self.transcriber else None
        if self.transcriber:
            self.transcriber.start()
            self.consolidator.start()
        threading.Thread(target=self._prepare, daemon=True).start()

    def add_agenda(self, points: list[str]) -> int:
        """Your agenda points, into the checklist: Claude ticks them off from what is said (Danish or English) on
        each turn, as it does its own items, and you can tick them by hand. Without the AI they are yours to tick."""
        added = 0
        with self.lock:
            if self.plan is None:
                self.plan = {"goal": None, "brief": None, "items": []}
            mine = [it for it in self.plan["items"] if it.get("kind") == "agenda"]
            have = {it["label"].lower() for it in mine}
            for point in points:
                if point.lower() in have:
                    continue
                iid = f"d{len(mine) + added + 1}"
                self.plan["items"].append({"id": iid, "label": point, "say": "", "kind": "agenda"})
                self.status[iid] = "open"
                have.add(point.lower())
                added += 1
        return added

    def set_item(self, iid: str, done: bool) -> None:
        """You ticked a point (or took the tick away) yourself."""
        with self.lock:
            if iid in self.status:
                self.status[iid] = "covered" if done else "open"
                note = self.item_notes.setdefault(iid, {"got": "", "missing": ""})
                note["missing"] = ""
                if done:
                    note["by"] = "you"
                else:
                    note.pop("by", None)

    def pauses(self) -> list[dict]:
        """After each of your questions (your line ending in "?"): how long until someone spoke, and who. The
        transcript line finds the spot, the talk meter's 100 ms frames measure it: the end of your question is the
        first half second without your voice from just before the line's end; the pause runs to the next voice."""
        with self.lock:
            qs = [l for l in self.lines if l["who"] == "me" and l["text"].rstrip().endswith("?")]
        me, them = self.meter.me, self.meter.them
        n = min(len(me), len(them))
        keep = {}
        for l in qs:
            if l["t"] in self._pauses:
                keep[l["t"]] = self._pauses[l["t"]]
                continue
            k, quiet = max(0, int((l.get("end") or l["t"]) * 10) - 5), 0
            while k < n and quiet < 5:
                quiet = quiet + 1 if not me[k] else 0
                k += 1
            if quiet < 5:
                continue   # still asking
            q = j = k - 5
            while j + 1 < n and not ((me[j] or them[j]) and (me[j + 1] or them[j + 1])):
                j += 1
            if j + 1 >= n:
                continue   # nobody has said anything yet
            keep[l["t"]] = {"at": round(q / 10, 1), "pause": round((j - q) / 10, 1),
                            "who": "them" if them[j] and not me[j] else "me", "q": l["text"][-90:]}
        self._pauses = keep
        return [keep[t] for t in sorted(keep)]

    @staticmethod
    def pause_stats(ps: list[dict]) -> dict | None:
        if not ps:
            return None
        waits = [p["pause"] for p in ps if p["who"] == "them"]
        return {"questions": len(ps), "answered": len(waits), "talked_on": len(ps) - len(waits),
                "median": round(float(np.median(waits)), 1) if waits else None}

    def whisper_prompt(self) -> str:
        words = (load_settings().get("words") or []) + (self.ctx.get("words") or [])
        return speech_prompt(words, [self.me] + names_from((self.ctx.get("attendees") or []) + (self.ctx.get("emails") or [])))

    def _dispatch(self, kind: str, who: str, t0: float, audio: np.ndarray) -> None:
        if not self.transcriber or t0 + len(audio) / SR < self.transcribe_from:
            return
        if kind == "consolidate":
            if self.transcriber.backend != "cpu":   # on the CPU path the first pass already is this one
                self.consolidator.q.put((who, t0, audio))
        else:
            self.transcriber.put(kind, who, t0, audio)

    def keep_lines(self, who: str, t0: float, length: float, segs: list[tuple]) -> list[dict]:
        """Whisper segments -> transcript lines, minus hallucination phrases, silence on this track, and (your
        mic) the other side leaking from the speakers."""
        own, other = (self.mic, self.sys) if who == "me" else (self.sys, self.mic)
        lines = []
        for start, end, text, no_speech, logprob in segs:
            text = text.strip()
            if not text or HALLUCINATIONS.search(text) or (no_speech > 0.6 and logprob < -1.0):
                continue
            a, b = t0 + start, t0 + min(end, length)
            own_rms, other_rms = own.window_level(a, b), other.window_level(a, b)
            if own_rms < 0.003:
                continue
            if who == "me" and other_rms > 1.5 * own_rms:
                continue
            # phone on speaker: the quieter voice on the mic is the other side (window_level: you = 0.1)
            side = "them" if self.phone and own_rms < PHONE_SPLIT * 0.1 else who
            lines.append({"t": round(a, 1), "end": round(b, 1), "who": side, "text": text})
        return lines

    # ---- clock
    def elapsed(self) -> float:
        if self.replay:
            return self.replay["from"] + (time.time() - self.t_wall0) * self.replay["speed"]
        return (datetime.now(timezone.utc) - self.started).total_seconds()

    def call_elapsed(self) -> float:
        """Minutes into the meeting: from the calendar start when the recording began early. The next call in
        the same recording counts from where it began; the same call picked up again keeps its clock."""
        begun = self.begun if self.after is not None and self.ctx.get("event_id") != (self.after or None) else 0.0
        if not self.replay and self.ctx.get("start"):
            start = datetime.fromisoformat(self.ctx["start"])
            return max(0.0, (datetime.now(timezone.utc) - max(start, self.started + timedelta(seconds=begun)))
                       .total_seconds())
        return self.elapsed() - begun

    @property
    def confirmed(self) -> bool:
        """A real call: in the calendar, a phone call, or the other side has talked for 3 s. A voice message or a
        mic test never gets here, so it gets no window and no Claude."""
        return bool(self.ctx.get("event_id") or self.phone or self.replay or self.meter.seconds()[1] >= 3)

    def note(self, msg: str) -> None:
        log(msg)
        with self.lock:
            self.notes = (self.notes + [msg])[-3:]

    # ---- context + plan, once at the start
    def _prepare(self) -> None:
        ready, event = {}, None
        try:
            event = self.forced_event
            if self.forced_event is not None:
                ctx = build_context(self.forced_event, self.started)
            else:
                # nearest start to where THIS call began: from the recording's start, a call that took the
                # mic after the one before it hung up got that call's title, plan and clock
                event = _calendar_event(self.started + timedelta(seconds=self.begun), datetime.now(timezone.utc))
                ready = PREP.get((event or {}).get("id") or "") or {}
                ctx = ready.get("ctx") or build_context(event, self.started)   # prepared ahead
        except Exception as e:  # noqa: BLE001
            self.note(f"context failed: {e}")
            ctx = dict(self.ctx, title="Unscheduled call")
        with self.lock:
            self.ctx = ctx
            if self.after is not None and ctx.get("event_id") != (self.after or None):
                self.meter.restart_at(int(self.begun * 10), self.mic, self.sys)   # its own talk, as its clock
        try:   # your agenda: pasted for this meeting before the call, else the list in the invite
            ev = event or {}
            points = (load_agendas().get(ev.get("id") or "") or {}).get("points") or agenda_points(ev.get("notes") or "", True)
            if points and self.add_agenda(points):
                log(f"agenda: {len(points)} points")
        except Exception as e:  # noqa: BLE001
            self.note(f"agenda not read: {e}")
        self.playbook = playbook(ctx["type"])
        log(f"call: {ctx['title']} | type {ctx['type']} ({ctx['type_source']}) | prep: {', '.join(ctx['prep_sources']) or 'none'}"
            + (" | brief prepared ahead" if ready.get("plan") else "") + f" | AI {self.how}")
        if not self.use_llm:
            self.ai_state = "off"
            return
        if not self.replay:
            while not self.finished and not self.confirmed:   # no Claude for a voice message
                self.ai_state = "waiting for the other side"
                time.sleep(1)
            if self.finished:
                return
        try:
            plan = ready.get("plan") or make_plan(ctx)
            with self.lock:
                mine = [it for it in (self.plan or {}).get("items", []) if it.get("kind") == "agenda"]
                plan = dict(plan, items=list(plan["items"]) + mine)   # a copy: the prepared plan stays as it was
                self.status = {**{it["id"]: "open" for it in plan["items"]}, **{it["id"]: self.status.get(it["id"], "open") for it in mine}}
                self.plan = plan
                self.ai_state = "on"
            log("plan: " + " | ".join(it["label"] for it in plan["items"]))
        except Exception as e:  # noqa: BLE001
            self.ai_state = f"off: {str(e)[:80]}"
            self.note(f"plan failed: {e}")

    # ---- the loop
    def step(self) -> None:
        meta = read_meta(self.folder)
        segs = (meta or {}).get("segments") or []
        until = self.elapsed() if self.replay else None
        for tr_, who in ((self.mic, "me"), (self.sys, "them")):
            for kind, t0, audio in tr_.sync_segments(self.folder, segs):
                self._dispatch(kind, who, t0, audio)
            if self.replay and self.replay["from"] > 0 and tr_.tail and not getattr(tr_, "skipped", False):
                tr_.skip_to(self.replay["from"])
                tr_.skipped = True
            tr_.pull(until)
            for kind, t0, audio in tr_.take_jobs(live=not self.finished):
                self._dispatch(kind, who, t0, audio)
        if self.feed is not None:
            el = self.elapsed()
            due = [l for l in self.feed if l["t"] <= el]
            self.feed = [l for l in self.feed if l["t"] > el]
            if due:
                self.add_lines(due, done_at=el)
        with self.lock:
            have = [t for t in (self.mic, self.sys) if t.tail]
            upto = min(len(t.rms) for t in have) if have else 0
            if self.replay and self.meter.i == 0 and self.replay["from"] > 0:
                self.meter.i = int(self.replay["from"] * 10)
                pad = np.zeros(self.meter.i, bool)
                self.meter.me, self.meter.them = pad.copy(), pad.copy()
            self.meter.update(self.mic, self.sys, upto)
        if self.use_llm and self.plan and not self.finished and not self.cue_busy:
            new = self._unsent()
            since = (time.time() - self.last_cue_call) * (self.replay or {}).get("speed", 1)
            # a turn as soon as they finish saying something, else at least every CUE_MAX_GAP s
            if new and since >= CUE_MIN_GAP and (any(l["who"] == "them" for l in new) or since >= CUE_MAX_GAP):
                self.last_cue_call = time.time()
                self.cue_busy = True
                threading.Thread(target=self._cue, daemon=True).start()

    def add_lines(self, lines: list[dict], done_at: float, utt: tuple[str, float] | None = None) -> None:
        with self.lock:
            self.lines = sorted(self.lines + lines, key=lambda l: l["t"])
            self.transcribed_until = max(self.transcribed_until, done_at)
            if utt:
                who, t0 = utt
                if self.partial.get(who, {}).get("t0", -1) <= t0:
                    self.partial.pop(who, None)
                self.delays = (self.delays + [max(0.0, self.elapsed() - done_at)])[-20:]
        for l in lines:
            log(f"  {mmss(l['t'])} {self._name(l['who'])}: {l['text']}")

    def _name(self, who: str) -> str:
        return self.me if who == "me" else (self.ctx.get("who") or "Them")

    def replace_lines(self, who: str, a: float, b: float, lines: list[dict]) -> None:
        """The 30 s second pass: its lines replace that side's quick lines in [a, b)."""
        with self.lock:
            keep = [l for l in self.lines
                    if not ((l["who"] == who or self.phone) and a <= (l["t"] + l.get("end", l["t"])) / 2 < b)]
            self.lines = sorted(keep + lines, key=lambda l: l["t"])
            for l in lines:   # Claude has already seen these words in their quick form
                w = l["who"]
                l["sent"] = l["t"] <= self.sent_t[w] or l.get("end", l["t"]) <= self.sent_end[w] + 0.3

    def _unsent(self) -> list[dict]:
        return [l for l in self.lines if not l.get("sent") and l["t"] > self.sent_t[l["who"]]]

    def set_partial(self, who: str, t0: float, text: str) -> None:
        """The grey live line, only while that utterance is still open (a locked-in one replaces it)."""
        track = self.mic if who == "me" else self.sys
        with self.lock:
            if text and track.utt_start == t0:
                self.partial[who] = {"t0": t0, "text": text, "at": self.elapsed()}
                self.partials_shown += 1

    def _transcript_text(self, cap: int = 14000) -> str:
        return "\n".join(f"[{mmss(l['t'])}] {self._name(l['who'])}: {l['text']}" for l in self.lines)[-cap:]

    def _cue(self) -> None:
        try:
            self._cue_call()
        finally:
            self.cue_busy = False

    def _cue_call(self) -> None:
        with self.lock:
            plan, status = self.plan, dict(self.status)
            notes = {k: dict(v) for k, v in self.item_notes.items()}
            el = self.call_elapsed()
            agenda = self.ctx.get("agenda") or []
            new = self._unsent()
            cooling = {c["item"]: c["at"] for c in self.cue_log if c.get("item") and c["at"] >= el - ITEM_COOLDOWN}
        planned_phase = self._planned_phase(el)

        def _state(iid: str) -> str:
            miss = (notes.get(iid) or {}).get("missing")
            return status.get(iid, "open") + (f", missing: {miss}" if status.get(iid) == "partly" and miss else "")
        items = "\n".join(f"- {it['id']} [{_state(it['id'])}] {it['label']}: {it['say']}" for it in plan["items"])
        clock = (f"Time: {mmss(el)} into a {self.ctx.get('planned_min') or '?'} min call. By the plan they should be in "
                 f"agenda section {planned_phase if planned_phase is not None else '?'}.\n\nCHECKLIST NOW:\n{items}\n\n"
                 "COOLING DOWN (cued in the last minutes, do not cue again):\n"
                 + ("\n".join(f"- {k} (cued at {mmss(v)})" for k, v in cooling.items()) or "- none"))
        lines = "\n".join(f"[{mmss(l['t'])}] {self._name(l['who'])}: {l['text']}" for l in new)
        if self.claude and self.claude_turns >= CUE_SESSION_TURNS:
            # each turn re-reads the whole conversation, so a long one gets slow and costly
            self.claude.close()
            self.claude = None
        fresh = self.claude is None
        if fresh:
            ag = "\n".join(f"{i}. {n} ({m} min)" for i, (n, m) in enumerate(agenda)) or "no agenda"
            recap = self._transcript_text(cap=6000 if self.claude_turns else 14000)
            user = (f"Call: {self.ctx['title']} ({type_label(self.ctx['type'])})\nGoal: {plan.get('goal', '')}\n\n"
                    f"AGENDA:\n{ag}\n\n{clock}\n\nTRANSCRIPT {'SO FAR' if not self.claude_turns else 'OF THE LAST MINUTES'}:\n{recap}")
        else:
            user = f"{clock}\n\nNEW LINES:\n{lines}"
        try:
            if fresh:
                self.claude = ai.Conversation(_fill(P.CUE_SYSTEM) + playbook_text(self.playbook) + extra_rules("cues")
                                              + feedback_block(), P.CUE_SCHEMA, self.how, DATA,
                                              effort=os.environ.get("SALES_COACH_CUE_EFFORT") or None)
                self.claude_turns = 0
            t = time.time()
            out = self.claude.ask(user)
            took = time.time() - t
            self.claude_turns += 1
        except Exception as e:  # noqa: BLE001
            if self.claude:
                self.claude.close()
            self.claude = None   # the next turn starts a new conversation with the whole transcript
            with self.lock:
                self.ai_state = f"retrying: {str(e)[:80]}"
            self.note(f"cue turn failed: {e}")
            return
        with self.lock:
            for l in new:
                self.sent_t[l["who"]] = max(self.sent_t[l["who"]], l["t"])
                self.sent_end[l["who"]] = max(self.sent_end[l["who"]], l.get("end", l["t"]))
            rank = {"open": 0, "partly": 1, "covered": 2}
            for it in out.get("items") or []:   # only forward: one garbled minute must not untick an item
                iid = it.get("id")
                if iid not in self.status or rank.get(it.get("status"), 0) < rank[self.status[iid]]:
                    continue
                self.status[iid] = it["status"]
                note = self.item_notes.setdefault(iid, {"got": "", "missing": ""})
                if (it.get("got") or "").strip():
                    note["got"] = it["got"].strip()
                if it["status"] != "partly":
                    note["missing"] = ""
                elif (it.get("missing") or "").strip():
                    note["missing"] = it["missing"].strip()
            self.phase = out.get("phase")
            # The model rephrases the same ask every minute, so the cue rules are kept here, not left to the
            # prompt: a cooldown per item, at most twice a call (once on a client or team meeting), never a
            # repeat, no booking push off a sales call, and one new cue at a time.
            sales = self.ctx.get("type") in sales_kinds()
            given = Counter(c["item"] for c in self.cue_log if c.get("item"))
            said = {re.sub(r"\W+", " ", c["text"].lower()).strip() for c in self.cue_log}
            last_at = max((c["at"] for c in self.cue_log), default=-10**6)

            def allowed(c: dict) -> bool:
                text, item = c.get("text", "").strip(), c.get("item") or ""
                if not text or re.sub(r"\W+", " ", text.lower()).strip() in said:
                    return False
                if item and (item in cooling or given[item] >= (ITEM_CUES_MAX if sales else 1)
                             or self.status.get(item) == "covered"):
                    return False
                if not sales and re.match(r"\W*book\b", text.lower()):
                    return False
                return True
            fresh_cues = [c for c in out.get("cues") or [] if allowed(c)] if el - last_at >= (CUE_EVERY if sales else CUE_EVERY_OTHER) else []
            new_cues = [{"text": c["text"].strip(), "source": (c.get("source") or "").strip(),
                         "item": c.get("item") or "", "at": round(el)} for c in fresh_cues[:1]]
            # the newest two stay up, so a cue doesn't vanish while you read it; one goes once its item is covered
            # or after 2 minutes
            keep = [c for c in self.ai_cues if self.status.get(c["item"]) != "covered" and el - c["at"] < 120
                    and not any(n["item"] and n["item"] == c["item"] for n in new_cues)]
            self.ai_cues = (new_cues + keep)[:2]
            self.ai_state = "on"
            self.cue_log += [dict(c) for c in new_cues]
        log(f"turn @ {mmss(el)} ({took:.1f}s, {len(new)} new line(s)): "
            f"{[c['text'] + (' (' + c['source'] + ')' if c['source'] else '') for c in self.ai_cues]} | phase {self.phase} | "
            + " ".join(f"{k}={v[0]}" for k, v in self.status.items()))

    def _planned_phase(self, el: float) -> int | None:
        agenda = self.ctx.get("agenda") or []
        acc = 0
        for i, (_, m) in enumerate(agenda):
            acc += m * 60
            if el < acc:
                return i
        return len(agenda) - 1 if agenda else None

    def _countdown(self) -> dict | None:
        """Time left to this call's end on the calendar (a late start buys no time), and the next meeting. A call
        off the calendar counts down to the next meeting."""
        if self.replay:
            return None
        ctx, now = self.ctx, datetime.now(timezone.utc)
        end = None
        try:
            if ctx.get("end"):
                end = datetime.fromisoformat(ctx["end"])
            elif ctx.get("start") and ctx.get("planned_min"):
                end = datetime.fromisoformat(ctx["start"]) + timedelta(minutes=ctx["planned_min"])
        except ValueError:
            end = None
        nxt = next_meeting(now, exclude=ctx.get("event_id"))
        near = nxt and nxt["start_dt"] - now <= timedelta(hours=3)
        target_dt = end or (nxt["start_dt"] if near else None)
        if not target_dt:
            return None
        total = (end - datetime.fromisoformat(ctx["start"])).total_seconds() if end and ctx.get("start") else None
        out = {"left_s": round((target_dt - now).total_seconds()), "total_s": round(total) if total else None,
               "to": "end" if end else "next"}
        if near:
            out["next"] = {k: v for k, v in nxt.items() if k != "start_dt"}
            out["next"]["in_s"] = round((nxt["start_dt"] - now).total_seconds())
        return out

    # ---- what the window shows
    def snapshot(self) -> dict:
        t = tr()
        with self.lock:
            ctx = self.ctx
            ct = CALL_TYPES.get(ctx["type"], CALL_TYPES["other"])
            tgt = target(ctx["type"])
            el = self.call_elapsed()
            planned = (ctx.get("planned_min") or 0) * 60
            share = self.meter.share()
            share5 = self.meter.share(3000)
            stretch = self.meter.my_stretch()
            cues = []
            # the computed cues first: they are about right now
            if ct["stretch"] and stretch >= ct["stretch"]:
                cues.append({"kind": "talk", "text": t["stretch_team" if ctx["type"] == "internal" else "stretch"].format(t=mmss(stretch))})
            elif tgt and share5 is not None and sum(self.meter.seconds(3000)) >= 90 and share5 > tgt[1] + 10:
                cues.append({"kind": "talk", "text": t["share5"].format(p=share5)})
            elif tgt and share5 is not None and sum(self.meter.seconds(3000)) >= 90 and share5 < tgt[0] - 15:
                cues.append({"kind": "talk", "text": t["share5_low"].format(p=share5)})
            ps = self.pauses()
            last3 = ps[-3:]
            # you answered your own question twice in the last three, the latest within the last minute
            if len(last3) >= 2 and sum(p["who"] == "me" for p in last3) >= 2 and last3[-1]["who"] == "me" \
                    and self.elapsed() - last3[-1]["at"] <= 60:
                cues.append({"kind": "talk", "text": t["pause"]})
            open_items = [it for it in (self.plan or {}).get("items", []) if self.status.get(it["id"]) != "covered"]
            cd = self._countdown()
            if cd and cd.get("total_s"):   # the calendar's clock, not the minutes since you joined
                planned, el = cd["total_s"], cd["total_s"] - cd["left_s"]
            if planned:
                left = planned - el
                if left < 0:
                    cues.append({"kind": "time", "text": t["over"].format(t=mmss(-left))})
                elif left <= 300:
                    cues.append({"kind": "time", "text": t["left_book" if ctx["type"] in sales_kinds() else "left"].format(t=mmss(left))})
                elif el >= 0.75 * planned and len(open_items) >= 3:
                    cues.append({"kind": "time", "text": t["open_items"].format(n=len(open_items), t=mmss(left))})
            cues += [{"kind": "ai", **c} for c in self.ai_cues]
            who = ctx.get("who") if ctx.get("who") not in (None, "", "Them") else t["them"]
            src = ctx["type_source"]
            title = {"Unscheduled call": t["untitled"], "Loading...": t["loading"]}.get(ctx["title"], ctx["title"])
            return {
                "active": not self.finished, "call_id": self.folder.name, "confirmed": self.confirmed, "demo": False,
                "title": title, "type": ctx["type"], "type_label": type_label(ctx["type"]),
                "type_source": t["src"].get(src, src), "types": {k: type_label(k) for k in CALL_TYPES},
                "target": tgt, "elapsed": round(self.call_elapsed()), "planned": planned or None, "countdown": cd,
                "share": share, "share5": share5, "stretch": round(stretch),
                "my_longest": round(self.meter.my_longest / 10), "their_longest": round(self.meter.their_longest / 10),
                "talk_s": [round(x) for x in self.meter.seconds()], "cues": cues[:3],
                "goal": (self.plan or {}).get("goal"),
                "next_items": [{"label": it["label"], "say": it["say"], "status": self.status.get(it["id"], "open"),
                                **self.item_notes.get(it["id"], {})}
                               for it in (self.plan or {}).get("items", []) if self.status.get(it["id"]) != "covered"][:2],
                "brief": (self.plan or {}).get("brief"), "links": ctx.get("links") or [],
                "mode": ctx.get("mode") or "other", "stage": ctx.get("stage") or "",
                "items": [{**it, "status": self.status.get(it["id"], "open"), **self.item_notes.get(it["id"], {})}
                          for it in (self.plan or {}).get("items", [])],
                "agenda": [{"name": n, "min": m} for n, m in ctx.get("agenda") or []], "phase": self.phase,
                "planned_phase": self._planned_phase(el),
                "lines": [{"t": mmss(l["t"]), "who": t["me"] if l["who"] == "me" else who, "text": l["text"]}
                          for l in self.lines[-6:]],
                "live": [{"t": mmss(p["t0"]), "who": t["me"] if w == "me" else who, "text": p["text"]}
                         for w, p in sorted(self.partial.items(), key=lambda kv: kv[1]["t0"])],
                "lag": round(float(np.median(self.delays)), 1) if self.delays else None,
                "speaking": [w for w, tr_ in (("me", self.mic), ("them", self.sys)) if tr_.utt_start is not None],
                "ai": self.ai_state, "prep_sources": ctx.get("prep_sources") or [], "notes": self.notes,
                "replay": bool(self.replay), "pause": self.pause_stats(ps), "my_notes": self.my_notes,
                "notice": None if self.notice or not load_settings().get("notice", True) or self.replay
                          else {"text": notice_text(self.how != "off"), "chat": not self.phone,
                                "auto": not self.phone and load_settings().get("notice_auto", True), "fail": self.notice_fail},
            }

    def set_type(self, kind: str) -> None:
        if kind in CALL_TYPES:
            with self.lock:
                self.ctx = dict(self.ctx, type=kind, type_source="set by you")
            log(f"call type set to {kind}")

    # ---- the end
    def finish(self, notes: bool | None = None) -> dict:
        """Stop, save the talk stats, and (live) write the call notes from the live transcript."""
        notes = (not self.replay) if notes is None else notes
        # the recorder deletes calls under 90 s (a mic test, a meeting left at once); "Stop og slet" deletes it too
        alive = self.folder.exists() and not self.forgotten
        notes = notes and alive and self.transcriber is not None
        with self.lock:
            self.finished = True
            snap = self.snapshot()
        for tr_, who in ((self.mic, "me"), (self.sys, "them")):
            for kind, t0, audio in tr_.flush():
                self._dispatch(kind, who, t0, audio)
        if self.transcriber:
            self.transcriber.stop()   # it finishes the locked-in lines still queued, then exits
            self.consolidator.q.put(None)
        summary = {
            "demo": None, "std": self.ctx.get("std") or [], "links": self.ctx.get("links") or [],
            "title": snap["title"], "started": self.started.isoformat(), "duration_s": snap["elapsed"],
            "type": snap["type"], "type_source": snap["type_source"], "target": snap["target"],
            "share": snap["share"], "talk_s": snap["talk_s"], "my_longest_s": snap["my_longest"],
            "their_longest_s": snap["their_longest"],
            "items": [{"label": i["label"], "status": i["status"], "got": i.get("got") or "", "missing": i.get("missing") or ""}
                      for i in snap["items"]],
            "cues": self.cue_log, "prep_sources": snap["prep_sources"], "folder": self.folder.name,
            "end_planned": self.ctx.get("end"), "ended_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "plan": self.plan, "agenda": self.ctx.get("agenda") or [], "who": self.ctx.get("who"),
            "planned_min": self.ctx.get("planned_min"), "emails": self.ctx.get("emails") or [],
            "attendees": self.ctx.get("attendees") or [], "version": VERSION, "notice": self.notice,
            "pause": snap.get("pause"), "pauses": self.pauses(), "my_notes": self.my_notes,
        }
        if not self.replay and alive:
            try:
                (self.folder / "copilot.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
            except OSError as e:
                log(f"copilot.json not written: {e}")
        log(f"call done: you {snap['share']}% (target {snap['target']}), longest stretch you "
            f"{mmss(snap['my_longest'])} / them {mmss(snap['their_longest'])}, "
            f"{sum(1 for i in snap['items'] if i['status'] == 'covered')}/{len(snap['items'])} covered")
        if notes:
            summary["notes_status"] = "writing"
            self.notes_thread = threading.Thread(target=self._write_notes, args=(summary, time.time()), daemon=True)
            self.notes_thread.start()
        if self.use_llm and self.plan and len(self.lines) >= 6 and (alive or self.replay):
            summary["debrief_status"] = "writing"
            threading.Thread(target=self._debrief, args=(summary, snap, time.time()), daemon=True).start()
        elif self.claude:
            self.claude.close()
            self.claude = None
        return summary

    def _debrief(self, summary: dict, snap: dict, hung_up: float) -> None:
        """The coach's verdict within ~10 s of hang-up. Asked in the call's own conversation, which has heard
        everything, so only the last lines and the numbers go in; a fresh call with the whole transcript when
        there is none."""
        t = tr()
        try:
            if self.transcriber:
                self.transcriber.join(timeout=6)   # the last sentence, locked in
            items = ", ".join(f"{i['label']}={i['status']}" for i in snap["items"])
            cues = "; ".join(f"{mmss(c['at'])} {c['text']}" for c in self.cue_log[-12:]) or "none"
            # only numbers that were measured: an empty meter (no audio) read as "talked None%, longest 0:00" and
            # got the verdict "almost only you talked" on a call that was 57/43
            stats = (f"{self.me} talked {snap['share']}% of the time (target {snap['target']}); their longest stretch "
                     f"{mmss(snap['my_longest'])}, the other side's longest {mmss(snap['their_longest'])}. "
                     if snap["share"] is not None else "Talk share was not measured on this call (no audio); judge it "
                     "from the transcript only. ")
            stats += f"Checklist at the end: {items}. Cues shown during the call: {cues}."
            pst = snap.get("pause")
            if pst:
                stats += (f" After {self.me}'s {pst['questions']} questions they let the other side answer {pst['answered']} times"
                          + (f" (median wait {pst['median']} s before the answer)" if pst["median"] is not None else "")
                          + f" and talked on themselves {pst['talked_on']} times.")
            new = self._unsent()
            lines = "\n".join(f"[{mmss(l['t'])}] {self._name(l['who'])}: {l['text']}" for l in new)
            std = self.ctx.get("std") or []
            bank = ("\n\nSTANDARD QUESTIONS (id | short | question | on file before this call):\n"
                    + "\n".join(f"- {q['id']} | {q['short']} | {q['da']} | {q.get('on_file') or 'nothing'}" for q in std)) if std else ""
            now = datetime.now(LOCAL_TZ)
            stats += bank + f"\n\nTODAY: {now:%A %Y-%m-%d}, {now:%H:%M} local time." + call_kind_note(self.ctx["type"])
            msg = f"THE CALL HAS ENDED. {stats}\n\nLAST LINES:\n{lines or '(none)'}\n\nFill debrief now."
            out = None
            if self.claude:
                try:
                    out = self.claude.ask(msg, timeout=40)
                except Exception as e:  # noqa: BLE001
                    log(f"debrief in the conversation failed ({e}); a fresh call with the whole transcript")
            if not out or not out.get("debrief"):
                out = ask_json(_fill(P.CUE_SYSTEM) + playbook_text(self.playbook) + extra_rules("cues") + feedback_block(),
                               f"Call: {self.ctx['title']} ({type_label(self.ctx['type'])})\nGoal: {(self.plan or {}).get('goal', '')}"
                               f"\n\nTRANSCRIPT:\n{self._transcript_text(30000)}\n\nTHE CALL HAS ENDED. {stats}\n\nFill debrief now.",
                               P.CUE_SCHEMA)
            debrief = tidy_debrief(out.get("debrief") or {}, (self.plan or {}).get("items") or [], std)
            if (debrief.get("booking") or {}).get("agreed"):
                summary["booking"] = draft_booking(debrief["booking"], self.ctx, demo=False)
            took = round(time.time() - hung_up)
            summary.update({"debrief": debrief, "debrief_status": "ready" if debrief else "none", "debrief_after_s": took})
            log(f"debrief ready {took} s after hang-up: {debrief.get('verdict', '')[:120]}")
            if not self.replay and self.folder.exists():
                short = {q["id"]: q["short"] for q in std}
                md = [f"# {t['result']}: {self.ctx['title']}", "", debrief.get("verdict", ""), ""]
                if std:
                    md += [f"## {t['std_this']}"] + ([f"- {short.get(a['id'], a['id'])}: {a['answer']}"
                                                      + (f" ({t['vague']})" if a.get("status") == "vague" else "")
                                                      for a in debrief.get("answers") or []] or [f"- {t['none']}"]) + [""]
                md += [f"## {t['next_steps']}"] + [f"- {s}" for s in debrief.get("next_steps") or []]
                md += ["", f"## {t['ask_next']}"] + [f"- {next((q['da'] for q in std if q['id'] == i), i)}" for i in debrief.get("ask_next") or []]
                md += ["", f"## {t['coach']}"] + [f"- {m['at']}: \"{m['say']}\" ({m['expert']})" for m in debrief.get("moments") or []]
                md += ["", f"{t['practise']}: " + debrief.get("practise", "")]
                (self.folder / "debrief.md").write_text("\n".join(md) + "\n")
                (self.folder / "copilot.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
        except Exception as e:  # noqa: BLE001
            summary["debrief_status"] = f"failed: {str(e)[:120]}"
            log(f"debrief failed: {e}")
        finally:
            if self.claude:
                self.claude.close()
                self.claude = None

    def _write_notes(self, summary: dict, hung_up: float) -> None:
        """Notes about a minute after hang-up: transcript.md with a summary, next steps, decisions and asks on
        top. The live transcript is already done at hang-up, so nothing waits for a full re-run."""
        t = tr()
        try:
            self.transcriber.join(timeout=600)
            with self.lock:
                lines = sorted(self.lines, key=lambda l: l["t"])
            if not lines or self.transcriber.model is None:
                summary["notes_status"] = "none: no live transcript"
                log(summary["notes_status"])
                return
            ctx = self.ctx
            others = ctx.get("attendees") or []
            other_label = others[0] if len(others) == 1 else (ctx.get("who") if ctx.get("who") != "Them" else "Other side")
            duration = self.elapsed()
            complete = self.transcribe_from <= 30 and not self.transcriber.is_alive()
            ended = self.started + timedelta(seconds=duration)
            text_lines = [f"[{stamp(l['t'])}] {self.me if l['who'] == 'me' else other_label}: {l['text']}" for l in lines]
            labels = f"from the tracks (your mic = {self.me}, call audio = {other_label})"
            if not complete:
                labels += f". The live transcript starts at {stamp(self.transcribe_from)} (the coach started late)"
            emails = ctx.get("emails") or []
            header = [
                f"# {ctx['title']}", "",
                f"- When: {self.started.astimezone(LOCAL_TZ):%Y-%m-%d %H:%M}-{ended.astimezone(LOCAL_TZ):%H:%M}",
                f"- Calendar: {ctx['title'] if ctx.get('event_id') else 'no matching event'}",
                f"- Attendees: {', '.join(others) or 'unknown'}",
                f"- Attendee emails: {', '.join(emails) or '-'}",
                f"- Source: Sales Coach {VERSION} live transcript, Whisper large-v3, {'English' if self.lang == 'en' else 'Danish' if (self.lang or self.default_lang) == 'da' else self.lang}.",
                f"- Speaker labels: {labels}", "",
            ]
            words = (load_settings().get("words") or []) + (ctx.get("words") or [])
            notes = summarize(text_lines, ctx["title"], others, words, self.my_notes) if self.use_llm else None
            body = ([notes.strip(), "", "## Transcript", ""] if notes else ["## Transcript", ""]) + text_lines
            (self.folder / "transcript.md").write_text("\n".join(header + body) + "\n")
            (self.folder / "turns.json").write_text(json.dumps(
                [{"start": l["t"], "end": l.get("end", l["t"]), "track": "mic" if l["who"] == "me" else "system",
                  "text": l["text"]} for l in lines], ensure_ascii=False, indent=1))
            took = round(time.time() - hung_up)
            summary.update({"notes": notes or "", "notes_status": "ready", "notes_after_s": took,
                            "notes_file": str(self.folder / "transcript.md")})
            log(f"notes ready {took} s after hang-up: {self.folder / 'transcript.md'} "
                f"({len(text_lines)} lines, {'complete' if complete else 'partial'})")
            if not self.replay:
                notify(t["notes_ready"], t["notes_after"].format(title=ctx["title"][:60], s=took))
                addons.after_call(self.folder, summary)
        except Exception as e:  # noqa: BLE001
            summary["notes_status"] = f"failed: {str(e)[:120]}"
            log(f"notes failed: {e}")


# ---- hang-up detection: the recorder stops 30 s after the call app lets go of the mic; the result should be
# there within ~10 s, so the coach asks Core Audio itself which apps hold the mic.
MEETING_APPS = ("com.google.chrome", "com.apple.safari", "com.apple.webkit", "company.thebrowser", "com.brave.browser",
                "com.microsoft.edgemac", "org.mozilla.firefox", "com.vivaldi.vivaldi", "com.operasoftware.opera",
                "us.zoom.xos", "com.microsoft.teams", "com.tinyspeck.slackmacgap", "com.apple.facetime",
                "net.whatsapp.whatsapp", "desktop.whatsapp", "com.hnc.discord", "com.cisco.webex", "com.webex")
_ca = None


def _fourcc(s: str) -> int:
    return int.from_bytes(s.encode(), "big")


def call_app_holds_mic() -> bool | None:
    """True while a call app (browser, Zoom, Teams...) has the mic open; None if Core Audio can't be asked."""
    global _ca
    import ctypes
    try:
        if _ca is None:
            ca = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
            cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
            cf.CFRelease.argtypes = [ctypes.c_void_p]
            _ca = (ca, cf)
        ca, cf = _ca

        class Addr(ctypes.Structure):
            _fields_ = [("sel", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("elem", ctypes.c_uint32)]

        def addr(sel: str) -> Addr:
            return Addr(_fourcc(sel), _fourcc("glob"), 0)

        a = addr("prs#")
        size = ctypes.c_uint32(0)
        if ca.AudioObjectGetPropertyDataSize(1, ctypes.byref(a), 0, None, ctypes.byref(size)) != 0:
            return None
        ids = (ctypes.c_uint32 * (size.value // 4))()
        if ca.AudioObjectGetPropertyData(1, ctypes.byref(a), 0, None, ctypes.byref(size), ids) != 0:
            return None
        for obj in ids:
            running, n = ctypes.c_uint32(0), ctypes.c_uint32(4)
            ar = addr("piri")
            if ca.AudioObjectGetPropertyData(obj, ctypes.byref(ar), 0, None, ctypes.byref(n), ctypes.byref(running)) or not running.value:
                continue
            ref, n = ctypes.c_void_p(), ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
            ab = addr("pbid")
            if ca.AudioObjectGetPropertyData(obj, ctypes.byref(ab), 0, None, ctypes.byref(n), ctypes.byref(ref)) or not ref.value:
                continue
            buf = ctypes.create_string_buffer(512)
            ok = cf.CFStringGetCString(ref, buf, 512, 0x08000100)
            cf.CFRelease(ref)
            if ok and buf.value.decode(errors="ignore").lower().startswith(MEETING_APPS):
                return True
        return False
    except Exception:  # noqa: BLE001
        return None


def active_recording() -> tuple[Path, datetime] | None:
    """The folder the recorder is writing right now, if any."""
    best = None
    for meta_file in ROOT.glob("*/meta.json"):
        meta = read_meta(meta_file.parent)
        if not meta or meta.get("status") != "recording":
            continue
        try:
            os.kill(int(meta.get("pid") or 0), 0)
        except (OSError, ValueError):
            continue
        started = datetime.fromisoformat(meta["started"].replace("Z", "+00:00"))
        if best is None or started > best[1]:
            best = (meta_file.parent, started)
    return best


def idle_seconds() -> float:
    """Seconds since the last key or mouse move (IOHIDSystem's HIDIdleTime); 0 when unknown."""
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r'"HIDIdleTime" = (\d+)', out)
        return int(m[1]) / 1e9 if m else 0.0
    except (OSError, subprocess.SubprocessError):
        return 0.0


def notify(title: str, text: str) -> None:
    if os.environ.get("SALES_COACH_QUIET"):   # tests
        return
    script = f'display notification {json.dumps(text)} with title {json.dumps(title)}'
    subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------------- the window

WIDGET_APP = Path(os.environ.get("SALES_COACH_APP") or Path.home() / "Applications" / "Sales Coach.app")
FILES: dict[str, str] = {}   # token -> a local PDF an add-on's link may open (served at /file/<token>)


class App:
    def __init__(self):
        self.session: Session | None = None
        self.last: dict | None = None
        self.last_poll = 0.0
        self.last_agent = ""
        self.ended_event = ""                   # the ended call's calendar event, for the next one in its recording
        self.ended_folder: Path | None = None   # hung up, while the recorder still finishes that recording
        self.forgotten_at = 0.0                 # "Stop og slet": the window says it is gone

    def window_open(self) -> bool:
        """The window asks for /state every second while it is open."""
        return time.time() - self.last_poll < 5

    def state(self) -> dict:
        s = self.session
        out = s.snapshot() if s else {"active": False, "last": self.last, "next": next_call(),
                                      "readiness": DAY_PLAN.get("calls") or [], "briefs": briefs(), "upcoming": upcoming()}
        settings = load_settings()
        out["settings"] = settings
        out["auto_open_labels"] = tr()["auto_open"]
        out["me"] = {"name": settings.get("name") or "", "initials": "".join(p[0] for p in (settings.get("name") or "").split()[:2]).upper()}
        out["ai_backend"] = how_ai()
        out["version"] = VERSION
        out["forgotten"] = time.time() - self.forgotten_at < 30
        out["phone_on"] = PHONE_FLAG.exists()
        out.setdefault("types", {k: type_label(k) for k in CALL_TYPES})   # names for the call list, also between calls
        return out


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json")

        def do_GET(self):
            path = self.path.split("?")[0]
            q = parse_qs(urlparse(self.path).query)
            if path == "/state":
                # the menu bar item and a hidden window poll too; neither is a window someone can see
                if "from=menubar" not in self.path and "hidden=1" not in self.path:
                    app.last_poll = time.time()
                    app.last_agent = self.headers.get("User-Agent", "")
                self._json(app.state())
            elif path == "/growth":
                self._json(growth())
            elif path == "/calls":
                self._json(calls_list())
            elif path == "/bundle":   # one call as text, for a Claude chat
                text = bundle_text((q.get("id") or [""])[0])
                self._send(200 if text else 404, (text or "not found").encode(), "text/plain; charset=utf-8")
            elif path == "/call":
                c = call_detail((q.get("id") or [""])[0])
                self._json(c or {}, 200 if c else 404)
            elif path == "/transcript":
                d = _call_dir((q.get("id") or [""])[0])
                if d and (d / "transcript.md").is_file():
                    self._send(200, (d / "transcript.md").read_bytes(), "text/plain; charset=utf-8")
                else:
                    self._send(404, b"not found", "text/plain")
            elif path == "/health":   # for checks: does not count as a window
                self._json({"window_open": app.window_open(), "viewer": app.last_agent[:120], "call": bool(app.session),
                            "settings": load_settings(), "ai": how_ai(), "version": VERSION})
            elif path.startswith("/file/"):
                f = FILES.get(path.split("/file/", 1)[1])
                if f and Path(f).exists():
                    self._send(200, Path(f).read_bytes(), "application/pdf")
                else:
                    self._send(404, b"not found", "text/plain")
            elif path in ("/", "/index.html"):
                self._send(200, HUD_FILE.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            if self.path.startswith("/type"):
                if app.session:
                    app.session.set_type(body.get("type", ""))
                self._json({})
            elif self.path.startswith("/note"):   # the Sales Coach app's own events, into this log
                log(f"app: {str(body.get('msg') or '')[:200]}")
                self._json({})
            elif self.path.startswith("/phone"):
                # the phone call on speaker has its own button at the bottom of the window, not only in the menu
                if body.get("on"):
                    PHONE_FLAG.touch()
                else:
                    PHONE_FLAG.unlink(missing_ok=True)
                log(f"phone call {'started' if body.get('on') else 'stopped'} from the window")
                self._send(200, json.dumps({"phone_on": PHONE_FLAG.exists()}).encode(), "application/json")
            elif self.path.startswith("/settings"):
                self._json(save_settings(body))
            elif urlparse(self.path).path == "/notice":   # they were told: you said it, pasted it, or the app wrote it
                s = app.session
                if s and body.get("how") in ("said", "chat", "chat_auto"):
                    s.notice = {"how": body["how"], "at": round(s.call_elapsed())}
                    log(f"recording notice: {body['how']} at {mmss(s.call_elapsed())}")
                self._json({})
            elif self.path.startswith("/notice_fail"):   # the app could not write in the chat: why, for the card
                if app.session:
                    app.session.notice_fail = str(body.get("why") or "")[:300]
                    log(f"recording notice not written in the chat: {app.session.notice_fail}")
                self._json({})
            elif self.path.startswith("/agenda"):   # your agenda: for the call going on, or a coming meeting
                points = agenda_points(str(body.get("text") or ""))
                ev = str(body.get("event_id") or "")
                if ev:
                    save_agenda(ev, points)
                    self._json({"saved": len(points)})
                elif app.session:
                    self._json({"added": app.session.add_agenda(points)})
                else:
                    self._json({"error": "no call and no meeting"}, 400)
            elif self.path.startswith("/mynotes"):   # "Tag noter her": during the call, or on a call's page after it
                text = str(body.get("text") or "")[:20000]
                s_ = app.session
                d = _call_dir(str(body.get("call") or "")) if body.get("call") else None
                if s_ and str(body.get("call") or s_.folder.name) == s_.folder.name:   # the call going on
                    s_.my_notes = text
                    self._json({"saved": True})
                elif d:
                    try:
                        c = json.loads((d / "copilot.json").read_text())
                        c["my_notes"] = text
                        (d / "copilot.json").write_text(json.dumps(c, ensure_ascii=False, indent=1, default=str))
                        if app.last and app.last.get("folder") == d.name:
                            app.last["my_notes"] = text
                        self._json({"saved": True})
                    except (OSError, ValueError):
                        self._json({"error": "no such call"}, 400)
                else:
                    self._json({"error": "no call"}, 400)
            elif self.path.startswith("/item"):   # you ticked a point yourself
                if app.session:
                    app.session.set_item(str(body.get("id") or ""), bool(body.get("done")))
                self._json({})
            elif self.path.startswith("/forget"):   # "Stop og slet": the call going on now, nothing kept
                s = app.session
                if s:
                    s.forgotten = True
                    s.use_llm, s.ai_state = False, "off"
                    app.forgotten_at = time.time()
                    threading.Thread(target=forget_call, args=(s.folder,), daemon=True).start()
                self._json({})
            elif self.path.startswith("/delete_call"):   # one past call, from its page
                d = _call_dir(str(body.get("call") or ""))
                if d and not (app.session and app.session.folder == d):
                    forget_call(d)
                    if app.last and app.last.get("folder") == d.name:
                        app.last = last_summary()
                    self._json({})
                else:
                    self._json({"error": "no such call"}, 400)
            elif self.path.startswith("/book"):
                # the window's button: the one click that books it (never on its own)
                b = (app.last or {}).get("booking") or {}
                if b.get("status") != "draft" or b.get("demo"):
                    self._json({"error": "nothing to book"}, 400)
                    return
                try:
                    b["link"] = create_booking(b, _call_dir((app.last or {}).get("folder") or ""))
                    b["status"] = "booked"
                    if b["link"].startswith("file:"):
                        b["note"] = tr()["book_opened"]
                    log(f"booked: {b['title']} {b['when']}")
                except Exception as e:  # noqa: BLE001
                    b["status"], b["note"] = "draft", f"Booking failed: {str(e)[:120]}"
                    log(f"booking failed: {e}")
                self._json(b)
            elif self.path.startswith("/feedback"):
                # your feedback on a call improves the coach, call by call
                d = _call_dir(str(body.get("call") or "")) if body.get("call") else None
                last = json.loads((d / "copilot.json").read_text()) if d else (app.last or {})
                row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "rating": body.get("rating") if body.get("rating") in ("useful", "not_useful") else None,
                       "note": str(body.get("note") or "").strip()[:2000],
                       **{k: last.get(k) for k in ("title", "started", "type", "share", "target", "cues", "items")}}
                with open(FEEDBACK_FILE, "a") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                fb = {"rating": row["rating"], "note": row["note"]}
                if d:
                    last["feedback"] = fb
                    (d / "copilot.json").write_text(json.dumps(last, ensure_ascii=False, indent=1, default=str))
                if app.last is not None and (not d or app.last.get("started") == last.get("started")):
                    app.last["feedback"] = fb
                log(f"feedback: {row['rating']} {row['note'][:80]}")
                self._json({})
            else:
                self._send(404, b"not found", "text/plain")
    return Handler


def start_server(app: App) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"window: http://127.0.0.1:{PORT}")
    return srv


def open_window() -> None:
    """The floating window app; a Chrome app window when it is not installed."""
    if WIDGET_APP.exists():
        cmd = ["open", "-g", "-a", str(WIDGET_APP)]   # -g: never in front of what you are doing
    else:
        cmd = ["open", "-na", "Google Chrome", "--args", f"--app=http://127.0.0.1:{PORT}/", "--window-size=460,900"]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---- every call, for the window's "Opkald" view: an overview, and how one call went. The source is each
# recording folder's copilot.json.
def _call_dir(cid: str) -> Path | None:
    if not cid:
        return None
    p = (ROOT / cid).resolve()
    ok = p.parent == ROOT.resolve() and ((p / "copilot.json").is_file() or (p / "meta.json").is_file())
    return p if ok else None


_TURNS: dict = {}


def _turn_stats(folder: Path) -> dict:
    """Talk share and longest stretches from a recording's turns.json (mic = you). Turns of one side less than
    2 s apart count as one stretch."""
    f = folder / "turns.json"
    try:
        mtime = f.stat().st_mtime
    except OSError:
        return {}
    if f in _TURNS and _TURNS[f][0] == mtime:
        return _TURNS[f][1]
    try:
        turns = json.loads(f.read_text())
    except (OSError, ValueError):
        return {}
    talk = {"mic": 0.0, "system": 0.0}
    longest = {"mic": 0.0, "system": 0.0}
    side, a, b = None, 0.0, 0.0
    for t in turns:
        tr_ = "mic" if t.get("track") == "mic" else "system"
        talk[tr_] += max(0.0, t["end"] - t["start"])
        if tr_ == side and t["start"] - b < 2.0:
            b = t["end"]
            continue
        if side:
            longest[side] = max(longest[side], b - a)
        side, a, b = tr_, t["start"], t["end"]
    if side:
        longest[side] = max(longest[side], b - a)
    total = talk["mic"] + talk["system"]
    out = {"share": round(100 * talk["mic"] / total) if total else None, "talk_s": [round(talk["mic"]), round(talk["system"])],
           "my_longest_s": round(longest["mic"]), "their_longest_s": round(longest["system"])}
    _TURNS[f] = (mtime, out)
    return out


def _call_type(meta: dict) -> str:
    """For a call the coach did not sit on: team, else the title's words."""
    ev = (meta.get("events") or [{}])[0] or {}
    emails = [e.lower() for e in ev.get("attendee_emails") or []]
    own = team_domains()
    if emails and own and all(e.rpartition("@")[2] in own for e in emails):
        return "internal"
    low = (meta.get("title") or "").lower()
    return next((kind for pattern, kind in TITLE_TYPES if re.search(pattern, low)), "other")


def calls_list(limit: int = 80) -> list[dict]:
    """Every recorded call, newest first: the ones the coach sat on (copilot.json) and transcribed ones before it."""
    rows = []
    t = tr()
    for folder in ROOT.iterdir() if ROOT.exists() else []:
        if not folder.is_dir():
            continue
        try:
            c = json.loads((folder / "copilot.json").read_text())
        except (OSError, ValueError):
            c = None
        meta = read_meta(folder) or {}
        if not c and meta.get("status") != "transcribed":
            continue
        if c:
            d = c.get("debrief") or {}
            rows.append({"id": folder.name, "title": c.get("title") or "", "started": c.get("started"),
                         "duration_s": c.get("duration_s"), "type": c.get("type") or "other", "share": c.get("share"),
                         "target": c.get("target"), "demo": bool(c.get("demo")), "coached": True,
                         "verdict": d.get("verdict") or "", "feedback": bool(c.get("feedback"))})
        else:
            kind = _call_type(meta)
            st = _turn_stats(folder)
            rows.append({"id": folder.name, "title": meta.get("title") or t["untitled"],
                         "started": (meta.get("started") or "").replace("Z", "+00:00"), "duration_s": meta.get("duration_s"),
                         "type": kind, "share": st.get("share"), "target": target(kind), "demo": False,
                         "coached": False, "verdict": "", "feedback": False})
    for r in rows:
        r["type_label"] = type_label(r["type"])
    return sorted(rows, key=lambda r: r["started"] or "", reverse=True)[:limit]


# ---- "Bliv bedre": how the calls go over the weeks, three goals, and what to practise. Three things, no more:
# on time, talk share within target, no long monologues (which drive the other two). Targets are the share of the
# week's calls that make it.
GOAL_TARGETS = {"on_time": 100, "talk": 80, "my_mono": 80}
RECORDER_TAIL_S = 30   # the recorder stops this long after the call app lets go of the mic
GROWTH_WEEKS = 8
_growth_lock = threading.Lock()


def _call_record(folder: Path) -> dict | None:
    """One real call's numbers: type, talk share and stretches, minutes over the calendar's end."""
    meta = read_meta(folder)
    if not meta:
        return None
    try:
        c = json.loads((folder / "copilot.json").read_text())
    except (OSError, ValueError):
        c = None
    if (c and c.get("demo")) or meta.get("status") == "practice" or (not c and meta.get("status") != "transcribed"):
        return None
    kind = (c or {}).get("type") or _call_type(meta)
    st = {"share": c.get("share"), "my_longest_s": c.get("my_longest_s"), "their_longest_s": c.get("their_longest_s")} \
        if c else _turn_stats(folder)
    started = (c or {}).get("started") or (meta.get("started") or "").replace("Z", "+00:00")
    if not started or st.get("share") is None:
        return None
    ev = (meta.get("events") or [{}])[0]
    over = None
    try:
        end = (c or {}).get("end_planned") or ev.get("end")
        # the meeting ended when the other side last spoke: the recorder runs on while the call app keeps the mic
        try:
            turns = json.loads((folder / "turns.json").read_text())
            last = max((t["end"] for t in turns if t.get("track") != "mic"), default=None)
        except (OSError, ValueError):
            last = None
        if last is not None:
            ended = datetime.fromisoformat(meta["started"].replace("Z", "+00:00")) + timedelta(seconds=last)
        elif (c or {}).get("ended_at"):
            ended = datetime.fromisoformat(c["ended_at"])
        else:
            ended = datetime.fromisoformat(meta["ended"].replace("Z", "+00:00")) - timedelta(seconds=RECORDER_TAIL_S)
        if end:
            over = round((ended - datetime.fromisoformat(end.replace("Z", "+00:00"))).total_seconds())
    except (KeyError, ValueError, TypeError):
        pass
    ct = CALL_TYPES.get(kind, CALL_TYPES["other"])
    return {"id": folder.name, "started": started, "type": kind, "title": (c or meta).get("title") or ev.get("title") or "",
            "share": st.get("share"), "target": target(kind), "stretch": ct["stretch"], "my_longest_s": st.get("my_longest_s"),
            "their_longest_s": st.get("their_longest_s"), "over_s": over, "coached": bool(c),
            "debrief": (c or {}).get("debrief") or {}, "feedback": (c or {}).get("feedback"), "pause": (c or {}).get("pause")}


def _pause_week(rs: list[dict]) -> dict:
    """The week's pause after your questions: how often you let them answer, and the median wait before they did."""
    ps = [r["pause"] for r in rs if r.get("pause")]
    q = sum(p["questions"] for p in ps)
    meds = [p["median"] for p in ps if p.get("median") is not None]
    return {"questions": q, "answered_pct": round(100 * sum(p["answered"] for p in ps) / q) if q else None,
            "pause_median": round(float(np.median(meds)), 1) if meds else None}


def bundle_text(cid: str) -> str | None:
    """One call as text, to paste in a Claude chat or anywhere: your agenda and notes, the coach's result and
    notes, the transcript. Headings in Danish, like everything you read."""
    c = call_detail(cid)
    d = _call_dir(cid)
    if not c or not d:
        return None
    parts = [f"# {c.get('title') or 'Møde'}", f"Tidspunkt: {(c.get('started') or '')[:16].replace('T', ' ')}"]
    status = {i.get("label"): i.get("status") for i in c.get("items") or []}
    mine = [it["label"] for it in ((c.get("plan") or {}).get("items") or []) if it.get("kind") == "agenda"]
    if mine:
        parts.append("## Min dagsorden\n" + "\n".join(f"- [{'x' if status.get(m) == 'covered' else ' '}] {m}" for m in mine))
    if (c.get("my_notes") or "").strip():
        parts.append("## Mine noter\n" + c["my_notes"].strip())
    db = c.get("debrief") or {}
    if db.get("verdict"):
        res = [db["verdict"]]
        res += [f"- Næste skridt: {x}" for x in db.get("next_steps") or []]
        res += [f"- Spørg næste gang: {x}" for x in db.get("ask_next") or [] if isinstance(x, str)]
        if db.get("practise"):
            res.append(f"- Øv: {db['practise']}")
        parts.append("## Coachens vurdering\n" + "\n".join(res))
    if c.get("notes"):   # the notes carry their own headings
        parts.append(c["notes"].strip() if c["notes"].lstrip().startswith("#") else "## Noter\n" + c["notes"].strip())
    try:
        tx = (d / "transcript.md").read_text(errors="ignore").split("## Transcript", 1)
        if len(tx) == 2 and tx[1].strip():
            parts.append("## Transskription\n" + tx[1].strip())
    except OSError:
        pass
    return "\n\n".join(parts) + "\n"


def _goal_hit(goal: str, r: dict) -> bool | None:
    """Did this call meet the goal? None when it does not apply (no calendar end, no talk target)."""
    if goal == "on_time":
        return None if r["over_s"] is None else r["over_s"] <= 60
    if goal == "talk":
        return None if not r["target"] else r["share"] <= r["target"][1]
    if goal == "my_mono":
        return None if not r["stretch"] or r["my_longest_s"] is None else r["my_longest_s"] <= r["stretch"]
    return None


def _week(started: str) -> str:
    y, w, _ = datetime.fromisoformat(started).astimezone(LOCAL_TZ).isocalendar()
    return f"{y}-W{w:02d}"


def _growth_refresh(recs: list[dict], weeks: list[dict]) -> None:
    """The coach's read on you across calls: once per new coached call, at most hourly, in the background."""
    if how_ai() == "off" or not _growth_lock.acquire(blocking=False):
        return
    try:
        coached = [r for r in recs if r["coached"] and r["debrief"].get("verdict")][:15]
        key = ",".join(r["id"] for r in coached)
        try:
            old = json.loads(GROWTH_FILE.read_text())
        except (OSError, ValueError):
            old = {}
        if not coached or old.get("key") == key or time.time() - old.get("at", 0) < 3600 and old.get("out"):
            return
        lines = []
        for r in coached:
            d = r["debrief"]
            when = datetime.fromisoformat(r["started"]).astimezone(LOCAL_TZ)
            lines.append(f"- {r['title']} ({day_str(when, weekday=False)}, {type_label(r['type'])}): talked {r['share']}%"
                         + (f" (target {r['target'][0]}-{r['target'][1]}%)" if r["target"] else "")
                         + f", longest {r['my_longest_s']} s, theirs {r['their_longest_s']} s"
                         + (f", {round(r['over_s'] / 60)} min over time" if (r["over_s"] or 0) > 60 else "")
                         + f". Coach: {d.get('verdict', '')} Practise: {d.get('practise', '')} Moments: "
                         + "; ".join(f"{m.get('at')} \"{m.get('say', '')}\"" for m in d.get("moments") or [])
                         + (f" Their feedback: {r['feedback'].get('rating')} {r['feedback'].get('note', '')}" if r.get("feedback") else ""))
        user = ("NUMBERS PER WEEK (newest last):\n" + json.dumps(weeks, ensure_ascii=False) + "\n\nRECENT CALLS (newest first):\n"
                + "\n".join(lines))
        out = ask_json(_fill(P.GROWTH_SYSTEM) + extra_rules("growth"), user, P.GROWTH_SCHEMA)
        GROWTH_FILE.write_text(json.dumps({"key": key, "at": time.time(), "calls": len(coached), "out": out}, ensure_ascii=False))
        log(f"growth: coach read on {len(coached)} calls")
    except Exception as e:  # noqa: BLE001
        log(f"growth read failed: {e}")
    finally:
        _growth_lock.release()


def growth() -> dict:
    """The "Bliv bedre" tab: the three goals this week and the weeks before, and the coach's read."""
    t = tr()
    recs = sorted((r for r in (_call_record(f) for f in ROOT.iterdir() if f.is_dir()) if r),
                  key=lambda r: r["started"], reverse=True) if ROOT.exists() else []
    now = datetime.now(LOCAL_TZ)
    names = []
    for back in range(GROWTH_WEEKS - 1, -1, -1):
        y, w, _ = (now - timedelta(weeks=back)).isocalendar()
        names.append(f"{y}-W{w:02d}")
    by_week = {n: [r for r in recs if _week(r["started"]) == n] for n in names}
    while len(names) > 4 and not by_week[names[0]]:   # the weeks before the first recording say nothing
        names.pop(0)

    def rate(goal: str, rs: list[dict]) -> dict:
        hits = [h for h in (_goal_hit(goal, r) for r in rs) if h is not None]
        return {"hit": sum(hits), "of": len(hits), "pct": round(100 * sum(hits) / len(hits)) if hits else None}

    def info(goal: str, r: dict) -> str:   # one meeting, for its dot in the chart
        d = datetime.fromisoformat(r["started"]).astimezone(LOCAL_TZ)
        val = (t["g_over"].format(m=round(r["over_s"] / 60)) if goal == "on_time"
               else t["g_talk"].format(p=r["share"], hi=r["target"][1]) if goal == "talk"
               else t["g_mono"].format(a=mmss(r["my_longest_s"]), b=mmss(r["stretch"])))
        return f"{r['title'][:44]}, {day_str(d, weekday=False)}: {val}"

    this_week = sorted(by_week[names[-1]], key=lambda r: r["started"])
    goal_rows = [{"id": k, "label": t["goals"][k][0], "about": t["goals"][k][1], "target": GOAL_TARGETS[k],
                  "this": rate(k, this_week), "weeks": [{"week": n, **rate(k, by_week[n])} for n in names],
                  "calls": [{"hit": h, "info": info(k, r)} for r in this_week if (h := _goal_hit(k, r)) is not None]}
                 for k in GOAL_TARGETS]
    weeks = []
    for n in names:
        rs = by_week[n]
        sales = [r["share"] for r in rs if r["type"] in sales_kinds()]
        weeks.append({"week": n, "calls": len(rs), "share_sales": round(sum(sales) / len(sales)) if sales else None,
                      **{k: rate(k, rs)["pct"] for k in GOAL_TARGETS},
                      "my_longest": round(sum(r["my_longest_s"] or 0 for r in rs) / len(rs)) if rs else None,
                      **_pause_week(rs)})
    try:
        cached = json.loads(GROWTH_FILE.read_text())
    except (OSError, ValueError):
        cached = {}
    threading.Thread(target=_growth_refresh, args=(recs, weeks), daemon=True).start()
    return {"week": names[-1], "weeks": weeks, "goals": goal_rows, "calls": len(recs), "pause": _pause_week(this_week),
            "coach": cached.get("out"), "coach_calls": cached.get("calls"), "coach_at": cached.get("at")}


def call_detail(cid: str) -> dict | None:
    d = _call_dir(cid)
    if not d:
        return None
    try:
        c = json.loads((d / "copilot.json").read_text())
        c["coached"] = True
    except (OSError, ValueError):
        meta = read_meta(d) or {}
        kind = _call_type(meta)
        c = {"title": meta.get("title") or "", "started": (meta.get("started") or "").replace("Z", "+00:00"),
             "duration_s": meta.get("duration_s"), "type": kind, "target": target(kind), "coached": False, **_turn_stats(d)}
    if not c.get("notes"):
        c["notes"] = _summary_of(d)
    c.update(id=cid, type_label=type_label(c.get("type") or "other"),
             transcript_url=f"/transcript?id={quote(cid)}" if (d / "transcript.md").is_file() else None)
    return c


def last_summary() -> dict | None:
    """The last call for the window after a restart."""
    def _started(p: Path) -> str:
        try:
            return json.loads(p.read_text()).get("started") or ""
        except (OSError, ValueError):
            return ""
    newest = max(ROOT.glob("*/copilot.json"), key=_started, default=None) if ROOT.exists() else None
    try:
        return json.loads(newest.read_text()) if newest else None
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- the service

def run_session(app: App, session: Session, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            session.step()
        except Exception as e:  # noqa: BLE001
            session.note(f"step failed: {e}")
        time.sleep(0.5)


# --------------------------------------------------------------------------- after the call: the audio

TIDY_AFTER = timedelta(minutes=15)   # a call the coach did not sit on: the recorder's own last writes are long done
_TIDY = threading.Lock()


def tidy_recordings() -> None:
    """How long a call is kept (storage limitation under GDPR), in the settings:
    - audio: about 700 MB an hour raw. Once a call is over and its notes are written, each track is compressed
      (AAC, speech quality, about 15 MB an hour); after keep_audio_days it is deleted (0 = right after the call).
    - everything else of a call (transcript, notes, the coach's result) and your feedback on it: deleted after
      keep_notes_days (None = until you delete it yourself)."""
    if not _TIDY.acquire(blocking=False):   # a long call still compressing from the last minute
        return
    try:
        settings = load_settings()
        keep = int(settings.get("keep_audio_days", 7))
        keep_notes = settings.get("keep_notes_days")
        active = active_recording()
        now = datetime.now(timezone.utc)
        for folder in sorted(p for p in ROOT.iterdir() if p.is_dir()) if ROOT.exists() else []:
            meta = read_meta(folder)
            if not meta or meta.get("status") == "recording" or (active and folder == active[0]):
                continue
            try:
                done_at = datetime.fromtimestamp((folder / "meta.json").stat().st_mtime, timezone.utc)
                started = datetime.fromisoformat(meta["started"].replace("Z", "+00:00"))
            except (OSError, KeyError, ValueError):
                continue
            if keep_notes and now - started >= timedelta(days=int(keep_notes)):
                shutil.rmtree(folder, ignore_errors=True)
                log(f"call deleted after {keep_notes} days: {folder.name}")
                continue
            audio = list(folder.glob("*.caf")) + list(folder.glob("*.m4a"))
            if not audio:
                continue
            if not (folder / "transcript.md").is_file() and now - done_at < TIDY_AFTER:
                continue   # the notes still read the raw tracks
            if now - started >= timedelta(days=keep):
                for f in audio:
                    f.unlink(missing_ok=True)
                log(f"audio deleted after {keep} days: {folder.name}")
                continue
            for caf in folder.glob("*.caf"):
                m4a = caf.with_suffix(".m4a")
                r = subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", "-c", "1", "-b", "32000", str(caf), str(m4a)],
                                   capture_output=True, text=True, timeout=3600)
                if r.returncode == 0 and m4a.exists() and m4a.stat().st_size > 0:
                    caf.unlink()
                else:
                    m4a.unlink(missing_ok=True)
                    log(f"audio not compressed: {caf} ({(r.stderr or '').strip()[:200]})")
        if keep_notes and FEEDBACK_FILE.exists():   # your feedback rows quote the call: same limit
            cut = (now - timedelta(days=int(keep_notes))).isoformat()
            rows = [l for l in FEEDBACK_FILE.read_text().splitlines() if l.strip()]
            keep_rows = [l for l in rows if (json.loads(l).get("at") or "") >= cut]
            if len(keep_rows) != len(rows):
                FEEDBACK_FILE.write_text("".join(l + "\n" for l in keep_rows))
    except Exception as e:  # noqa: BLE001
        log(f"tidy failed: {e}")
    finally:
        _TIDY.release()


def _minute_tick() -> None:
    """Every minute, in the background: the recorder's meeting schedule, add-ons, the day list."""
    try:
        cal.write_schedule(DATA, ROOT)
    except Exception as e:  # noqa: BLE001
        log(f"schedule not written: {e}")
    addons.tick()
    threading.Thread(target=tidy_recordings, daemon=True).start()   # a long call takes a while to compress
    plan = day_plan()
    DAY_PLAN.clear()
    DAY_PLAN.update({"calls": plan, "generated_at": datetime.now(timezone.utc).isoformat()})


def morning_open(app: App, settings: dict) -> None:
    """The first time the Mac is in use after 06:00, the window opens on the day's calls, once a day."""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if now.hour < 6 or app.session or not settings["enabled"] or settings.get("auto_open") == "never":
        return
    try:
        if MORNING_FILE.read_text().strip() == today:
            return
    except OSError:
        pass
    if idle_seconds() > 120:   # not at the Mac yet
        return
    MORNING_FILE.write_text(today)
    soon = [v for v in PREP.values() if v["start"] - datetime.now(timezone.utc) < timedelta(hours=16)]
    if soon and not app.window_open():
        open_window()
        log(f"morning: opened the day's {len(soon)} call(s)")


def maybe_open(app: App, settings: dict) -> None:
    mode = settings.get("auto_open", "all")
    if mode == "never" or app.window_open():   # one window is enough; it follows every call
        return
    if mode == "calendar" and not app.session.ctx.get("event_id"):
        return
    open_window()


def boot() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    loaded = addons.load(load_settings().get("addons") or [], log)
    if loaded:
        log(f"add-ons: {', '.join(loaded)}")
    for k, v in addons.call_types().items():
        CALL_TYPES[k] = {**CALL_TYPES.get(k, CALL_TYPES["other"]), **v}
    apply_my_types(load_settings())


def cmd_serve(args) -> None:
    boot()
    ROOT.mkdir(parents=True, exist_ok=True)
    app = App()
    app.last = last_summary()
    start_server(app)
    log(f"Sales Coach {VERSION}: recordings {ROOT}, data {DATA}, AI {how_ai()}")
    if WIDGET_APP.exists() and not args.no_open and subprocess.run(
            ["pgrep", "-f", f"{WIDGET_APP.name}/Contents/MacOS"], capture_output=True).returncode != 0:
        subprocess.Popen(["open", "-g", "-a", str(WIDGET_APP), "--args", "--background"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stop = None
    _load_prep()
    _load_joined()
    last_minute = 0.0
    while True:
        try:
            settings = load_settings()
            if time.time() - last_minute >= 60:
                last_minute = time.time()
                threading.Thread(target=_minute_tick, daemon=True).start()
                if settings["enabled"]:
                    threading.Thread(target=prep_upcoming, daemon=True).start()
            if not args.no_open:
                morning_open(app, settings)
            # the window opens before a prepared call, so the brief is read before joining
            now = datetime.now(timezone.utc)
            for entry in PREP.values():
                if (not entry.get("opened") and entry.get("status") == "ready" and not app.session
                        and settings["enabled"] and settings.get("auto_open") != "never" and not args.no_open
                        and entry["start"] - OPEN_BEFORE <= now <= entry["start"] + timedelta(minutes=5)):
                    entry["opened"] = True
                    if not app.window_open():
                        open_window()
            rec = active_recording() if settings["enabled"] else None   # off: nothing is measured or written
            s = app.session
            # Hang-up = the call app let go of the mic HANGUP_S ago. The recorder itself only stops 30 s later.
            mic = call_app_holds_mic() if (rec or s) else None
            if (s is not None and s.phone) or (rec and (read_meta(rec[0]) or {}).get("trigger") == "phone"):
                mic = None   # a phone call: no call app on the Mac, it ends when the recording is stopped
            if s is not None:
                s.mic_free_since = None if mic or mic is None else (s.mic_free_since or time.time())
            hung_up = bool(s and s.mic_free_since and time.time() - s.mic_free_since >= HANGUP_S)
            if s is not None and (not rec or rec[0] != s.folder or hung_up):
                stop.set()
                time.sleep(0.5)
                s.step()
                summary = s.finish(notes=None if settings["enabled"] else False)
                if not s.forgotten:
                    app.last = summary
                app.session = None
                app.ended_folder = s.folder if hung_up and rec and rec[0] == s.folder else None
                app.ended_event = s.ctx.get("event_id") or ""
                log(f"call ended ({'hang-up' if hung_up else 'recording stopped'}): {s.folder.name}")
                if s.opened and settings["enabled"] and settings.get("auto_open") != "never" and not args.no_open:
                    open_window()   # the result is the point of the minute after a call
            # a new call, or the same recording picked up again (the app took the mic back)
            if rec and app.session is None and (rec[0] != app.ended_folder or mic):
                after = app.ended_event if rec[0] == app.ended_folder else None
                if rec[0] == app.ended_folder:
                    app.ended_folder = None
                log(f"call started: {rec[0].name}")
                app.session = Session(*rec, after=after)
                stop = threading.Event()
                threading.Thread(target=run_session, args=(app, app.session, stop), daemon=True).start()
            s = app.session
            if s and not s.opened and s.confirmed and not args.no_open:
                s.opened = True
                maybe_open(app, settings)
            if not args.no_open:
                join_tick(app, settings)
        except Exception as e:  # noqa: BLE001  (one bad call must not take the coach down)
            log(f"loop error: {e}")
        time.sleep(1)


def _feed_from(transcript: Path, me: str, from_s: float) -> list[dict]:
    """[hh:mm:ss] Name: text lines; `me` is your speaker name in them (default: your first name)."""
    rows = re.findall(r"^\[(\d\d):(\d\d):(\d\d)\] ([^:]+): (.*)$", transcript.read_text(errors="ignore"), re.M)
    me = me or me_first()
    feed = [{"t": int(h) * 3600 + int(m) * 60 + int(s), "who": "me" if name == me else "them", "text": text}
            for h, m, s, name, text in rows]
    return [l for l in feed if l["t"] >= from_s]


def cmd_replay(args) -> None:
    """A recorded call through the coach at any speed, as if it were live (for testing changes)."""
    boot()
    folder = Path(args.folder).expanduser()
    meta = read_meta(folder)
    started = datetime.fromisoformat(meta["started"].replace("Z", "+00:00"))
    app = App()
    start_server(app)
    event = json.loads(Path(args.event_json).read_text()) if args.event_json else None
    feed = _feed_from(Path(args.transcript), args.me or "", args.from_min * 60) if args.transcript else None
    session = Session(folder, started, replay={"from": args.from_min * 60, "speed": args.speed},
                      use_llm=not args.no_llm, event=event if event is not None else {}, whisper=not args.no_whisper, feed=feed)
    app.session = session
    stop = threading.Event()
    threading.Thread(target=run_session, args=(app, session, stop), daemon=True).start()
    if args.open:
        open_window()
    end = args.from_min * 60 + args.minutes * 60
    last_print = 0.0
    while session.elapsed() < end:
        time.sleep(1)
        if time.time() - last_print >= 30:
            last_print = time.time()
            st = session.snapshot()
            log(f"@{mmss(st['elapsed'])} you {st['share']}% (5 min {st['share5']}%), stretch {st['stretch']}s, "
                f"speech-to-line {st['lag']}s, live updates {session.partials_shown}, cues {[c['text'] for c in st['cues']]}")
    stop.set()
    time.sleep(1)
    session.step()
    print(json.dumps(session.snapshot(), ensure_ascii=False, indent=1)[:4000])
    if args.debrief:
        summary = session.finish(notes=False)
        t = time.time()
        while summary.get("debrief_status") == "writing" and time.time() - t < 120:
            time.sleep(0.5)
        log(f"debrief after {summary.get('debrief_after_s')} s:\n" + json.dumps(summary.get("debrief"), ensure_ascii=False, indent=1))
        if args.out:
            Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    elif args.dump:
        session.finish(notes=False)
        if session.transcriber:
            session.transcriber.join(timeout=120)
            session.consolidator.join(timeout=300)
        Path(args.dump).write_text(json.dumps(session.lines, ensure_ascii=False, indent=1))
        log(f"lines written to {args.dump}")
    if args.hold:
        app.session, app.last = None, (summary if args.debrief else None)
        log(f"holding the window for {args.hold}s")
        time.sleep(args.hold)


def cmd_cues(args) -> None:
    """A recorded call's transcript through the coach as it is now, turn by turn at the live cadence, without
    waiting: every cue that would have shown. For judging a change to the cues."""
    boot()
    folder = _call_dir(args.folder) or Path(args.folder).expanduser()
    c = json.loads((folder / "copilot.json").read_text())
    plan = c.get("plan")
    if not plan:
        sys.exit("no checklist saved with this call")
    lines = _feed_from(folder / "transcript.md", args.me or "", 0)
    for a, b in zip(lines, lines[1:]):
        a["end"] = b["t"]
    if args.minutes:
        lines = [l for l in lines if l["t"] <= args.minutes * 60]
    session = Session.__new__(Session)   # no recorder, no Whisper: only what a coach turn reads
    session.__dict__.update(lock=threading.RLock(), lines=[], plan=plan, status={it["id"]: "open" for it in plan["items"]},
                            item_notes={}, cue_log=[], ai_cues=[], phase=None, ai_state="on", claude=None, claude_turns=0,
                            sent_t={"me": -1.0, "them": -1.0}, sent_end={"me": -1.0, "them": -1.0}, notes=[], how=how_ai(),
                            me=me_first(), ctx={"title": c.get("title") or "", "type": c.get("type") or "other",
                                                "agenda": c.get("agenda") or [], "planned_min": c.get("planned_min"),
                                                "who": c.get("who") or "Them"},
                            playbook=playbook(c.get("type") or "other"))
    clock = {"t": 0.0}
    session.call_elapsed = lambda: clock["t"]
    last, pending, turns = -99.0, [], 0
    for l in lines:
        session.lines.append(l)
        pending.append(l)
        clock["t"] = l["t"]
        since = l["t"] - last
        if since >= CUE_MIN_GAP and (any(p["who"] == "them" for p in pending) or since >= CUE_MAX_GAP):
            before = len(session.cue_log)
            session._cue_call()
            turns += 1
            last, pending = l["t"], []
            for q in session.cue_log[before:]:
                print(f"{mmss(q['at'])} [{q['item'] or '-'}] {q['text']}" + (f"  ({q['source']})" if q["source"] else ""), flush=True)
    print(f"\n{turns} turns, {len(session.cue_log)} cues (live: {len(c.get('cues') or [])}); "
          + " ".join(f"{k}={v[0]}" for k, v in session.status.items()))
    if session.claude:
        session.claude.close()


def cmd_stats(args) -> None:
    """Your share of the words on the last N calls with notes."""
    me = me_first()
    rows = []
    for f in sorted(ROOT.glob("*/transcript.md")):
        t = f.read_text(errors="ignore")
        lines = re.findall(r"^\[\d\d:\d\d:\d\d\] ([^:]+): (.*)$", t.split("## Transcript", 1)[-1], re.M)
        mine = sum(len(x[1].split()) for x in lines if x[0] == me)
        theirs = sum(len(x[1].split()) for x in lines if x[0] != me)
        if mine + theirs < 400 or mine == 0 or theirs == 0:
            continue   # too short, or one track missing (a 0% call is a dead mic, not silence)
        rows.append((f.parent.name[:16], t.splitlines()[0].lstrip("# ")[:60], round(100 * mine / (mine + theirs))))
    for d, title, share in rows[-args.n:]:
        print(f"{d}  {share:3d}%  {title}")
    if rows:
        print(f"median {int(np.median([r[2] for r in rows[-args.n:]]))}% over {len(rows[-args.n:])} calls")


def cmd_settings(args) -> None:
    changes = {k: v for k, v in vars(args).items() if k not in ("cmd", "func") and v is not None}
    s = save_settings(changes) if changes else load_settings()
    print(json.dumps(s, ensure_ascii=False, indent=1))
    print(f"AI: {how_ai()}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--no-open", action="store_true", help="never open the window by itself")
    r = sub.add_parser("replay")
    r.add_argument("folder")
    r.add_argument("--speed", type=float, default=1.0)
    r.add_argument("--from-min", type=float, default=0.0)
    r.add_argument("--minutes", type=float, default=10.0)
    r.add_argument("--event-json", help="the calendar event for this call (calendar_feed's format)")
    r.add_argument("--transcript", help="feed a finished transcript.md instead of running Whisper")
    r.add_argument("--me", help="your speaker name in that transcript")
    r.add_argument("--no-llm", action="store_true")
    r.add_argument("--no-whisper", action="store_true", help="talk meter only")
    r.add_argument("--dump", help="write the final transcript lines (JSON) here at the end")
    r.add_argument("--debrief", action="store_true", help="end with the coach's result, as at hang-up")
    r.add_argument("--out", help="with --debrief: write the call's summary JSON here")
    r.add_argument("--open", action="store_true")
    r.add_argument("--hold", type=int, default=0, help="keep the window served this many seconds after")
    cu = sub.add_parser("cues", help="a recorded call's transcript through the coach as it is now")
    cu.add_argument("folder")
    cu.add_argument("--minutes", type=float)
    cu.add_argument("--me", help="your speaker name in its transcript (default: your first name)")
    st = sub.add_parser("stats")
    st.add_argument("n", nargs="?", type=int, default=20)
    se = sub.add_parser("settings", help="show or change the settings")
    se.add_argument("--name")
    se.add_argument("--company")
    se.add_argument("--about", help="what you sell, in one line")
    se.add_argument("--language", choices=list(T))
    se.add_argument("--ai", choices=["auto", "claude_code", "api", "off"])
    se.add_argument("--api-key", dest="api_key", help="your Anthropic API key (stored in the Keychain)")
    se.add_argument("--words", help="names and terms for speech-to-text, comma separated")
    se.add_argument("--team-domains", dest="team_domains", help="your team's mail domains, comma separated")
    se.add_argument("--addons", help="add-on files, comma separated")
    se.add_argument("--keep-audio-days", dest="keep_audio_days", type=int,
                    help="days to keep call audio (compressed) before it is deleted; 0 = right after the call")
    se.add_argument("--keep-notes-days", dest="keep_notes_days",
                    help="days to keep transcripts, notes and results; never = until you delete them")
    se.add_argument("--notice", type=lambda v: v.lower() in ("1", "on", "yes", "true"),
                    help="on/off: remind you at the start of each call to tell them you record")
    args = p.parse_args()
    {"serve": cmd_serve, "replay": cmd_replay, "stats": cmd_stats, "cues": cmd_cues, "settings": cmd_settings}[args.cmd](args)


if __name__ == "__main__":
    main()

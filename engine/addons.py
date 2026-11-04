"""Add-ons: your own data in the coach, without changing it.

An add-on is one Python file listed in the "addons" setting (or SALES_COACH_ADDONS, paths separated by
":"). It defines any of these functions; all are optional, and one that fails is logged and skipped,
never the call:

    calendar(time_min, time_max) -> list[dict] | None
        Events in calendar_feed's format, instead of calendar.json. None = not mine, ask the next one.
    call_types() -> dict
        Extra call types: {"key": {"da": label, "en": label, "target": (lo, hi) | None, "stretch": s, "sales": bool}}.
    context(event, ctx) -> None
        Adds to the call's context in place: ctx["prep_parts"] (text the checklist is built from),
        ctx["history_parts"] (who they are, where you left off), ctx["links"] ({"label", "url"}),
        ctx["type"]/ctx["type_source"], ctx["mode"] ("client" | "lead" | "other"), ctx["stage"],
        ctx["std"] (your standard questions: {"id", "short", "q", "on_file"}), ctx["who"], ctx["words"].
    prompt_notes(kind) -> str
        Extra rules for the AI; kind is "plan", "cues" or "growth".
    playbook(call_type) -> list[dict]
        Extra principles for the cues: {"id", "expert", "claim", "action"}.
    book(draft) -> str | None
        Book the agreed next meeting yourself (e.g. with invitations); return its link. None = not mine.
    day_plan() -> list[dict]
        Coming calls with what is still missing before each: {"event_id", "start", "title", "type", "items"}.
    after_call(folder, summary) -> None
        Runs after the coach's notes for a call are written.
    tick() -> None
        Every minute, in the background.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime
from pathlib import Path

_loaded: list = []
_log = print


def load(paths: list[str], log=print) -> list[str]:
    global _log
    _log = log
    _loaded.clear()
    paths = list(paths or []) + [p for p in os.environ.get("SALES_COACH_ADDONS", "").split(os.pathsep) if p]
    names = []
    for p in dict.fromkeys(paths):
        path = Path(p).expanduser()
        try:
            spec = importlib.util.spec_from_file_location(f"coach_addon_{path.stem}", path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            _loaded.append(mod)
            names.append(path.stem)
        except Exception as e:  # noqa: BLE001
            log(f"add-on {path} not loaded: {e}")
    return names


def _each(hook: str, *args):
    for mod in _loaded:
        fn = getattr(mod, hook, None)
        if not fn:
            continue
        try:
            yield fn(*args)
        except Exception as e:  # noqa: BLE001
            _log(f"add-on {mod.__name__}.{hook} failed: {e}")


def calendar(time_min: datetime, time_max: datetime) -> list[dict] | None:
    return next((r for r in _each("calendar", time_min, time_max) if r is not None), None)


def call_types() -> dict:
    out = {}
    for r in _each("call_types"):
        out.update(r or {})
    return out


def context(event: dict | None, ctx: dict) -> None:
    for _ in _each("context", event, ctx):
        pass


def prompt_notes(kind: str) -> str:
    return "\n".join(r for r in _each("prompt_notes", kind) if r)


def playbook(call_type: str) -> list[dict]:
    return [p for r in _each("playbook", call_type) for p in r or []]


def book(draft: dict) -> str | None:
    return next((r for r in _each("book", draft) if r), None)


def day_plan() -> list[dict] | None:
    got = [c for r in _each("day_plan") for c in r or []]
    return got if any(getattr(m, "day_plan", None) for m in _loaded) else None


def after_call(folder: Path, summary: dict) -> None:
    for _ in _each("after_call", folder, summary):
        pass


def tick() -> None:
    for _ in _each("tick"):
        pass

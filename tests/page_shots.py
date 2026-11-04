#!/usr/bin/env python3
"""The guide page's pictures of the window, taken from the made-up test call, so the page shows the window as
it is now. Run it after a change to the window (release.sh reminds you):

    .venv/bin/python tests/page_shots.py

Writes docs/img/during.png (in the call), after.png (the result) and notice.png (telling them you record).
Needs the engine's packages, Playwright (`pip install playwright && playwright install chromium`), the Mac's
Danish voice for the test call, and Claude (Claude Code or an API key) for the checklist and cues.
Runs on its own port and folders, so it never touches a coach that is running.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
IMG = REPO / "docs" / "img"
PORT = 8767
BASE = f"http://127.0.0.1:{PORT}"


def state() -> dict:
    try:
        return json.loads(urllib.request.urlopen(f"{BASE}/state", timeout=2).read())
    except OSError:
        return {}


def wait_for(test, timeout: float, what: str) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        st = state()
        if st and test(st):
            return st
        time.sleep(1)
    sys.exit(f"timed out waiting for {what}")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="sc-shots-"))
    env = {**os.environ, "SALES_COACH_ROOT": str(tmp / "rec"), "SALES_COACH_DATA": str(tmp / "data"),
           "SALES_COACH_PORT": str(PORT), "SALES_COACH_QUIET": "1", "SALES_COACH_APP": "/nonexistent"}
    call = tmp / "rec" / "call"
    subprocess.run([sys.executable, str(REPO / "tests" / "make_test_call.py"), str(call)], check=True, env=env)
    subprocess.run([sys.executable, str(ENGINE / "coach.py"), "settings", "--name", "Anna Holm", "--company",
                    "Holm Rådgivning", "--about", "Bogføring og økonomistyring for små virksomheder", "--language", "da"],
                   check=True, env=env, stdout=subprocess.DEVNULL)
    replay = subprocess.Popen([sys.executable, str(ENGINE / "coach.py"), "replay", str(call), "--event-json",
                               str(call / "event.json"), "--minutes", "1.3", "--debrief", "--hold", "180"],
                              env=env, stdout=open(tmp / "replay.log", "w"), stderr=subprocess.STDOUT)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            live = browser.new_page(viewport={"width": 360, "height": 560}, device_scale_factor=2)
            wait_for(lambda st: st.get("active"), 120, "the call to start")
            live.goto(f"{BASE}/?widget=1")
            st = wait_for(lambda st: st.get("active") and st.get("elapsed", 0) >= 45 and st.get("cues")
                          and any(c.get("kind") == "ai" for c in st["cues"]), 150, "a cue from the coach")
            live.wait_for_timeout(1500)
            live.screenshot(path=str(IMG / "during.png"), clip={"x": 0, "y": 0, "width": 360, "height": 505})
            print("during.png")

            # the reminder to tell them you record: the same moment, as a live call shows it (a replay never asks)
            st["notice"] = {"text": "Hej! Jeg optager vores samtale lokalt på min computer, så jeg kan skrive noter "
                                    "bagefter. Den deles ikke, men en AI-assistent (Claude) hjælper mig med noterne, og "
                                    "lyden slettes efter 7 dage. Sig endelig til, hvis du hellere vil have, at jeg ikke optager."}
            card = browser.new_page(viewport={"width": 520, "height": 900}, device_scale_factor=2)
            card.add_init_script(f"window.COPILOT_PREVIEW = {json.dumps(st)};")
            card.goto((ENGINE / "window.html").as_uri() + "?widget=1")
            card.wait_for_timeout(1500)
            card.locator(".notice").first.screenshot(path=str(IMG / "notice.png"))
            print("notice.png")

            wait_for(lambda st: not st.get("active") and ((st.get("last") or {}).get("debrief")), 180, "the result")
            after = browser.new_page(viewport={"width": 380, "height": 760}, device_scale_factor=2)
            after.goto(f"{BASE}/?widget=1")
            after.wait_for_timeout(2500)
            after.screenshot(path=str(IMG / "after.png"), clip={"x": 0, "y": 0, "width": 380, "height": 645})
            print("after.png")
            browser.close()
    finally:
        replay.terminate()
    print(f"Pictures updated in {IMG}. Check them: the page quotes the cue and the result from these pictures (\"Here:\""
          " in docs/index.html), and Claude words them a little differently each run. Keep a picture with "
          "git checkout -- docs/img/<name>.png, or update the quote. Then commit with a subject starting \"Guide:\".")


if __name__ == "__main__":
    main()

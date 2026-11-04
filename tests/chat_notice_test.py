#!/usr/bin/env python3
"""Checks the Meet chat message steps without a real meeting: the app's JavaScript (from `--chat-js`) against a
mock of Google Meet's chat (closed chat, a "Chat med alle" button, a reactions button that also says "Send", the
real send button next to the box), and the AppleScript quoting through osascript. A real Meet call is the final test.

    .venv/bin/python tests/chat_notice_test.py [path to the built SalesCoach binary]
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

TEXT = 'Hej! Jeg optager vores samtale lokalt. "Citat" med æøå, \\ og en apostrof\'s test.'
MOCK = """<!doctype html><meta charset="utf-8"><body>
<div id="bar"><button aria-label="Send en reaktion" onclick="window.reacted=1">☺</button>
<button aria-label="Chat med alle" onclick="openChat()">💬</button></div><div id="panel"></div><div id="log"></div>
<script>
function openChat() {
  if (document.querySelector('textarea')) return;
  setTimeout(() => {   // Meet draws the panel a moment after the click
    document.getElementById('panel').innerHTML = '<div><div><textarea aria-label="Send en besked"></textarea></div>'
      + '<button aria-label="Send en besked" disabled>➤</button></div>';
    const box = document.querySelector('textarea'), send = document.querySelector('#panel button');
    box.addEventListener('input', () => { send.disabled = !box.value; });
    send.onclick = () => { const d = document.createElement('div'); d.textContent = box.value; document.getElementById('log').appendChild(d); box.value = ''; send.disabled = true; };
  }, 400);
}
</script>"""


def main() -> None:
    binary = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parents[1] / "build" / "test" / "SalesCoach")
    data = json.loads(subprocess.run([binary, "--chat-js", TEXT], capture_output=True, text=True, check=True).stdout)
    for raw, quoted in zip(data["steps"], data["quoted"]):
        back = subprocess.run(["osascript", "-e", f"return {quoted}"], capture_output=True, text=True, check=True).stdout.rstrip("\n")
        assert back == raw, "AppleScript quoting changed the script"
    print("AppleScript quoting: ok")
    page = Path(tempfile.mkdtemp()) / "meet.html"
    page.write_text(MOCK)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto(page.as_uri())
        r1 = pg.evaluate(data["steps"][0])
        pg.wait_for_timeout(1200)
        r2 = pg.evaluate(data["steps"][1])
        pg.wait_for_timeout(500)
        r3 = pg.evaluate(data["steps"][2])
        logged = pg.inner_text("#log")
        reacted = pg.evaluate("window.reacted || 0")
        again = pg.evaluate(data["steps"][0])   # the chat is open now: not clicked again
        b.close()
    print(f"steps: {r1} | {r2} | {r3}; open chat again: {again}; reactions pressed: {reacted}")
    assert (r1, r2, r3, again, reacted) == ("opened", "sent", "ok", "open", 0), "the chat steps did not work"
    assert logged.strip() == TEXT, f"wrong text in the chat: {logged!r}"
    print("Mock Meet chat: ok, the message arrived word for word")


if __name__ == "__main__":
    main()

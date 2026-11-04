# Sales Coach

A live coach for your online sales calls, on your own Mac. No bot joins the call.

While you talk it shows:

- **how much of the talking is yours**, against a target for the kind of call (a first meeting wants you listening, a walkthrough lets you present)
- **the live transcript** of both sides, about 1.5 seconds behind speech
- **a short checklist** for this call, ticked off as the other side answers, with what is still missing
- **one short cue at a time**, only when it matters: a follow-up on what they just said, a number to dig into, time running out
- **a countdown** to the end of the meeting and to your next one

When you hang up, you get:

- **the coach's result** within about 10 seconds: one honest sentence, what was agreed, what to ask next time, one thing to practise
- **notes** with a summary, next steps, decisions and numbers, about a minute later
- **your call history**, and a page that shows over the weeks whether you end on time, keep your talk share on target and avoid long monologues

It works with Google Meet, Zoom, Teams, Slack, FaceTime and WhatsApp calls, and phone calls on speaker.

**Status: 0.1, in development.** It runs every day on one Mac. It isn't ready to install on yours yet: see [ROADMAP.md](ROADMAP.md).

Made by [RM Marketing](https://rm-marketing.dk).

## Privacy

- Audio never leaves your Mac. Speech-to-text runs on the Mac (Whisper large-v3).
- With the AI on, the transcript text and your call plan go to Claude, on **your own** Claude account (Claude Code, or your own API key). Nothing goes to us.
- Recordings stay in a folder on your Mac.
- Recording a call is your responsibility. Tell the people you talk to that you record, and follow the rules where you work (in the EU, GDPR applies when you record for business).

## What you need

- A Mac with Apple Silicon (M1 or newer) and macOS 14.4 or newer
- About 3 GB free for the speech model (downloaded once)
- For the coach's checklist, cues and notes: [Claude Code](https://claude.com/claude-code) signed in on your Claude subscription, or an Anthropic API key. Without either, the talk meter, transcript and clock still work.

## How it is built

| Part | What it does |
|------|-------------|
| Recorder | Records your microphone and the call audio to two separate tracks while a call app holds the mic. Stops when the call ends. |
| `engine/` | The coach: reads both tracks as they grow, transcribes, runs the checklist and cues, serves the window on `http://127.0.0.1:8765`. |
| Sales Coach app | The small floating window next to your call, a menu bar item, and your Mac Calendar as the source of meetings. |

The engine's files:

- `coach.py`: the call loop, the window's server, call history, the "Bliv bedre" page
- `audio.py`: reading the tracks, the talk meter
- `speech.py`: Whisper, live and a better second pass
- `ai.py`: Claude, through Claude Code or an API key
- `prompts.py`: what the coach asks Claude
- `calendar_feed.py`: your meetings, from `calendar.json`
- `addons.py`: plug your own data in (CRM, notes, your own question bank) without changing the coach
- `window.html`: the window

## Try it on a made-up call

```bash
python3 -m venv .venv && .venv/bin/pip install -r engine/requirements.txt
.venv/bin/python tests/make_test_call.py /tmp/sc-test/call
SALES_COACH_ROOT=/tmp/sc-test SALES_COACH_DATA=/tmp/sc-test/data \
  .venv/bin/python engine/coach.py settings --name "Anna Holm" --company "Holm Rådgivning" --language da
SALES_COACH_ROOT=/tmp/sc-test SALES_COACH_DATA=/tmp/sc-test/data \
  .venv/bin/python engine/coach.py replay /tmp/sc-test/call --event-json /tmp/sc-test/call/event.json --minutes 1.3 --debrief
```

The test call is 70 seconds of the Mac's Danish voice playing both sides of a first meeting. The replay prints the talk share, the transcript, the checklist and the coach's result.

## Add-ons

An add-on is one Python file listed in the settings (`coach.py settings --addons /path/to/addon.py`). It can add context before a call (who they are, where you left off, your own questions), extra call types, rules for the AI, a calendar source, or book the next meeting with invitations. See the top of `engine/addons.py`.

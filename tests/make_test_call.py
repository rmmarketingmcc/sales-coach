#!/usr/bin/env python3
"""A made-up two-track test call, spoken by the Mac's own Danish voice, in the recorder's layout.

    python3 tests/make_test_call.py <out folder>

Writes <out>/meta.json, mic_1.caf (you), system_1.caf (them) and <out>/event.json (the calendar event), so
`coach.py replay <out> --event-json <out>/event.json` runs the whole coach (talk meter, Whisper, cues, the
result) on a call with no real person in it. Needs macOS `say` and ffmpeg.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

SR = 16000
VOICE = "Sara"
GAP = 0.7   # seconds between turns
# Anna sells bookkeeping to small builders; Jesper runs one. Nobody here is real.
SCRIPT = [
    ("me", "Hej Jesper, tak fordi du ville tage et møde. Har du tyve minutter i dag?"),
    ("them", "Ja, det har jeg. Vi har meget travlt, så det passer fint med et kort møde."),
    ("me", "Godt. Fortæl lidt om, hvordan I klarer bogføringen i dag."),
    ("them", "Min kone gør det om aftenen. Det tager vel ti timer om måneden, og vi kommer altid for sent med momsen."),
    ("me", "Hvad koster det jer, når momsen kommer for sent?"),
    ("them", "Sidste år fik vi to bøder på omkring tre tusinde kroner. Og hun er træt af det."),
    ("me", "Hvem bestemmer, hvis I skal have hjælp udefra?"),
    ("them", "Det gør jeg sammen med hende. Vi har snakket om det siden sommeren."),
    ("me", "Hvad har I prøvet før?"),
    ("them", "Vi havde en revisor i to år, men han svarede aldrig på mails, så vi stoppede."),
    ("me", "Det lyder frustrerende. Hvis vi tog bogføringen, hvad skulle der så ske, før det var en succes for jer?"),
    ("them", "At vi aldrig får en bøde igen, og at hun får sine aftener tilbage."),
    ("me", "Skal vi sætte et nyt møde på torsdag klokken ti, hvor jeg viser et konkret forslag?"),
    ("them", "Ja, torsdag klokken ti passer. Send gerne en invitation."),
]


def speak(text: str, tmp: Path) -> np.ndarray:
    aiff = tmp / "line.aiff"
    subprocess.run(["say", "-v", VOICE, "-o", str(aiff), text], check=True)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(aiff), "-f", "s16le", "-ac", "1", "-ar", str(SR), "-"],
                         check=True, capture_output=True).stdout
    return np.frombuffer(raw, "<i2")


def write_caf(samples: np.ndarray, path: Path) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "-",
                    "-c:a", "pcm_s16le", "-f", "caf", str(path)], input=samples.tobytes(), check=True)


def main() -> None:
    out = Path(sys.argv[1]).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    tracks = {"me": [], "them": []}
    with tempfile.TemporaryDirectory() as tmp:
        for who, text in SCRIPT:
            x = speak(text, Path(tmp))
            gap = np.zeros(int(GAP * SR), "<i2")
            other = "them" if who == "me" else "me"
            tracks[who] += [x, gap]
            tracks[other] += [np.zeros(len(x), "<i2"), gap]
    mic, system = np.concatenate(tracks["me"]), np.concatenate(tracks["them"])
    write_caf(mic, out / "mic_1.caf")
    write_caf(system, out / "system_1.caf")
    started = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    meta = {"started": started.isoformat().replace("+00:00", "Z"), "status": "done", "trigger": "test",
            "duration_s": round(len(mic) / SR),
            "segments": [{"track": "mic", "file": "mic_1.caf", "offset": 0}, {"track": "system", "file": "system_1.caf", "offset": 0}]}
    (out / "meta.json").write_text(json.dumps(meta, indent=1))
    event = {"id": "test-call", "title": "Intro: Byg & Bad og Holm Rådgivning",
             "start": started.isoformat(), "end": (started + timedelta(minutes=20)).isoformat(),
             "notes": "Første snak om bogføring for Byg & Bad.", "meet": "https://meet.google.com/abc-defg-hij",
             "attendees": [{"email": "anna@holmraadgivning.dk", "name": "Anna Holm", "self": True, "status": "accepted"},
                           {"email": "jesper@bygogbad.dk", "name": "Jesper Bak", "status": "accepted"}]}
    (out / "event.json").write_text(json.dumps(event, ensure_ascii=False, indent=1))
    print(f"{out}: {meta['duration_s']} s, {len(SCRIPT)} lines")


if __name__ == "__main__":
    main()

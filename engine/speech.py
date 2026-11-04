"""Speech-to-text on the Mac: Whisper large-v3, live and a second, better pass.

Live: MLX Whisper on the Mac's GPU (40 s of audio in ~1.6 s, 3 s in ~0.25 s), so a line shows about
1.5 s after it is said. large-v3-turbo was faster but invented text on short windows.
Second pass: faster-whisper large-v3 on the CPU with beam 5, every ~30 s over the last stretch; it
replaces the quick lines (MLX decodes greedily, which cost ~10 points of accuracy on Danish).
Without MLX the CPU model does everything, locked-in lines only.
"""
from __future__ import annotations

import os
import queue
import re
import threading

import numpy as np

from audio import SR

MLX_REPO = os.environ.get("SALES_COACH_MLX", "mlx-community/whisper-large-v3-mlx")
CPU_MODEL = "large-v3"
BEAM = 5
# Whisper keeps only the last ~223 tokens of initial_prompt; ~600 characters stays inside that.
PROMPT_MAX_CHARS = 600
# Whisper's classic Danish/English hallucinations on silence or music.
HALLUCINATIONS = re.compile(
    r"(tak fordi du så med|tak for at (?:du )?s[åe] med|vi ses i næste video|undertekster af|danske tekster|"
    r"www\.|abonnér|thanks for watching|subtitles by)", re.I)
GENERIC_MAILBOXES = {"info", "kontakt", "hej", "mail", "post", "admin", "support", "noreply", "no-reply", "office"}

_cpu_model = None
_cpu_lock = threading.Lock()


def cpu_model():
    global _cpu_model
    with _cpu_lock:
        if _cpu_model is None:
            from faster_whisper import WhisperModel
            _cpu_model = WhisperModel(CPU_MODEL, device="cpu", compute_type="int8",
                                      cpu_threads=max(4, (os.cpu_count() or 8) // 2))
    return _cpu_model


def names_from(attendees: list[str]) -> list[str]:
    """Guest names, and first names from addresses (emilie.hansen@ -> Emilie Hansen)."""
    out = []
    for a in attendees or []:
        a = (a or "").strip()
        if "@" not in a:
            if a:
                out.append(a)
            continue
        local = a.lower().split("@", 1)[0]
        if local in GENERIC_MAILBOXES or len(local) < 3:
            continue
        out.append(" ".join(p.capitalize() for p in re.split(r"[._-]+", local) if p.isalpha()))
    return out


def prompt(words: list[str], names: list[str]) -> str:
    """Names and spellings only (a descriptive sentence gets parroted back over silence). Whisper
    keeps the END of the prompt, so your general words go first and this call's names last."""
    tail = [n for n in names if n]
    head = [w for w in words or [] if w and w.lower() not in {t.lower() for t in tail}]
    while head and len(", ".join(head + tail)) > PROMPT_MAX_CHARS:
        head.pop()
    text = ", ".join(head + tail)
    return text + "." if text else ""


class Transcriber(threading.Thread):
    """One Whisper model for both sides. Locked-in utterances first, oldest first; between them the
    newest live re-transcription per side (an older one is stale the moment a newer one exists)."""

    def __init__(self, session):
        super().__init__(daemon=True)
        self.session = session
        self.finals: list = []
        self.partials: dict[str, tuple] = {}
        self.cv = threading.Condition()
        self.stopping = False
        self.backend: str | None = None
        self.model = None

    def put(self, kind: str, who: str, t0: float, audio: np.ndarray) -> None:
        with self.cv:
            if kind == "final":
                self.finals.append((who, t0, audio))
                if self.partials.get(who, (None,))[0] == t0:
                    self.partials.pop(who)   # its live version is pointless now
            elif self.backend != "cpu":
                self.partials[who] = (t0, audio)
            self.cv.notify()

    def stop(self) -> None:
        with self.cv:
            self.stopping = True
            self.cv.notify()

    def _load(self) -> None:
        try:
            import mlx_whisper
            mlx_whisper.transcribe(np.zeros(SR, np.float32), path_or_hf_repo=MLX_REPO, language="da", verbose=None)
            self.model, self.backend = mlx_whisper, "mlx"
        except Exception as e:  # noqa: BLE001
            self.session.note(f"MLX Whisper unavailable ({e}); CPU Whisper, locked-in lines only")
            self.model, self.backend = cpu_model(), "cpu"

    def _asr(self, audio: np.ndarray, lang: str | None, prompt_text: str | None) -> tuple[list[tuple], str | None]:
        """[(start, end, text, no_speech_prob, avg_logprob)], detected language."""
        if self.backend == "mlx":
            r = self.model.transcribe(audio, path_or_hf_repo=MLX_REPO, language=lang, initial_prompt=prompt_text or None,
                                      condition_on_previous_text=False, verbose=None)
            return ([(s["start"], s["end"], s["text"], s.get("no_speech_prob", 0.0), s.get("avg_logprob", 0.0))
                     for s in r.get("segments") or []], r.get("language"))
        segs, info = self.model.transcribe(audio, language=lang, initial_prompt=prompt_text or None, vad_filter=True,
                                           condition_on_previous_text=False, beam_size=BEAM)
        return [(s.start, s.end, s.text, s.no_speech_prob, s.avg_logprob) for s in segs], info.language

    def run(self) -> None:
        try:
            self._load()
        except Exception as e:  # noqa: BLE001
            self.session.note(f"Whisper not loaded: {e}")
            return
        turn = 0
        while True:
            with self.cv:
                while not self.finals and not self.partials and not self.stopping:
                    self.cv.wait()
                if self.finals:
                    kind, (who, t0, audio) = "final", self.finals.pop(0)
                elif self.partials and not self.stopping:
                    turn += 1
                    who = sorted(self.partials)[turn % len(self.partials)]
                    kind, (t0, audio) = "partial", self.partials.pop(who)
                else:
                    return   # stopping and nothing left
            s = self.session
            try:
                detect = s.lang is None and who == "them" and kind == "final" and len(audio) >= 4 * SR
                segs, lang = self._asr(audio, None if detect else (s.lang or s.default_lang),
                                       s.whisper_prompt() if kind == "final" else None)
                if detect:   # the other side decides the call's language
                    s.lang = "en" if lang == "en" else s.default_lang
            except Exception as e:  # noqa: BLE001
                s.note(f"transcribe failed: {e}")
                continue
            lines = s.keep_lines(who, t0, len(audio) / SR, segs)
            if kind == "final":
                s.add_lines(lines, utt=(who, t0), done_at=t0 + len(audio) / SR)
            else:
                s.set_partial(who, t0, " ".join(l["text"] for l in lines))


class Consolidator(threading.Thread):
    """The second pass on the CPU (beam 5). Not urgent, so it never holds up the live lines, and the
    quick notes don't wait for it."""

    def __init__(self, session):
        super().__init__(daemon=True)
        self.session = session
        self.q: queue.Queue = queue.Queue()

    def run(self) -> None:
        try:
            model = cpu_model()
        except Exception as e:  # noqa: BLE001
            self.session.note(f"second pass off: {e}")
            return
        while (item := self.q.get()) is not None:
            who, t0, audio = item
            s = self.session
            try:
                segs, _ = model.transcribe(audio, language=s.lang or s.default_lang, initial_prompt=s.whisper_prompt() or None,
                                           vad_filter=True, condition_on_previous_text=False, beam_size=BEAM)
                segs = [(x.start, x.end, x.text, x.no_speech_prob, x.avg_logprob) for x in segs]
            except Exception as e:  # noqa: BLE001
                s.note(f"second pass failed: {e}")
                continue
            s.replace_lines(who, t0, t0 + len(audio) / SR, s.keep_lines(who, t0, len(audio) / SR, segs))

"""The two sides of a call, read live from the recorder's tracks.

The recorder writes your microphone and the call audio (the other side) to separate 16-bit CAF
files while the call runs. This reads both as they grow: 100 ms levels for the talk meter, and
utterances (start, audio) for speech-to-text. Nothing here leaves the Mac.
"""
from __future__ import annotations

import math
import struct
from pathlib import Path

import numpy as np

SR = 16000
FRAME = SR // 10                 # 100 ms
SPEECH_RMS = 0.005               # ~-46 dBFS and up is someone talking
HANGOVER = 3                     # frames either side that still count as talking (syllable gaps)
BREAK_TURN = 15                  # 1.5 s of the other side talking ends your stretch ("mm", "ja" don't)
END_TURN = 30                    # 3 s of your own silence ends your stretch
END_SILENCE = 7                  # frames (0.7 s) of silence lock an utterance in
MAX_UTT_S = 20                   # a longer utterance is locked in anyway, at its latest short pause
PARTIAL_EVERY = 0.8              # seconds between live re-transcriptions of an open utterance
PRE_ROLL = 0.3                   # seconds kept before speech starts (Whisper clips the first word otherwise)
# Short utterances lose context, so every ~30 s the last stretch of each side is transcribed again
# in one piece and replaces its quick lines: the window gets text in ~1.5 s, the notes the better version.
CONSOLIDATE_S = 30
# A phone call on speaker: both voices are on the mic and the call-audio track is silent. You sit at
# the Mac and the phone's speaker is further away, so speech under this share of your level is them.
PHONE_SPLIT = 0.5


def caf_layout(path: Path) -> dict | None:
    """Sample rate, channels and where the samples start in a 16-bit LPCM CAF.
    The recorder's data chunk says size -1 until the file is closed, so read to EOF instead."""
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"caff":
                return None
            f.seek(8)
            desc = None
            while True:
                h = f.read(12)
                if len(h) < 12:
                    return None
                kind, size = h[:4], struct.unpack(">q", h[4:])[0]
                pos = f.tell()
                if kind == b"desc":
                    sr, fid, flags, bpp, _fpp, ch, bits = struct.unpack(">d4sIIIII", f.read(32))
                    desc = (int(sr), fid, flags, bpp, ch, bits)
                    f.seek(pos + size)
                elif kind == b"data":
                    if not desc or desc[1] != b"lpcm" or desc[5] != 16 or not desc[2] & 2:
                        return None  # only little-endian 16-bit integer PCM, what pcmFileSettings writes
                    return {"sr": desc[0], "ch": desc[4], "bpf": desc[3], "data": pos + 4}  # 4 = edit count
                else:
                    if size < 0:
                        return None
                    f.seek(pos + size)
    except OSError:
        return None


class CafTail:
    """Reads a CAF track while the recorder is still writing it, as mono 16 kHz float32."""

    def __init__(self, path: Path, offset: float):
        self.path, self.offset = path, offset
        self.layout = None
        self.pos = 0
        self.frames_read = 0

    def read(self, max_seconds: float | None = None) -> np.ndarray:
        if self.layout is None:
            self.layout = caf_layout(self.path)
            if self.layout is None:
                return np.zeros(0, np.float32)
            self.pos = self.layout["data"]
        sr, ch, bpf = self.layout["sr"], self.layout["ch"], self.layout["bpf"]
        step = sr // math.gcd(sr, SR)  # read whole resampling blocks so no sample is lost between reads
        try:
            avail = (self.path.stat().st_size - self.pos) // bpf
        except OSError:
            return np.zeros(0, np.float32)
        if max_seconds is not None:
            avail = min(avail, max(0, int(max_seconds * sr) - self.frames_read))
        n = (avail // step) * step
        if n <= 0:
            return np.zeros(0, np.float32)
        with open(self.path, "rb") as f:
            f.seek(self.pos)
            raw = f.read(n * bpf)
        n = len(raw) // bpf // step * step
        self.pos += n * bpf
        self.frames_read += n
        x = np.frombuffer(raw[:n * bpf], "<i2").reshape(-1, ch).mean(axis=1).astype(np.float32) / 32768.0
        if sr == SR:
            return x
        if sr % SR == 0:
            k = sr // SR
            return x.reshape(-1, k).mean(axis=1)
        out_n = n * SR // sr
        return np.interp(np.arange(out_n) * (sr / SR), np.arange(n), x).astype(np.float32)

    @property
    def seconds(self) -> float:
        return self.frames_read / self.layout["sr"] if self.layout else 0.0


class Track:
    """One side of the call across the recorder's segments: 100 ms levels + audio not yet transcribed."""

    def __init__(self, name: str):
        self.name = name
        self.tail: CafTail | None = None
        self.files: set[str] = set()
        self.rms: list[float] = []      # one per 100 ms frame on the recording's clock
        self.carry = np.zeros(0, np.float32)
        self.buf = np.zeros(0, np.float32)   # audio from buf_t0 on: the open utterance plus a little before
        self.buf_t0 = 0.0
        self.floor = SPEECH_RMS
        self.k = 0                           # next frame the utterance detector looks at
        self.utt_start: float | None = None  # open utterance (seconds), None between utterances
        self.silence = 0
        self.last_partial = 0.0              # utterance length at the last live re-transcription
        self.cbuf = np.zeros(0, np.float32)  # audio since cons_t0, for the 30 s re-transcription
        self.cons_t0 = 0.0
        self.end_silence = END_SILENCE       # a practice call can lock lines in sooner
        self.last_voice = 0.0                # end of the last frame above the floor (recording clock)

    def sync_segments(self, folder: Path, segments: list[dict]) -> list[tuple[str, float, np.ndarray]]:
        """Switch to a new segment when the recorder rolls one. Returns what was open, locked in."""
        mine = [s for s in segments if s.get("track") == self.name and (folder / s["file"]).exists()]
        if not mine or mine[-1]["file"] in self.files:
            return []
        seg = mine[-1]
        self.files.add(seg["file"])
        leftover = self.flush() if self.tail else []
        self.tail = CafTail(folder / seg["file"], float(seg.get("offset") or 0))
        start_frame = int(round(self.tail.offset * 10))
        if len(self.rms) < start_frame:
            self.rms += [0.0] * (start_frame - len(self.rms))
        self._reset(len(self.rms))
        return leftover

    def _reset(self, frame: int) -> None:
        self.k = frame
        self.buf, self.buf_t0 = np.zeros(0, np.float32), frame / 10
        self.cbuf, self.cons_t0 = np.zeros(0, np.float32), frame / 10
        self.carry = np.zeros(0, np.float32)
        self.utt_start, self.silence, self.last_partial = None, 0, 0.0

    def skip_to(self, seconds: float) -> None:
        """Replay from minute N: start reading there, as if the call began then."""
        if not self.tail:
            return
        if self.tail.layout is None:
            self.tail.layout = caf_layout(self.tail.path)
            if self.tail.layout is None:
                return
        lay = self.tail.layout
        frames = max(0, int((seconds - self.tail.offset) * lay["sr"]))
        frames -= frames % (lay["sr"] // math.gcd(lay["sr"], SR))
        self.tail.pos = lay["data"] + frames * lay["bpf"]
        self.tail.frames_read = frames
        self.rms = [0.0] * int(round((self.tail.offset + frames / lay["sr"]) * 10))
        self._reset(len(self.rms))

    def pull(self, until: float | None) -> int:
        """Read new audio (up to `until` seconds on the recording clock). Returns new frames."""
        if not self.tail:
            return 0
        limit = None if until is None else max(0.0, until - self.tail.offset)
        x = self.tail.read(limit)
        if not x.size:
            return 0
        self.buf = np.concatenate([self.buf, x])
        self.cbuf = np.concatenate([self.cbuf, x])
        x = np.concatenate([self.carry, x])
        n = len(x) // FRAME
        self.carry = x[n * FRAME:]
        if n:
            self.rms += np.sqrt(np.mean(x[:n * FRAME].reshape(n, FRAME) ** 2, axis=1)).tolist()
            recent = np.array(self.rms[-600:])
            # Room noise / hum sets its own floor; a speaking voice is well above it.
            self.floor = max(SPEECH_RMS, 3 * float(np.percentile(recent, 20)))
        return n

    def level(self) -> float:
        """Typical level while this side talks (75th percentile of active frames in the last 5 min)."""
        recent = np.array(self.rms[-3000:])
        active = recent[recent > self.floor]
        return float(np.percentile(active, 75)) if len(active) >= 20 else self.floor * 4

    def window_level(self, start: float, end: float) -> float:
        """RMS between two seconds, scaled so this side's speaking level is 0.1."""
        frames = np.array(self.rms[int(start * 10):max(int(start * 10) + 1, int(np.ceil(end * 10)))])
        if not frames.size:
            return 0.0
        return float(np.sqrt(np.mean(frames ** 2))) * 0.1 / max(self.level(), 1e-4)

    def _audio(self, a: float, b: float) -> np.ndarray:
        return self.buf[max(0, int((a - self.buf_t0) * SR)):max(0, int((b - self.buf_t0) * SR))]

    def _final(self, a: float, b: float) -> tuple[float, np.ndarray] | None:
        """Lock in [a, b): the audio for Whisper, or None when it holds under 0.3 s of speech
        (Whisper invents text on near-silence)."""
        active = sum(1 for r in self.rms[int(a * 10):int(b * 10)] if r > self.floor)
        audio = self._audio(a, b)
        return (a, audio) if active >= 3 and len(audio) >= SR // 4 else None

    def take_jobs(self, live: bool) -> list[tuple[str, float, np.ndarray]]:
        """Utterances from the new frames: ("final", start, audio) when one ends at a 0.7 s pause (or
        runs past MAX_UTT_S), and with live=True one ("partial", start, audio) of the open utterance
        every PARTIAL_EVERY s, for the grey live line."""
        jobs = []
        n = len(self.rms)
        for f in range(self.k, n):
            active = self.rms[f] > self.floor
            if active:
                self.last_voice = (f + 1) / 10
            if self.utt_start is None:
                if active:
                    self.utt_start = max(self.buf_t0, f / 10 - PRE_ROLL)
                    self.silence, self.last_partial = 0, 0.0
                continue
            self.silence = 0 if active else self.silence + 1
            if self.silence >= self.end_silence:
                end = (f - self.silence + 3) / 10              # 0.2 s of tail after the last word
                job = self._final(self.utt_start, end)
                if job:
                    jobs.append(("final", *job))
                self.utt_start = None
            elif f / 10 - self.utt_start >= MAX_UTT_S:
                # split at the latest short pause in the last 5 s, else here
                cut = next((g for g in range(f, max(int(self.utt_start * 10), f - 50), -1)
                            if all(r <= self.floor for r in self.rms[g - 3:g])), f) / 10
                job = self._final(self.utt_start, cut)
                if job:
                    jobs.append(("final", *job))
                self.utt_start, self.last_partial = cut, 0.0
        self.k = n
        now = n / 10
        # between utterances (never inside one, or its lines would come twice), once CONSOLIDATE_S has
        # passed: the whole stretch again, in one piece
        if self.utt_start is None and now - self.cons_t0 >= CONSOLIDATE_S:
            job = self._consolidate(now)
            if job:
                jobs.append(("consolidate", *job))
        if live and self.utt_start is not None:
            length = now - self.utt_start
            if length >= 0.6 and length - self.last_partial >= PARTIAL_EVERY:
                self.last_partial = length
                jobs.append(("partial", self.utt_start, self._audio(self.utt_start, now)))
        # keep only the open utterance (or a pre-roll) in memory
        keep_from = self.utt_start if self.utt_start is not None else now - PRE_ROLL
        drop = int((keep_from - self.buf_t0) * SR)
        if drop > 0:
            self.buf, self.buf_t0 = self.buf[drop:], self.buf_t0 + drop / SR
        return jobs

    def _consolidate(self, cut: float) -> tuple[float, np.ndarray] | None:
        """[cons_t0, cut) for the second, better pass; None when it held no speech."""
        a = self.cons_t0
        n = max(0, int((cut - a) * SR))
        audio, self.cbuf, self.cons_t0 = self.cbuf[:n], self.cbuf[n:], cut
        active = sum(1 for r in self.rms[int(a * 10):int(cut * 10)] if r > self.floor)
        return (a, audio) if active >= 5 else None

    def flush(self) -> list[tuple[str, float, np.ndarray]]:
        """The call ended or the segment rolled: lock in everything still open, and the last stretch."""
        jobs = [j for j in self.take_jobs(live=False) if j[0] != "partial"]
        end = len(self.rms) / 10
        if self.utt_start is not None:
            job = self._final(self.utt_start, end)
            if job:
                jobs.append(("final", *job))
            self.utt_start = None
        job = self._consolidate(end)
        if job:
            jobs.append(("consolidate", *job))
        return jobs


class TalkMeter:
    """Who talks, frame by frame: mic = you, call audio = them. Mic bleed from speakers is ignored."""

    def __init__(self):
        self.i = 0
        self.me = np.zeros(0, bool)
        self.them = np.zeros(0, bool)
        self.my_stretch_start: int | None = None
        self.their_stretch_start: int | None = None
        self.me_run = self.them_run = 0
        self.my_silence = self.their_silence = 0
        self.my_longest = 0
        self.their_longest = 0
        self.phone = False   # phone call on speaker: both sides on the mic, split by loudness

    def update(self, mic: Track, sys_: Track, upto: int) -> None:
        if upto <= self.i:
            return
        lvl_m = mic.level()
        raw_m = np.array(mic.rms[self.i:upto] + [0.0] * max(0, upto - len(mic.rms)))[:upto - self.i]
        raw_s = np.array(sys_.rms[self.i:upto] + [0.0] * max(0, upto - len(sys_.rms)))[:upto - self.i]
        if self.phone:
            # 0.5 s average, so one quiet syllable of yours doesn't count as the other side
            lo = max(0, self.i - 2)
            ctx = np.array(mic.rms[lo:upto + 2] + [0.0] * max(0, upto + 2 - len(mic.rms)))[:upto + 2 - lo]
            avg = np.convolve(ctx, np.ones(5) / 5, mode="same")[self.i - lo:self.i - lo + (upto - self.i)]
            talking = raw_m > mic.floor
            act_m = talking & (avg >= PHONE_SPLIT * lvl_m)
            act_s = talking & ~act_m
        else:
            act_s = raw_s > sys_.floor
            # With speakers the mic also hears them, but far quieter than your own voice.
            act_m = (raw_m > mic.floor) & ~(act_s & (raw_m < 0.35 * lvl_m))
        self.me = np.concatenate([self.me, act_m])
        self.them = np.concatenate([self.them, act_s])
        for k in range(self.i, upto):
            me = self._smooth(self.me, k)
            them = self._smooth(self.them, k)
            self.me_run = self.me_run + 1 if me else 0
            self.them_run = self.them_run + 1 if them else 0
            self.my_silence = 0 if me else self.my_silence + 1
            self.their_silence = 0 if them else self.their_silence + 1
            # A stretch survives "mm"/"ja" from the other side; it ends when they talk 1.5 s or you stop 3 s.
            if self.them_run >= BREAK_TURN or self.my_silence >= END_TURN:
                self.my_stretch_start = None
            elif me and self.my_stretch_start is None:
                self.my_stretch_start = k
            if self.me_run >= BREAK_TURN or self.their_silence >= END_TURN:
                self.their_stretch_start = None
            elif them and self.their_stretch_start is None:
                self.their_stretch_start = k
            if self.my_stretch_start is not None:
                self.my_longest = max(self.my_longest, k - self.my_stretch_start)
            if self.their_stretch_start is not None:
                self.their_longest = max(self.their_longest, k - self.their_stretch_start)
        self.i = upto

    def restart_at(self, frame: int, mic: Track, sys_: Track) -> None:
        """Count only from frame on: the next call in the same recording. The frames before are the call
        before's, whose talk share and longest stretches would otherwise carry over."""
        upto, phone = self.i, self.phone
        self.__init__()
        self.phone, self.i = phone, frame
        self.me, self.them = np.zeros(frame, bool), np.zeros(frame, bool)
        self.update(mic, sys_, upto)

    @staticmethod
    def _smooth(arr: np.ndarray, k: int) -> bool:
        return bool(arr[max(0, k - HANGOVER):k + HANGOVER + 1].any())

    def seconds(self, last: int | None = None) -> tuple[float, float]:
        me, them = (self.me[-last:], self.them[-last:]) if last else (self.me, self.them)
        return float(me.sum()) / 10, float(them.sum()) / 10

    def share(self, last: int | None = None) -> float | None:
        me, them = self.seconds(last)
        return round(100 * me / (me + them)) if me + them >= 20 else None

    def my_stretch(self) -> float:
        return (self.i - self.my_stretch_start) / 10 if self.my_stretch_start is not None else 0.0


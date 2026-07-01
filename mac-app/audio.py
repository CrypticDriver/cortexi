"""
Audio capture + whisper.cpp transcription worker (Mac).

Captures from an aggregate device (BlackHole + mic) via ffmpeg avfoundation,
chunks into N-second WAV segments, runs whisper.cpp locally, and calls a
callback with each transcript segment. Audio never leaves the machine.
"""
import os
import time
import queue
import shlex
import threading
import subprocess
import tempfile
from pathlib import Path


def _expand(p: str) -> str:
    return os.path.expanduser(p)


class Transcriber:
    def __init__(self, cfg: dict, on_segment):
        w = cfg["whisper"]
        self.whisper_cli = _expand(w["whisper_cli"])
        self.model_path = _expand(w["model_path"])
        self.language = w.get("language", "zh")
        self.segment_seconds = int(w.get("segment_seconds", 45))
        self.capture_device = cfg["audio"]["capture_device"]
        self.sample_rate = int(cfg["audio"].get("sample_rate", 16000))
        self.on_segment = on_segment
        self._stop = threading.Event()
        self._threads = []
        self._workdir = Path(tempfile.mkdtemp(prefix="mc-audio-"))
        self._last_text = ""          # for cross-segment dedup
        self.silence_rms = float(cfg.get("audio", {}).get("silence_rms", 0.006))

    # ---- ffmpeg device index lookup ----
    def _find_device_index(self) -> str:
        """Return avfoundation audio device index matching capture_device name."""
        try:
            out = subprocess.run(
                ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                capture_output=True, text=True, timeout=15,
            ).stderr
        except Exception:
            return "0"
        in_audio = False
        for line in out.splitlines():
            if "AVFoundation audio devices" in line:
                in_audio = True
                continue
            if in_audio:
                # line like:  [AVFoundation ...] [1] MeetingCopilot-Aggregate
                if self.capture_device in line:
                    lb = line.rfind("[")
                    rb = line.rfind("]")
                    if lb != -1 and rb != -1 and rb > lb:
                        return line[lb + 1:rb]
        return "0"

    def start(self):
        self._stop.clear()
        t = threading.Thread(target=self._record_loop, daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self):
        self._stop.set()

    def _record_loop(self):
        idx = self._find_device_index()
        seg = 0
        while not self._stop.is_set():
            seg += 1
            wav = self._workdir / f"seg_{seg:05d}.wav"
            # record one segment with ffmpeg (avfoundation audio-only)
            cmd = (
                f'ffmpeg -y -f avfoundation -i ":{idx}" '
                f'-t {self.segment_seconds} -ac 1 -ar {self.sample_rate} '
                f'-loglevel error "{wav}"'
            )
            try:
                subprocess.run(shlex.split(cmd), timeout=self.segment_seconds + 20)
            except Exception:
                if self._stop.is_set():
                    break
                time.sleep(1)
                continue
            if self._stop.is_set():
                break
            # transcribe in a separate thread so recording continues
            threading.Thread(
                target=self._transcribe, args=(wav,), daemon=True
            ).start()

    @staticmethod
    def _wav_rms(wav: Path) -> float:
        """Mean amplitude (0..1) of a 16-bit mono WAV; no audioop (gone in 3.13)."""
        try:
            import wave, array, math
            with wave.open(str(wav), "rb") as w:
                if w.getsampwidth() != 2:
                    return 1.0
                frames = w.readframes(w.getnframes())
            if not frames:
                return 0.0
            a = array.array("h")
            a.frombytes(frames)
            if not len(a):
                return 0.0
            s2 = 0.0
            for v in a:
                s2 += v * v
            return math.sqrt(s2 / len(a)) / 32768.0
        except Exception:
            return 1.0  # on error, don't gate it out

    @staticmethod
    def _looks_hallucinated(text: str) -> bool:
        """Detect whisper silence-hallucination: heavy repetition / low diversity."""
        t = text.strip()
        if len(t) < 4:
            return True
        # collapse whitespace/punct for analysis
        core = "".join(ch for ch in t if ch.isalnum())
        if len(core) < 3:
            return True
        # unique-char ratio too low => "吃了吃了吃了" style loop
        uniq = len(set(core)) / max(1, len(core))
        if len(core) >= 8 and uniq < 0.25:
            return True
        # a single short token repeated many times
        import re
        tokens = re.split(r"[\s,，。.!?！？…]+", t)
        tokens = [x for x in tokens if x]
        if len(tokens) >= 4:
            most = max(set(tokens), key=tokens.count)
            if tokens.count(most) / len(tokens) > 0.6:
                return True
        # repeated short substring (e.g. "吃了" appearing many times) dominating text
        for n in (2, 3):
            if len(core) >= n * 4:
                counts = {}
                for i in range(len(core) - n + 1):
                    g = core[i:i + n]
                    counts[g] = counts.get(g, 0) + 1
                top = max(counts.values())
                # if one n-gram covers a large share of the text -> loop hallucination
                if top * n / len(core) > 0.5:
                    return True
        return False

    def _transcribe(self, wav: Path):
        if not wav.exists() or wav.stat().st_size < 2000:
            return
        # --- silence gate: skip near-silent segments (kills whisper hallucination) ---
        if self._wav_rms(wav) < self.silence_rms:
            try:
                wav.unlink(missing_ok=True)
            except Exception:
                pass
            return
        out_prefix = str(wav.with_suffix(""))
        cmd = [
            self.whisper_cli,
            "-m", self.model_path,
            "-f", str(wav),
            "-l", self.language,
            "-otxt",
            "-of", out_prefix,
            "-np", "-nt",
            "-mc", "0",                 # max-context 0: no cross-segment carry => no repeat snowball
            "-t", "4",
            "-tp", "0.0",               # temperature 0: deterministic, less drift
            "-et", "2.8",               # entropy threshold: bail on gibberish
            "-nth", "0.6",              # no-speech threshold: treat low-conf as silence
        ]
        ran_ok = False
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            ran_ok = (r.returncode == 0)
        except Exception:
            ran_ok = False
        txt_file = Path(out_prefix + ".txt")
        # fallback: if the tuned flags aren't supported by this whisper build
        # (nonzero exit / no output), retry with the minimal, always-supported set
        if not ran_ok or not txt_file.exists():
            minimal = [
                self.whisper_cli, "-m", self.model_path, "-f", str(wav),
                "-l", self.language, "-otxt", "-of", out_prefix, "-np", "-nt",
            ]
            try:
                subprocess.run(minimal, capture_output=True, text=True, timeout=300)
            except Exception:
                return
        if txt_file.exists():
            text = txt_file.read_text().strip()
            cleaned = text.replace("[BLANK_AUDIO]", "").replace("(silence)", "").strip()
            # drop hallucinations & exact repeats of the previous segment
            if (cleaned and len(cleaned) > 1
                    and not self._looks_hallucinated(cleaned)
                    and cleaned != self._last_text):
                self._last_text = cleaned
                try:
                    self.on_segment(cleaned)
                finally:
                    pass
        # cleanup
        try:
            wav.unlink(missing_ok=True)
            Path(out_prefix + ".txt").unlink(missing_ok=True)
        except Exception:
            pass

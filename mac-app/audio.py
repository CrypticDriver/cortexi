"""
Audio capture + whisper.cpp transcription worker (Mac).

Dual-channel speaker separation:
  Records the aggregate device in STEREO, where by convention
    left  channel  = system audio (the OTHER party, via BlackHole)
    right channel  = your microphone (YOU)
  Each channel is split out and transcribed independently, so every
  transcript segment carries a speaker label ("对方" / "我").
  Falls back to mono (no speaker label) when audio.mode != "dual".

Audio never leaves the machine; only transcribed text is sent upstream.
"""
import os
import re
import time
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
        audio = cfg.get("audio", {})
        self.capture_device = audio["capture_device"]
        self.sample_rate = int(audio.get("sample_rate", 16000))
        # "dual" => stereo split into 对方(L)/我(R); "mono" => single mixed track
        self.mode = audio.get("mode", "dual")
        # left=对方 by default; flip with audio.left_is_me = true if wired oppositely
        self.left_is_me = bool(audio.get("left_is_me", False))
        self.silence_rms = float(audio.get("silence_rms", 0.006))
        # on_segment(text) or on_segment(text, speaker=...)
        self.on_segment = on_segment
        self._stop = threading.Event()
        self._threads = []
        self._workdir = Path(tempfile.mkdtemp(prefix="mc-audio-"))
        self._last = {"对方": "", "我": "", "": ""}  # per-speaker dedup

    # ---- ffmpeg device index lookup ----
    def _find_device_index(self) -> str:
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
            if in_audio and self.capture_device in line:
                lb, rb = line.rfind("["), line.rfind("]")
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

    def _emit(self, text, speaker):
        """Call on_segment, tolerating both 1-arg and 2-arg callbacks."""
        try:
            self.on_segment(text, speaker)
        except TypeError:
            label = f"【{speaker}】" if speaker else ""
            self.on_segment(label + text)

    def _record_loop(self):
        idx = self._find_device_index()
        channels = 2 if self.mode == "dual" else 1
        seg = 0
        while not self._stop.is_set():
            seg += 1
            wav = self._workdir / f"seg_{seg:05d}.wav"
            cmd = (
                f'ffmpeg -y -f avfoundation -i ":{idx}" '
                f'-t {self.segment_seconds} -ac {channels} -ar {self.sample_rate} '
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
            threading.Thread(target=self._process_segment, args=(wav,), daemon=True).start()

    # ---- per-segment processing ----
    def _process_segment(self, wav: Path):
        if not wav.exists() or wav.stat().st_size < 2000:
            return
        try:
            if self.mode == "dual":
                left = wav.with_name(wav.stem + "_L.wav")
                right = wav.with_name(wav.stem + "_R.wav")
                # split stereo into two mono tracks
                self._split_stereo(wav, left, right)
                me_label, other_label = "我", "对方"
                left_spk = me_label if self.left_is_me else other_label
                right_spk = other_label if self.left_is_me else me_label
                # transcribe both channels (each with its own silence gate)
                for track, spk in ((left, left_spk), (right, right_spk)):
                    self._transcribe(track, spk)
                for f in (left, right):
                    self._safe_unlink(f)
            else:
                self._transcribe(wav, "")
        finally:
            self._safe_unlink(wav)

    def _split_stereo(self, src: Path, left: Path, right: Path):
        cmd = [
            "ffmpeg", "-y", "-i", str(src),
            "-filter_complex", "[0:a]channelsplit=channel_layout=stereo[l][r]",
            "-map", "[l]", "-ac", "1", str(left),
            "-map", "[r]", "-ac", "1", str(right),
            "-loglevel", "error",
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except Exception:
            pass

    @staticmethod
    def _safe_unlink(p: Path):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass

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
            return 1.0

    @staticmethod
    def _looks_hallucinated(text: str) -> bool:
        """Detect whisper silence-hallucination: heavy repetition / low diversity."""
        t = text.strip()
        if len(t) < 4:
            return True
        core = "".join(ch for ch in t if ch.isalnum())
        if len(core) < 3:
            return True
        uniq = len(set(core)) / max(1, len(core))
        if len(core) >= 8 and uniq < 0.25:
            return True
        tokens = [x for x in re.split(r"[\s,，。.!?！？…]+", t) if x]
        if len(tokens) >= 4:
            most = max(set(tokens), key=tokens.count)
            if tokens.count(most) / len(tokens) > 0.6:
                return True
        for n in (2, 3):
            if len(core) >= n * 4:
                counts = {}
                for i in range(len(core) - n + 1):
                    g = core[i:i + n]
                    counts[g] = counts.get(g, 0) + 1
                if max(counts.values()) * n / len(core) > 0.5:
                    return True
        return False

    def _transcribe(self, wav: Path, speaker: str):
        if not wav.exists() or wav.stat().st_size < 2000:
            return
        # silence gate: skip near-silent tracks (kills whisper hallucination AND
        # gives free speaker turn-taking: whoever is quiet produces nothing)
        if self._wav_rms(wav) < self.silence_rms:
            return
        out_prefix = str(wav.with_suffix(""))
        cmd = [
            self.whisper_cli, "-m", self.model_path, "-f", str(wav),
            "-l", self.language, "-otxt", "-of", out_prefix, "-np", "-nt",
            "-mc", "0",       # no cross-segment context => no repeat snowball
            "-t", "4",
            "-tp", "0.0",     # temperature 0
            "-et", "2.8",     # entropy threshold
            "-nth", "0.6",    # no-speech threshold
        ]
        ran_ok = False
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            ran_ok = (r.returncode == 0)
        except Exception:
            ran_ok = False
        txt_file = Path(out_prefix + ".txt")
        if not ran_ok or not txt_file.exists():
            minimal = [
                self.whisper_cli, "-m", self.model_path, "-f", str(wav),
                "-l", self.language, "-otxt", "-of", out_prefix, "-np", "-nt",
            ]
            try:
                subprocess.run(minimal, capture_output=True, text=True, timeout=300)
            except Exception:
                self._safe_unlink(txt_file)
                return
        if txt_file.exists():
            text = txt_file.read_text().strip()
            cleaned = text.replace("[BLANK_AUDIO]", "").replace("(silence)", "").strip()
            if (cleaned and len(cleaned) > 1
                    and not self._looks_hallucinated(cleaned)
                    and cleaned != self._last.get(speaker, "")):
                self._last[speaker] = cleaned
                self._emit(cleaned, speaker)
        self._safe_unlink(txt_file)

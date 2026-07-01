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

    def _transcribe(self, wav: Path):
        if not wav.exists() or wav.stat().st_size < 2000:
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
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except Exception:
            return
        txt_file = Path(out_prefix + ".txt")
        if txt_file.exists():
            text = txt_file.read_text().strip()
            # whisper emits [BLANK_AUDIO] / (silence) markers; skip noise
            cleaned = text.replace("[BLANK_AUDIO]", "").strip()
            if cleaned and len(cleaned) > 1:
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

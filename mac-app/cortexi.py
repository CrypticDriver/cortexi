"""
CortexI (副脑一代) — Mac menubar app.

Menubar 🧠:
  ● 开始录音 / ■ 停止录音
  📷 上传图片/文件
  💬 打开本地界面 (localhost)
  📝 会后总结
  ⚙︎ 状态 / 退出

Local UI is served from ui/index.html on 127.0.0.1:<ui_port>, backed by a tiny
HTTP API that proxies to the remote server and exposes the live transcript.
"""
import os
import json
import threading
import webbrowser
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import rumps

from remote import RemoteClient
from audio import Transcriber

HERE = Path(__file__).resolve().parent

# Config lives in the user's home dir so it survives app upgrades and stays
# writable even when running from inside a read-only .app bundle.
CONFIG_DIR = Path.home() / ".meeting-copilot"
CONFIG_PATH = CONFIG_DIR / "config.json"


def _find_ui_dir():
    """Locate ui/ whether running as plain script or inside a .app bundle."""
    candidates = [
        HERE / "ui",                       # plain: mac-app/ui
        HERE.parent / "Resources" / "ui",  # py2app: Contents/Resources/ui
        HERE / "Resources" / "ui",
    ]
    for c in candidates:
        if (c / "index.html").exists():
            return c
    return HERE / "ui"


UI_DIR = _find_ui_dir()


DEFAULT_CONFIG = {
    "server_url": "",
    "app_token": "",
    "ui_port": 8787,
    "whisper": {
        "model_path": "~/.meeting-copilot/whisper/ggml-large-v3.bin",
        "whisper_cli": "~/.meeting-copilot/whisper.cpp/build/bin/whisper-cli",
        "language": "zh",
        "segment_seconds": 30,
    },
    "audio": {
        "capture_device": "MeetingCopilot-Aggregate",
        "sample_rate": 16000,
        "mode": "dual",
        "left_is_me": False,
        "silence_rms": 0.006,
    },
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    # one-time seed: if user config absent but a legacy ./config.json exists, adopt it
    legacy = HERE / "config.json"
    if not CONFIG_PATH.exists() and legacy.exists():
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(legacy.read_text())
        except Exception:
            pass
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text() or "{}")
        except Exception:
            user = {}
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))


class AppState:
    """Shared state between menubar, audio worker, and local UI."""
    def __init__(self):
        self.transcript = []      # list of {text, ts}
        self.files = []           # list of {name}
        self.answers = []         # list of {q, a}
        self.summary = ""
        self.recording = False
        self.meeting_title = ""
        self.lock = threading.Lock()

    def snapshot(self):
        with self.lock:
            return {
                "transcript": list(self.transcript),
                "files": list(self.files),
                "answers": list(self.answers),
                "summary": self.summary,
                "recording": self.recording,
                "title": self.meeting_title,
            }


class LocalUIHandler(BaseHTTPRequestHandler):
    state: AppState = None
    remote: RemoteClient = None
    ui_dir: Path = None
    app = None  # back-ref to MeetingCopilotApp for record/summary control

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = (self.ui_dir / "index.html").read_text()
            return self._send(200, html, "text/html; charset=utf-8")
        if self.path == "/state":
            return self._send(200, json.dumps(self.state.snapshot(), ensure_ascii=False))
        if self.path == "/config":
            cfg = self.app.cfg
            tok = cfg.get("app_token", "") or ""
            masked = (tok[:6] + "…" + tok[-4:]) if len(tok) > 12 else ("已设置" if tok else "")
            return self._send(200, json.dumps({
                "server_url": cfg.get("server_url", ""),
                "token_set": bool(tok),
                "token_masked": masked,
            }, ensure_ascii=False))
        return self._send(404, "{}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {}
        if self.path == "/ask":
            q = payload.get("question", "").strip()
            if not q:
                return self._send(400, json.dumps({"error": "empty"}))
            try:
                ans = self.remote.ask(q)
            except Exception as e:
                ans = f"[远程错误] {e}"
            with self.state.lock:
                self.state.answers.append({"q": q, "a": ans})
            return self._send(200, json.dumps({"answer": ans}, ensure_ascii=False))
        if self.path == "/record/start":
            try:
                self.app.start_recording()
                return self._send(200, json.dumps({"recording": True}, ensure_ascii=False))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False))
        if self.path == "/record/stop":
            try:
                self.app.stop_recording()
                return self._send(200, json.dumps({"recording": False}, ensure_ascii=False))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False))
        if self.path == "/summarize":
            try:
                self.app.trigger_summary()
                return self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False))
        if self.path == "/config":
            su = (payload.get("server_url") or "").strip()
            tok = (payload.get("app_token") or "").strip()
            try:
                result = self.app.update_config(su, tok)
                return self._send(200, json.dumps(result, ensure_ascii=False))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False))
        return self._send(404, "{}")


class MeetingCopilotApp(rumps.App):
    def __init__(self, cfg):
        super().__init__("🧠", quit_button=None)
        self.cfg = cfg
        self.state = AppState()
        self.remote = RemoteClient(cfg["server_url"], cfg["app_token"],
                                   cfg.get("origin_verify", ""))
        self.transcriber = None
        self.ui_port = int(cfg.get("ui_port", 8787))

        self.menu = [
            rumps.MenuItem("● 开始录音", callback=self.toggle_record),
            rumps.MenuItem("📷 上传图片/文件", callback=self.upload_file),
            rumps.MenuItem("💬 打开本地界面", callback=self.open_ui),
            None,
            rumps.MenuItem("📝 会后总结", callback=self.do_summary),
            None,
            rumps.MenuItem("⚙︎ 检查连接", callback=self.check_health),
            rumps.MenuItem("退出", callback=self.quit_app),
        ]
        self._start_ui_server()

    # ---------- UI server ----------
    def _start_ui_server(self):
        LocalUIHandler.state = self.state
        LocalUIHandler.remote = self.remote
        LocalUIHandler.ui_dir = UI_DIR
        LocalUIHandler.app = self
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.ui_port), LocalUIHandler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def _notify(self, title, subtitle, msg):
        try:
            rumps.notification(title, subtitle, msg)
        except Exception:
            pass

    def update_config(self, server_url, app_token):
        """Save server_url/token to config.json and hot-reload RemoteClient."""
        if server_url:
            self.cfg["server_url"] = server_url.rstrip("/")
        # only overwrite token if a non-empty new value is provided
        if app_token:
            self.cfg["app_token"] = app_token
        save_config(self.cfg)
        # rebuild remote client with new creds
        self.remote = RemoteClient(self.cfg["server_url"], self.cfg["app_token"],
                                   self.cfg.get("origin_verify", ""))
        LocalUIHandler.remote = self.remote
        # test connection
        if not self.cfg.get("server_url") or not self.cfg.get("app_token"):
            return {"saved": True, "connected": False, "detail": "已保存，但 server_url 或 token 为空"}
        try:
            h = self.remote.health()
            return {"saved": True, "connected": True, "detail": json.dumps(h, ensure_ascii=False)}
        except Exception as e:
            return {"saved": True, "connected": False, "detail": "连接失败：" + str(e)}

    # ---------- shared record/summary (menubar + web UI) ----------
    def start_recording(self):
        if self.state.recording:
            return
        self.remote.start(title=None)
        self.transcriber = Transcriber(self.cfg, on_segment=self._on_segment)
        self.transcriber.start()
        with self.state.lock:
            self.state.recording = True
        try:
            self.title = "🔴"
        except Exception:
            pass
        self._notify("CortexI", "开始录音", "系统音频+麦克风，本地转写中")

    def stop_recording(self):
        if not self.state.recording:
            return
        if self.transcriber:
            self.transcriber.stop()
        with self.state.lock:
            self.state.recording = False
        try:
            self.title = "🧠"
        except Exception:
            pass
        self._notify("CortexI", "已停止录音", "可点『会后总结』生成纪要")

    def trigger_summary(self):
        if not self.remote.meeting_id:
            raise RuntimeError("还没有会议，先开始录音或上传文件")
        self._notify("CortexI", "正在生成总结…", "远程 CC 分析中")

        def run():
            try:
                summary = self.remote.summarize()
            except Exception as e:
                summary = "[远程错误] %s" % e
            with self.state.lock:
                self.state.summary = summary
            self._notify("CortexI", "总结已生成", "打开本地界面查看")
        threading.Thread(target=run, daemon=True).start()

    # ---------- menubar callbacks ----------
    def toggle_record(self, sender):
        if not self.state.recording:
            try:
                self.start_recording()
            except Exception as e:
                rumps.alert("无法连接远程服务", str(e))
                return
            sender.title = "■ 停止录音"
        else:
            self.stop_recording()
            sender.title = "● 开始录音"

    def _on_segment(self, text, speaker=""):
        # called from audio worker thread (speaker: "我"/"对方"/"")
        with self.state.lock:
            self.state.transcript.append({"text": text, "speaker": speaker})
        try:
            payload = f"【{speaker}】{text}" if speaker else text
            self.remote.feed(payload)
        except Exception:
            pass

    # ---------- upload ----------
    def upload_file(self, _):
        # use AppleScript file picker (works from menubar app without extra deps)
        script = 'POSIX path of (choose file with prompt "选择要上传的图片或文件")'
        try:
            import subprocess
            path = subprocess.run(["osascript", "-e", script],
                                  capture_output=True, text=True).stdout.strip()
        except Exception:
            path = ""
        if not path:
            return
        if not self.remote.meeting_id:
            try:
                self.remote.start(title=None)
            except Exception as e:
                rumps.alert("无法连接远程服务", str(e))
                return
        try:
            res = self.remote.upload(path)
            name = os.path.basename(path)
            with self.state.lock:
                self.state.files.append({"name": name})
            note = (res or {}).get("file", {}).get("note", "")
            rumps.notification("CortexI", f"已上传 {name}", note[:120])
        except Exception as e:
            rumps.alert("上传失败", str(e))

    def open_ui(self, _):
        webbrowser.open(f"http://127.0.0.1:{self.ui_port}/")

    def do_summary(self, _):
        if not self.remote.meeting_id:
            rumps.alert("还没有会议", "先开始录音或上传文件")
            return
        rumps.notification("CortexI", "正在生成总结…", "远程 CC 分析中")

        def run():
            try:
                summary = self.remote.summarize()
            except Exception as e:
                summary = f"[远程错误] {e}"
            with self.state.lock:
                self.state.summary = summary
            rumps.notification("CortexI", "总结已生成", "打开本地界面查看")
        threading.Thread(target=run, daemon=True).start()

    def check_health(self, _):
        try:
            h = self.remote.health()
            rumps.alert("连接正常", json.dumps(h, ensure_ascii=False))
        except Exception as e:
            rumps.alert("连接失败", str(e))

    def quit_app(self, _):
        if self.transcriber:
            self.transcriber.stop()
        rumps.quit_application()


if __name__ == "__main__":
    cfg = load_config()
    MeetingCopilotApp(cfg).run()

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
CONFIG_PATH = HERE / "config.json"


def load_config():
    if not CONFIG_PATH.exists():
        raise SystemExit("config.json 不存在，请先 cp config.example.json config.json 并填写")
    return json.loads(CONFIG_PATH.read_text())


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
        LocalUIHandler.ui_dir = HERE / "ui"
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.ui_port), LocalUIHandler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    # ---------- recording ----------
    def toggle_record(self, sender):
        if not self.state.recording:
            try:
                self.remote.start(title=None)
            except Exception as e:
                rumps.alert("无法连接远程服务", str(e))
                return
            self.transcriber = Transcriber(self.cfg, on_segment=self._on_segment)
            self.transcriber.start()
            with self.state.lock:
                self.state.recording = True
            sender.title = "■ 停止录音"
            self.title = "🔴"
            rumps.notification("CortexI", "开始录音", "系统音频+麦克风，本地转写中")
        else:
            if self.transcriber:
                self.transcriber.stop()
            with self.state.lock:
                self.state.recording = False
            sender.title = "● 开始录音"
            self.title = "🧠"
            rumps.notification("CortexI", "已停止录音", "可点『会后总结』生成纪要")

    def _on_segment(self, text):
        # called from audio worker thread
        with self.state.lock:
            self.state.transcript.append({"text": text})
        try:
            self.remote.feed(text)
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

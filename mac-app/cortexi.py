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

# Config resolution (fixed 2026-07-01): prefer the config.json sitting next to
# the source (mac-app/config.json) because that is what users naturally edit.
# Fall back to the home-dir copy only when the local one is not writable/present
# (e.g. running from inside a read-only .app bundle).
LOCAL_CONFIG_PATH = HERE / "config.json"
HOME_CONFIG_DIR = Path.home() / ".meeting-copilot"
HOME_CONFIG_PATH = HOME_CONFIG_DIR / "config.json"


def _active_config_path():
    """The config file we actually read/write.

    Rule: if a config.json exists next to the source, that one wins (matches
    user intuition). Otherwise use the home-dir copy (survives .app upgrades).
    """
    if LOCAL_CONFIG_PATH.exists():
        return LOCAL_CONFIG_PATH
    return HOME_CONFIG_PATH


# Backwards-compatible aliases (other code references these names)
CONFIG_DIR = HOME_CONFIG_DIR
CONFIG_PATH = _active_config_path()


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
        "segment_seconds": 14,
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
    path = _active_config_path()
    if path.exists():
        try:
            user = json.loads(path.read_text() or "{}")
        except Exception:
            user = {}
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def save_config(cfg):
    # Write back to whichever config is active so edits round-trip to the same
    # file the user sees. Prefer the local source-dir copy.
    path = _active_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    except Exception:
        # local dir not writable (read-only .app bundle) -> fall back to home
        HOME_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        HOME_CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))


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
            snap = self.state.snapshot()
            try:
                snap["meeting_id"] = self.remote.meeting_id
            except Exception:
                snap["meeting_id"] = None
            return self._send(200, json.dumps(snap, ensure_ascii=False))
        if self.path == "/sessions":
            try:
                items = self.remote.list_sessions()
                return self._send(200, json.dumps({
                    "sessions": items,
                    "current": self.remote.meeting_id,
                }, ensure_ascii=False))
            except Exception as e:
                return self._send(200, json.dumps({"sessions": [], "error": str(e)}, ensure_ascii=False))
        if self.path.startswith("/session/"):
            mid = self.path[len("/session/"):].strip("/")
            if mid:
                try:
                    data = self.remote.get_session(mid)
                    return self._send(200, json.dumps(data, ensure_ascii=False))
                except Exception as e:
                    return self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False))
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
        ctype = self.headers.get("Content-Type", "")
        # ---- multipart file upload from the web UI ----
        if self.path == "/upload" and ctype.startswith("multipart/form-data"):
            return self._handle_upload(length, ctype)
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
        if self.path == "/meeting/new":
            title = (payload.get("title") or "").strip() or None
            try:
                mid = self.app.new_meeting(title)
                return self._send(200, json.dumps({"ok": True, "meeting_id": mid}, ensure_ascii=False))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False))
        if self.path.startswith("/session/") and self.path.endswith("/delete"):
            mid = self.path[len("/session/"):-len("/delete")].strip("/")
            try:
                self.remote.delete_session(mid)
                return self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False))
        if self.path.startswith("/session/") and self.path.endswith("/rename"):
            mid = self.path[len("/session/"):-len("/rename")].strip("/")
            title = (payload.get("title") or "").strip()
            if not title:
                return self._send(200, json.dumps({"error": "empty"}, ensure_ascii=False))
            try:
                self.remote.rename(mid, title)
                if mid == getattr(self.remote, "meeting_id", None):
                    with self.state.lock:
                        self.state.meeting_title = title
                return self._send(200, json.dumps({"ok": True, "title": title}, ensure_ascii=False))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False))
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

    def _handle_upload(self, length, ctype):
        """Receive a multipart file from the web UI, save to a temp file, and
        forward it to the remote server via the meeting's upload endpoint."""
        import tempfile
        import email
        try:
            body = self.rfile.read(length) if length else b""
            # Build a minimal MIME document so email.parser can split the parts.
            header = b"Content-Type: " + ctype.encode() + b"\r\n\r\n"
            msg = email.message_from_bytes(header + body)
            filename = None
            filedata = None
            for part in msg.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                disp = str(part.get("Content-Disposition", "") or "")
                if "filename=" in disp:
                    filename = part.get_filename() or "upload.bin"
                    filedata = part.get_payload(decode=True)
                    break
            if filedata is None:
                return self._send(200, json.dumps({"error": "no file in upload"}, ensure_ascii=False))
            if not self.remote.meeting_id:
                self.remote.start(title=None)
            suffix = os.path.splitext(filename)[1] or ""
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
                tf.write(filedata)
                tmp_path = tf.name
            # rename temp so remote gets the real filename
            real_path = os.path.join(os.path.dirname(tmp_path), os.path.basename(filename))
            try:
                os.replace(tmp_path, real_path)
            except Exception:
                real_path = tmp_path
            res = self.remote.upload(real_path)
            try:
                os.remove(real_path)
            except Exception:
                pass
            with self.state.lock:
                self.state.files.append({"name": os.path.basename(filename)})
            note = (res or {}).get("file", {}).get("note", "")
            return self._send(200, json.dumps({"ok": True, "name": os.path.basename(filename), "note": note}, ensure_ascii=False))
        except Exception as e:
            return self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False))



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

    def new_meeting(self, title=None):
        """Start a brand-new meeting session. Recording (start/stop) only feeds
        into the CURRENT meeting; a new session is created only here."""
        # stop any ongoing capture first so it doesn't bleed into the new meeting
        if self.state.recording and self.transcriber:
            self.transcriber.stop()
            with self.state.lock:
                self.state.recording = False
        self.remote.start(title=title)
        # reset local live view for the fresh meeting
        with self.state.lock:
            self.state.transcript = []
            self.state.files = []
            self.state.answers = []
            self.state.summary = ""
            self.state.meeting_title = title or ""
        try:
            self.title = "\U0001f9e0"
        except Exception:
            pass
        return self.remote.meeting_id

    # ---------- shared record/summary (menubar + web UI) ----------
    def start_recording(self):
        if self.state.recording:
            return
        # Only create a new session if there is no current meeting yet.
        # Otherwise, resume feeding into the current meeting (start/stop is just
        # pause/resume of capture within the same meeting).
        if not self.remote.meeting_id:
            self.remote.start(title=None)
        self.transcriber = Transcriber(self.cfg, on_segment=self._on_segment)
        self.transcriber.start()
        with self.state.lock:
            self.state.recording = True
        try:
            self.title = "\U0001f534"
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

"""Remote CortexI server client."""
import json
import requests


class RemoteClient:
    def __init__(self, base_url: str, token: str, origin_verify: str = ""):
        self.base = base_url.rstrip("/")
        self.token = token
        self.origin_verify = origin_verify
        self.meeting_id = None

    def _headers(self, extra=None):
        h = {"Authorization": f"Bearer {self.token}"}
        if self.origin_verify:
            h["X-Origin-Verify"] = self.origin_verify
        if extra:
            h.update(extra)
        return h

    def health(self):
        r = requests.get(f"{self.base}/health", headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.json()

    def list_sessions(self):
        """Return the server-side list of all sessions (for the sidebar)."""
        r = requests.get(f"{self.base}/sessions", headers=self._headers(), timeout=15)
        r.raise_for_status()
        return r.json().get("sessions", [])

    def get_session(self, mid):
        """Return full state (segments/files/analyses/summary) of one session."""
        r = requests.get(f"{self.base}/session/{mid}/state", headers=self._headers(), timeout=15)
        r.raise_for_status()
        return r.json()

    def delete_session(self, mid):
        """Delete a session (and its data) on the server."""
        r = requests.delete(f"{self.base}/session/{mid}", headers=self._headers(), timeout=15)
        r.raise_for_status()
        if self.meeting_id == mid:
            self.meeting_id = None
        return r.json()

    def start(self, title=None):
        r = requests.post(
            f"{self.base}/session/start",
            headers=self._headers({"Content-Type": "application/json"}),
            data=json.dumps({"title": title}),
            timeout=15,
        )
        r.raise_for_status()
        self.meeting_id = r.json()["meeting_id"]
        return self.meeting_id

    def feed(self, text):
        if not self.meeting_id:
            return None
        r = requests.post(
            f"{self.base}/session/{self.meeting_id}/feed",
            headers=self._headers({"Content-Type": "application/json"}),
            data=json.dumps({"text": text}),
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def upload(self, filepath):
        if not self.meeting_id:
            return None
        with open(filepath, "rb") as f:
            r = requests.post(
                f"{self.base}/session/{self.meeting_id}/upload",
                headers=self._headers(),
                files={"file": f},
                timeout=120,
            )
        r.raise_for_status()
        return r.json()

    def _poll_job(self, job_id, timeout=240, interval=2):
        import time as _t
        deadline = _t.time() + timeout
        while _t.time() < deadline:
            r = requests.get(f"{self.base}/job/{job_id}", headers=self._headers(), timeout=15)
            r.raise_for_status()
            j = r.json()
            if j.get("status") == "done":
                return j.get("result", "")
            if j.get("status") == "error":
                return f"[远程错误] {j.get('result','')}"
            _t.sleep(interval)
        return "[超时] 远程分析未在预期时间内返回"

    def ask(self, question):
        r = requests.post(
            f"{self.base}/session/{self.meeting_id}/ask",
            headers=self._headers({"Content-Type": "application/json"}),
            data=json.dumps({"question": question}),
            timeout=30,
        )
        r.raise_for_status()
        return self._poll_job(r.json()["job_id"])

    def summarize(self):
        r = requests.post(
            f"{self.base}/session/{self.meeting_id}/summarize",
            headers=self._headers(),
            timeout=30,
        )
        r.raise_for_status()
        return self._poll_job(r.json()["job_id"])

    def stream_url(self):
        # SSE endpoint takes token as query param (EventSource can't set headers)
        return f"{self.base}/session/{self.meeting_id}/stream?token={self.token}"

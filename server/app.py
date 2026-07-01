"""
CortexI — remote analysis server.

Runs on the CortexI server EC2 (private, behind CloudFront-locked ALB).
- Accumulates per-meeting context (transcript segments + uploaded files) server-side.
- Delegates analysis/summarization to Claude Code headless via Bedrock:
      env CLAUDE_CODE_USE_BEDROCK=1 AWS_REGION=us-east-2 claude -p "<prompt>"
- Auth: Bearer token (APP_TOKEN) + optional X-Origin-Verify shared secret (set by CloudFront).
- Binds 0.0.0.0:8000 behind an ALB that is locked to CloudFront.

Endpoints:
  GET  /health
  POST /session/start            -> {meeting_id}
  POST /session/{id}/feed        transcript segment  {text, ts}
  POST /session/{id}/upload      multipart file (image/doc)
  POST /session/{id}/ask         {question} -> {answer}   (live Q&A over context so far)
  POST /session/{id}/summarize   -> {summary}             (post-meeting)
  GET  /session/{id}/stream      SSE: pushes analysis events to the Mac UI
  GET  /session/{id}/state       -> full context snapshot
"""
import os
import re
import json
import time
import uuid
import asyncio
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

# ---------------- config ----------------
APP_TOKEN = os.environ.get("MC_APP_TOKEN", "")            # required bearer token
ORIGIN_VERIFY = os.environ.get("MC_ORIGIN_VERIFY", "")     # optional CloudFront shared secret
CLAUDE_BIN = os.environ.get("MC_CLAUDE_BIN", "/usr/bin/claude")
AWS_REGION = os.environ.get("MC_BEDROCK_REGION", "us-east-2")
DATA_DIR = Path(os.environ.get("MC_DATA_DIR", "/home/ec2-user/meeting-copilot-data"))
CLAUDE_TIMEOUT = int(os.environ.get("MC_CLAUDE_TIMEOUT", "180"))
MAX_CONTEXT_CHARS = int(os.environ.get("MC_MAX_CONTEXT_CHARS", "60000"))

DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="CortexI Server", version="0.1.0")

# ---------------- in-memory session registry + SSE buses ----------------
# meeting_id -> {"segments":[...], "files":[...], "analyses":[...], "created":ts, "title":str}
SESSIONS: dict = {}
# meeting_id -> list[asyncio.Queue]
SUBSCRIBERS: dict = {}
# job_id -> {"status":"pending|done|error","result":str,"kind":str,"mid":str}
JOBS: dict = {}


def _now() -> float:
    return time.time()


def _meeting_dir(mid: str) -> Path:
    d = DATA_DIR / mid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _require_auth(authorization: Optional[str], x_origin_verify: Optional[str]):
    if ORIGIN_VERIFY:
        if x_origin_verify != ORIGIN_VERIFY:
            raise HTTPException(status_code=403, detail="bad origin verify")
    if APP_TOKEN:
        expected = f"Bearer {APP_TOKEN}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="unauthorized")


def _persist(mid: str):
    s = SESSIONS.get(mid)
    if not s:
        return
    (_meeting_dir(mid) / "session.json").write_text(
        json.dumps(s, ensure_ascii=False, indent=2)
    )


async def _publish(mid: str, event: dict):
    for q in SUBSCRIBERS.get(mid, []):
        try:
            q.put_nowait(event)
        except Exception:
            pass


# ---------------- claude invocation ----------------
def _run_claude(prompt: str) -> str:
    """Call Claude Code headless via Bedrock. Blocking; run in threadpool."""
    env = dict(os.environ)
    env["CLAUDE_CODE_USE_BEDROCK"] = "1"
    env["AWS_REGION"] = AWS_REGION
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", prompt],
            env=env,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return "[error] claude timed out"
    out = (proc.stdout or "").strip()
    if not out and proc.stderr:
        return f"[error] {proc.stderr.strip()[:500]}"
    return out


def _build_context(mid: str, tail_chars: int = MAX_CONTEXT_CHARS) -> str:
    s = SESSIONS[mid]
    parts = []
    if s.get("title"):
        parts.append(f"# 会议: {s['title']}")
    if s.get("files"):
        parts.append("## 已上传文件/图片:")
        for f in s["files"]:
            note = f.get("note") or ""
            parts.append(f"- {f['name']} ({f.get('kind','file')}) {note}")
    parts.append("## 转写记录(时间序):")
    for seg in s.get("segments", []):
        parts.append(seg["text"])
    ctx = "\n".join(parts)
    if len(ctx) > tail_chars:
        ctx = ctx[-tail_chars:]
    return ctx


# ---------------- models ----------------
class StartReq(BaseModel):
    title: Optional[str] = None


class FeedReq(BaseModel):
    text: str
    ts: Optional[float] = None


class AskReq(BaseModel):
    question: str


# ---------------- endpoints ----------------
@app.get("/health")
async def health():
    return {"ok": True, "sessions": len(SESSIONS), "claude": CLAUDE_BIN}


@app.post("/session/start")
async def session_start(
    req: StartReq,
    authorization: Optional[str] = Header(None),
    x_origin_verify: Optional[str] = Header(None),
):
    _require_auth(authorization, x_origin_verify)
    mid = uuid.uuid4().hex[:12]
    SESSIONS[mid] = {
        "title": req.title or time.strftime("会议 %Y-%m-%d %H:%M"),
        "created": _now(),
        "segments": [],
        "files": [],
        "analyses": [],
    }
    _persist(mid)
    return {"meeting_id": mid, "title": SESSIONS[mid]["title"]}


@app.post("/session/{mid}/feed")
async def session_feed(
    mid: str,
    req: FeedReq,
    authorization: Optional[str] = Header(None),
    x_origin_verify: Optional[str] = Header(None),
):
    _require_auth(authorization, x_origin_verify)
    if mid not in SESSIONS:
        raise HTTPException(404, "no such meeting")
    seg = {"text": req.text.strip(), "ts": req.ts or _now()}
    if seg["text"]:
        SESSIONS[mid]["segments"].append(seg)
        _persist(mid)
        await _publish(mid, {"type": "transcript", "text": seg["text"], "ts": seg["ts"]})
    return {"ok": True, "segments": len(SESSIONS[mid]["segments"])}


@app.post("/session/{mid}/upload")
async def session_upload(
    mid: str,
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None),
    x_origin_verify: Optional[str] = Header(None),
):
    _require_auth(authorization, x_origin_verify)
    if mid not in SESSIONS:
        raise HTTPException(404, "no such meeting")
    raw = await file.read()
    dest = _meeting_dir(mid) / file.filename
    dest.write_bytes(raw)
    kind = "image" if (file.content_type or "").startswith("image/") else "file"
    note = ""
    # If it's an image, let claude describe it for context (path handed to claude).
    if kind == "image":
        desc = await asyncio.to_thread(
            _run_claude,
            f"请用两三句话描述这张会议中上传的图片内容(用于会议记录上下文): {dest}",
        )
        note = f"[图片摘要] {desc}"
    entry = {"name": file.filename, "kind": kind, "path": str(dest), "note": note}
    SESSIONS[mid]["files"].append(entry)
    _persist(mid)
    await _publish(mid, {"type": "file", "name": file.filename, "note": note})
    return {"ok": True, "file": entry}


@app.post("/session/{mid}/ask")
async def session_ask(
    mid: str,
    req: AskReq,
    authorization: Optional[str] = Header(None),
    x_origin_verify: Optional[str] = Header(None),
):
    _require_auth(authorization, x_origin_verify)
    if mid not in SESSIONS:
        raise HTTPException(404, "no such meeting")
    ctx = _build_context(mid)
    prompt = (
        "你是会议实时助手。以下是目前为止的会议上下文(转写+已上传文件摘要)。"
        "请基于上下文简洁回答用户的问题,不要编造上下文里没有的信息。\n\n"
        f"=== 会议上下文 ===\n{ctx}\n\n=== 用户问题 ===\n{req.question}"
    )
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "pending", "result": "", "kind": "ask", "mid": mid}

    async def _work():
        ans = await asyncio.to_thread(_run_claude, prompt)
        SESSIONS[mid]["analyses"].append({"q": req.question, "a": ans, "ts": _now()})
        _persist(mid)
        JOBS[job_id] = {"status": "done", "result": ans, "kind": "ask", "mid": mid}
        await _publish(mid, {"type": "answer", "question": req.question, "answer": ans})

    asyncio.create_task(_work())
    return {"job_id": job_id, "status": "pending"}


@app.post("/session/{mid}/summarize")
async def session_summarize(
    mid: str,
    authorization: Optional[str] = Header(None),
    x_origin_verify: Optional[str] = Header(None),
):
    _require_auth(authorization, x_origin_verify)
    if mid not in SESSIONS:
        raise HTTPException(404, "no such meeting")
    ctx = _build_context(mid)
    prompt = (
        "你是会议纪要助手。基于以下完整会议上下文,输出结构化会议总结,用中文,包含:\n"
        "1) 一句话概述  2) 关键讨论点(分条)  3) 决定事项  4) 待办/行动项(含负责人如果提到)  "
        "5) 风险/待确认问题。只用上下文里的信息,不编造。\n\n"
        f"=== 会议上下文 ===\n{ctx}"
    )
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "pending", "result": "", "kind": "summary", "mid": mid}

    async def _work():
        summary = await asyncio.to_thread(_run_claude, prompt)
        SESSIONS[mid]["summary"] = {"text": summary, "ts": _now()}
        (_meeting_dir(mid) / "summary.md").write_text(summary)
        _persist(mid)
        JOBS[job_id] = {"status": "done", "result": summary, "kind": "summary", "mid": mid}
        await _publish(mid, {"type": "summary", "summary": summary})

    asyncio.create_task(_work())
    return {"job_id": job_id, "status": "pending"}


@app.get("/session/{mid}/state")
async def session_state(
    mid: str,
    authorization: Optional[str] = Header(None),
    x_origin_verify: Optional[str] = Header(None),
):
    _require_auth(authorization, x_origin_verify)
    if mid not in SESSIONS:
        raise HTTPException(404, "no such meeting")
    return SESSIONS[mid]


@app.get("/job/{job_id}")
async def job_status(
    job_id: str,
    authorization: Optional[str] = Header(None),
    x_origin_verify: Optional[str] = Header(None),
):
    _require_auth(authorization, x_origin_verify)
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "no such job")
    return j


@app.get("/session/{mid}/stream")
async def session_stream(
    mid: str,
    request: Request,
    token: Optional[str] = None,
):
    # SSE: EventSource can't set headers, so accept token as query param here.
    if APP_TOKEN and token != APP_TOKEN:
        raise HTTPException(401, "unauthorized")
    if mid not in SESSIONS:
        raise HTTPException(404, "no such meeting")
    q: asyncio.Queue = asyncio.Queue()
    SUBSCRIBERS.setdefault(mid, []).append(q)

    async def gen():
        try:
            yield f"data: {json.dumps({'type':'hello','mid':mid})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            SUBSCRIBERS.get(mid, []).remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream")

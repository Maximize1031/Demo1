import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import sqlite3
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal

from . import workflow

ROOT = workflow.ROOT
load_dotenv(ROOT / ".env")


def create_app(data_dir=None, testing=False):
    production = os.getenv("APP_ENV", "local") == "production"
    password = os.getenv("DEMO_PASSWORD", "")
    key = os.getenv("OPENAI_API_KEY", "")
    model = os.getenv("OPENAI_MODEL", "gpt-6-astra")
    if production and len(password) < 16:
        raise RuntimeError("Set DEMO_PASSWORD to at least 16 characters before deploying.")
    data = Path(data_dir or os.getenv("DATA_DIR", str(ROOT / "data")))
    data.mkdir(parents=True, exist_ok=True)
    db_path = data / "intake.sqlite3"
    pool = ThreadPoolExecutor(max_workers=2)
    daily_limit = int(os.getenv("MAX_LIVE_CALLS_PER_DAY", "50"))

    def db():
        connection = sqlite3.connect(db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    with db() as c:
        c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, expires REAL NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL,
                kind TEXT NOT NULL, mode TEXT NOT NULL, created REAL NOT NULL, error TEXT);
            CREATE TABLE IF NOT EXISTS attempts (ip TEXT NOT NULL, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS jobs_session ON jobs(session_id);
        """)
        c.execute("UPDATE jobs SET status='failed', error='The server restarted. Please retry.' "
                  "WHERE status IN ('queued','running')")
        c.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        c.execute("DELETE FROM jobs WHERE session_id NOT IN (SELECT id FROM sessions)")

    @asynccontextmanager
    async def lifespan(app):
        yield
        pool.shutdown(wait=True)

    app = FastAPI(title="Maximize 1031 Intake", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def security(request, call_next):
        if request.method == "POST" and request.headers.get("x-intake-request") != "1":
            return Response("Invalid request", status_code=403)
        response = await call_next(request)
        response.headers.update({
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            "Cache-Control": "no-store",
        })
        if production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def session(request):
        token = request.cookies.get("intake_session", "")
        sid = hashlib.sha256(token.encode()).hexdigest()
        with db() as c:
            row = c.execute("SELECT * FROM sessions WHERE id=? AND expires>?", (sid, time.time())).fetchone()
        if not row:
            raise HTTPException(401, "Please sign in to the demo.")
        return sid, json.loads(row["state"])

    def empty_state():
        return {"current": None, "revisions": [], "messages": []}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static/index.html")

    @app.get("/api/config")
    def config():
        return {"password_required": bool(password), "live_available": bool(key),
                "synthetic": True}

    class Login(BaseModel):
        password: str = Field(default="", max_length=256)

    @app.post("/api/login")
    def login(payload: Login, request: Request, response: Response):
        now = time.time()
        # Use the direct peer, not a client-controlled forwarding header.
        ip = request.client.host if request.client else "unknown"
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM attempts WHERE created<?", (now - 900,))
            count = c.execute("SELECT count(*) FROM attempts WHERE ip=?", (ip,)).fetchone()[0]
            if count >= 30:
                raise HTTPException(429, "Too many sign-in attempts. Try again in 15 minutes.")
            c.execute("INSERT INTO attempts VALUES (?,?)", (ip, now))
        if password and not hmac.compare_digest(payload.password.encode(), password.encode()):
            raise HTTPException(401, "That password did not match.")
        token = secrets.token_urlsafe(32)
        sid = hashlib.sha256(token.encode()).hexdigest()
        with db() as c:
            c.execute("INSERT INTO sessions VALUES (?,?,?)", (sid, now + 86400, json.dumps(empty_state())))
        response.set_cookie("intake_session", token, max_age=86400, httponly=True,
                            secure=production, samesite="strict")
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        sid, _ = session(request)
        with db() as c:
            c.execute("DELETE FROM sessions WHERE id=?", (sid,))
        response.delete_cookie("intake_session")
        return {"ok": True}

    @app.get("/api/state")
    def state(request: Request):
        sid, current = session(request)
        with db() as c:
            job = c.execute("SELECT id,status,kind,error FROM jobs WHERE session_id=? "
                            "ORDER BY created DESC LIMIT 1", (sid,)).fetchone()
        phase = current["current"]["phase"] if current["current"] else "initial"
        return {**current, "job": dict(job) if job else None,
                "sources": workflow.sources(phase)}

    def execute(job_id, sid, kind, mode, phase, question, snapshot):
        with db() as c:
            c.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        try:
            if kind == "review":
                result = (workflow.live_review(key, model, phase, snapshot["current"])
                          if mode == "live" else workflow.rehearsal(phase))
                result["created_at"] = time.time()
                snapshot["current"] = result
                snapshot["revisions"].append(result)
                snapshot["messages"] = []
            else:
                answer = workflow.answer_question(key, model, snapshot["current"], snapshot["messages"], question)
                snapshot["messages"] += [{"role": "user", "content": question},
                                         {"role": "assistant", "content": answer}]
                snapshot["messages"] = snapshot["messages"][-24:]
            with db() as c:
                c.execute("UPDATE sessions SET state=? WHERE id=?", (json.dumps(snapshot), sid))
                c.execute("UPDATE jobs SET status='completed' WHERE id=?", (job_id,))
        except Exception as exc:
            message = str(exc) if isinstance(exc, workflow.WorkflowError) else "The run could not finish. Please retry."
            logging.getLogger(__name__).warning("Job failed (%s)", type(exc).__name__)
            with db() as c:
                c.execute("UPDATE jobs SET status='failed',error=? WHERE id=?", (message, job_id))

    def enqueue(sid, snapshot, kind, mode, phase, question=""):
        if mode == "live" and not key:
            raise HTTPException(503, "Live AI is not configured yet. Rehearsal mode is available.")
        job_id = secrets.token_hex(16)
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM jobs WHERE session_id=? AND status IN ('queued','running')", (sid,)).fetchone():
                raise HTTPException(409, "A run is already in progress.")
            if c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 4:
                raise HTTPException(429, "The demo is busy. Please try again shortly.")
            if mode == "live":
                used = c.execute("SELECT count(*) FROM jobs WHERE mode='live' AND created>?",
                                 (time.time() - 86400,)).fetchone()[0]
                if used >= daily_limit:
                    raise HTTPException(429, "The demo's daily AI request allowance has been reached.")
            if len(snapshot["revisions"]) >= 12 and kind == "review":
                raise HTTPException(429, "Reset this demo before starting more reviews.")
            c.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,NULL)",
                      (job_id, sid, "queued", kind, mode, time.time()))
        pool.submit(execute, job_id, sid, kind, mode, phase, question, snapshot)
        return {"job_id": job_id}

    class Review(BaseModel):
        phase: Literal["initial", "updated"]
        mode: Literal["rehearsal", "live"]

    @app.post("/api/review", status_code=202)
    def review(payload: Review, request: Request):
        sid, snapshot = session(request)
        if payload.phase == "updated":
            current = snapshot["current"]
            if not current or current["phase"] != "initial" or current["mode"] != payload.mode:
                raise HTTPException(409, "Run the initial review in this mode before adding the next batch.")
        elif snapshot["current"]:
            raise HTTPException(409, "Reset the demo before starting a new initial review.")
        return enqueue(sid, snapshot, "review", payload.mode, payload.phase)

    class Question(BaseModel):
        message: str = Field(min_length=1, max_length=2000)

    @app.post("/api/chat", status_code=202)
    def chat(payload: Question, request: Request):
        sid, snapshot = session(request)
        if not snapshot["current"]:
            raise HTTPException(409, "Run the intake review first.")
        if not payload.message.strip() or workflow.SENSITIVE.search(payload.message):
            raise HTTPException(400, "Enter a question without tax identifiers.")
        return enqueue(sid, snapshot, "chat", snapshot["current"]["mode"],
                       snapshot["current"]["phase"], payload.message.strip())

    @app.post("/api/reset")
    def reset(request: Request):
        sid, _ = session(request)
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM jobs WHERE session_id=? AND status IN ('queued','running')", (sid,)).fetchone():
                raise HTTPException(409, "Wait for the current run before resetting.")
            c.execute("UPDATE sessions SET state=? WHERE id=?", (json.dumps(empty_state()), sid))
        return {"ok": True}

    @app.get("/api/download")
    def download(request: Request):
        _, snapshot = session(request)
        if not snapshot["current"]:
            raise HTTPException(404, "No assessment to download yet.")
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as z:
            for index, revision in enumerate(snapshot["revisions"], 1):
                for key_name, filename in zip(workflow.FIELDS, ("assessment.md", "client-email.md", "escrow-email.md")):
                    z.writestr(f"revision-{index}-{revision['phase']}/{filename}", revision[key_name])
            z.writestr("README.txt", "Synthetic demonstration. Emails are drafts only. "
                       "Rehearsal results are saved examples, not new AI output.\n"
                       "Run mode: " + snapshot["current"]["mode"])
        return Response(data.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="rivera-intake.zip"'})

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app


app = create_app()

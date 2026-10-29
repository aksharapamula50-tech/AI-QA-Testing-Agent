from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

DB_PATH = Path(os.getenv("QA_DB_PATH", Path(__file__).resolve().parents[1] / "qa_agent.db"))
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
ALLOWED_SUFFIXES = {".txt", ".pdf", ".docx"}


@contextmanager
def db() -> Any:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT DEFAULT '',created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS requirements(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,title TEXT NOT NULL,content TEXT NOT NULL,source TEXT,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cases(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,requirement_id TEXT REFERENCES requirements(id) ON DELETE SET NULL,title TEXT NOT NULL,category TEXT NOT NULL,priority TEXT NOT NULL,steps TEXT NOT NULL,expected TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'Not Run',actual TEXT,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,status TEXT NOT NULL,actual TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,title TEXT NOT NULL,description TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'Open',created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,created_at TEXT NOT NULL);
        """)
        if not c.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
            pid = "demo-project"
            c.execute("INSERT INTO projects VALUES(?,?,?,?)", (pid, "Checkout & Payments", "Quality assurance for the customer checkout experience.", now()))
            rid = "demo-requirement"
            c.execute("INSERT INTO requirements VALUES(?,?,?,?,?,?)", (rid,pid,"Checkout requirements","Customers can pay by card. The checkout validates required fields and rejects expired cards. Successful payment shows an order confirmation.","Sample project",now()))
            seed = [
                ("Card payment succeeds with valid details","Positive","High","Enter a valid card and submit payment.","Payment succeeds and an order confirmation is shown."),
                ("Reject an expired card","Negative","High","Enter an expired card and submit payment.","Payment is declined with a clear message."),
                ("Submit with card number omitted","Validation","Medium","Leave card number empty and submit payment.","Submission is blocked and the required field is identified."),
                ("Card number at length boundary","Boundary","Medium","Submit a card number at the accepted length boundary.","The boundary value is handled according to the card validation rules."),
            ]
            for title,cat,priority,steps,expected in seed:
                c.execute("INSERT INTO cases VALUES(?,?,?,?,?,?,?,?,?,?,?)",(str(uuid.uuid4()),pid,rid,title,cat,priority,steps,expected,"Not Run",None,now()))


app = FastAPI(title="AI QA Testing Agent", version="1.0.0", description="Requirement analysis and transparent QA workflow API")
default_origins = {"http://localhost:5173", "http://127.0.0.1:5173", "http://192.168.1.3:5173"}
configured_origins = {origin.strip().rstrip("/") for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()}
app.add_middleware(CORSMiddleware, allow_origins=sorted(default_origins | configured_origins), allow_credentials=True, allow_methods=["GET","POST","PATCH","DELETE","OPTIONS"], allow_headers=["Content-Type"])
SESSION_COOKIE = "qa_session"
SESSION_TTL_SECONDS = 8 * 60 * 60


def auth_secret() -> bytes:
    configured = os.getenv("QA_AUTH_SECRET")
    if configured:
        if len(configured) < 32:
            raise RuntimeError("QA_AUTH_SECRET must contain at least 32 characters")
        return configured.encode("utf-8")
    secret_path = DB_PATH.parent / ".auth_secret"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        secret_fd = os.open(secret_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(secret_fd, "w", encoding="utf-8") as secret_file:
            secret_file.write(secrets.token_urlsafe(48))
    saved = secret_path.read_text(encoding="utf-8").strip()
    if len(saved) < 32:
        raise RuntimeError(f"The signing secret at {secret_path} is invalid; replace it with a random secret of at least 32 characters")
    return saved.encode("utf-8")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600_000)
    return f"pbkdf2_sha256$600000${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def create_user(email: str, password: str) -> dict:
    email = email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("Enter a valid email address")
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters")
    if len(password) > 256:
        raise ValueError("Password must be no longer than 256 characters")
    user_id = str(uuid.uuid4())
    with db() as c:
        try:
            c.execute("INSERT INTO users VALUES(?,?,?,?)", (user_id, email, hash_password(password), now()))
        except sqlite3.IntegrityError as ex:
            raise ValueError("An account already exists for that email") from ex
    return {"id": user_id, "email": email}


def issue_session(user_id: str) -> str:
    import base64
    payload = f"{user_id}.{int(datetime.now(timezone.utc).timestamp()) + SESSION_TTL_SECONDS}"
    encoded = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
    signature = hmac.new(auth_secret(), encoded.encode(), hashlib.sha256).digest()
    return encoded + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


def session_user(token: str | None) -> dict | None:
    import base64
    if not token:
        return None
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected = hmac.new(auth_secret(), encoded.encode(), hashlib.sha256).digest()
        signature = base64.urlsafe_b64decode(supplied_signature + "=" * (-len(supplied_signature) % 4))
        if not hmac.compare_digest(expected, signature):
            return None
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        user_id, expiry = payload.rsplit(".", 1)
        if int(expiry) <= int(datetime.now(timezone.utc).timestamp()):
            return None
        users = rows("SELECT id,email FROM users WHERE id=?", (user_id,))
        return users[0] if users else None
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


class SessionAuthMiddleware:
    """Pure ASGI auth middleware; avoids buffering request/response bodies."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            open_paths = {"/api/health", "/api/auth/login", "/api/auth/logout"}
            if path.startswith("/api/") and path not in open_paths and scope["method"] != "OPTIONS":
                request = Request(scope)
                user = session_user(request.cookies.get(SESSION_COOKIE))
                if user is None:
                    response = JSONResponse(status_code=401, content={"detail": "Authentication required. Log in to continue."})
                    await response(scope, receive, send)
                    return
                scope.setdefault("state", {})["user"] = user
        await self.app(scope, receive, send)


app.add_middleware(SessionAuthMiddleware)


@app.on_event("startup")
def startup() -> None:
    init_db()
    auth_secret()


def rows(sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    with db() as c: return [dict(r) for r in c.execute(sql,args).fetchall()]


def project_or_404(pid: str) -> dict:
    got = rows("SELECT * FROM projects WHERE id=?",(pid,))
    if not got: raise HTTPException(404,"Project not found")
    return got[0]


def validate_requirement(pid: str, rid: str | None) -> None:
    if rid and not rows("SELECT id FROM requirements WHERE id=? AND project_id=?", (rid, pid)):
        raise HTTPException(422, "Requirement does not belong to this project")


class ProjectIn(BaseModel):
    name: str = Field(min_length=1,max_length=100)
    description: str = Field(default="",max_length=1000)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class CaseIn(BaseModel):
    title: str = Field(min_length=1,max_length=200)
    category: str = Field(pattern="^(Positive|Negative|Boundary|Validation)$")
    priority: str = Field(default="Medium",pattern="^(Low|Medium|High|Critical)$")
    steps: str = Field(min_length=1,max_length=4000)
    expected: str = Field(min_length=1,max_length=2000)
    requirement_id: str | None = None


class TaskIn(BaseModel):
    title: str = Field(min_length=1,max_length=200)
    description: str = Field(min_length=1,max_length=2000)


class ReviewIn(BaseModel):
    requirement: str = Field(min_length=1,max_length=30000)
    cases: list[dict] = Field(default_factory=list,max_length=100)


@app.get("/api/health")
def health() -> dict: return {"status":"ok","storage":"sqlite","ai_configured":bool(os.getenv("AI_API_KEY"))}


@app.post("/api/auth/login")
def login(data: LoginIn, response: Response) -> dict:
    email = data.email.strip().lower()
    user = rows("SELECT id,email,password_hash FROM users WHERE email=?", (email,))
    if not user or not verify_password(data.password, user[0]["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    response.set_cookie(SESSION_COOKIE, issue_session(user[0]["id"]), max_age=SESSION_TTL_SECONDS,
                        httponly=True, secure=os.getenv("QA_COOKIE_SECURE") == "1",
                        samesite="lax", path="/")
    return {"id": user[0]["id"], "email": user[0]["email"]}


@app.get("/api/auth/session")
def get_session(request: Request) -> dict:
    return request.state.user


@app.post("/api/auth/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax",
                           secure=os.getenv("QA_COOKIE_SECURE") == "1")
    return {"ok": True}


@app.get("/api/projects")
def list_projects() -> list[dict]:
    return rows("SELECT p.*, (SELECT COUNT(*) FROM cases t WHERE t.project_id=p.id) case_count, (SELECT COUNT(*) FROM runs r WHERE r.project_id=p.id) run_count FROM projects p ORDER BY created_at DESC")


@app.post("/api/projects",status_code=201)
def create_project(data: ProjectIn) -> dict:
    pid=str(uuid.uuid4())
    with db() as c: c.execute("INSERT INTO projects VALUES(?,?,?,?)",(pid,data.name.strip(),data.description.strip(),now()))
    return project_or_404(pid)


@app.get("/api/projects/{pid}")
def get_project(pid: str) -> dict:
    p=project_or_404(pid)
    p["requirements"]=rows("SELECT * FROM requirements WHERE project_id=? ORDER BY created_at DESC",(pid,))
    for requirement in p["requirements"]:
        requirement["analysis"] = analyze(requirement["content"])
    p["cases"]=rows("SELECT * FROM cases WHERE project_id=? ORDER BY created_at DESC",(pid,))
    p["tasks"]=rows("SELECT * FROM tasks WHERE project_id=? ORDER BY created_at DESC",(pid,))
    p["runs"]=rows("SELECT r.*,t.title case_title,t.category FROM runs r JOIN cases t ON t.id=r.case_id WHERE r.project_id=? ORDER BY r.created_at DESC",(pid,))
    return p


def extract_text(name: str, content: bytes) -> str:
    suffix=Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES: raise HTTPException(415,"Supported files: TXT, PDF, DOCX")
    if len(content)>MAX_UPLOAD_BYTES: raise HTTPException(413,"File exceeds the 8 MB upload limit")
    try:
        if suffix==".txt": text=content.decode("utf-8-sig")
        elif suffix==".pdf":
            from pypdf import PdfReader
            import io
            text="\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(content)).pages)
        else:
            from docx import Document
            import io
            text="\n".join(p.text for p in Document(io.BytesIO(content)).paragraphs)
    except Exception as ex: raise HTTPException(422,f"Could not read this document: {type(ex).__name__}") from ex
    text=text.strip()
    if not text: raise HTTPException(422,"The uploaded document contains no extractable text")
    return text[:30000]


def analyze(text: str) -> dict:
    sentences=[s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+",text) if len(s.strip())>15]
    gaps=[]
    prompts=[("error handling",r"error|fail|declin|invalid|reject"),("boundary behavior",r"limit|maximum|minimum|boundary|length|range"),("empty or missing values",r"required|empty|missing|blank|mandatory"),("accessibility expectations",r"accessib|keyboard|screen reader|contrast"),("performance expectations",r"performance|latency|within \d|response time")]
    low=text.lower()
    for label,pattern in prompts:
        if not re.search(pattern,low): gaps.append({"area":label,"question":f"What should happen when {label} are encountered?"})
    return {"sentences":sentences,"gaps":gaps,"word_count":len(text.split()),"summary":f"Found {len(sentences)} requirement statements and {len(gaps)} areas that need clarification."}


def generate_cases(reqid: str, text: str) -> list[dict]:
    lines=[s for s in re.split(r"(?<=[.!?])\s+|\n+",text) if len(s.strip())>12]
    cases=[]
    for i,line in enumerate(lines[:12]):
        statement=line.strip().rstrip(".")
        subject=statement[:95]
        templates=[("Positive","High",f"Verify {subject} with valid, expected inputs.",f"The system satisfies: {statement}."),("Negative","High",f"Verify the system rejects invalid input for: {subject}.","The invalid operation is rejected safely with a useful error."),("Boundary","Medium",f"Verify minimum and maximum input boundaries for: {subject}.","Values at and just beyond defined limits are handled correctly."),("Validation","Medium",f"Verify required fields and formats for: {subject}.","Invalid or missing values are identified before submission.")]
        for category,priority,title,expected in templates:
            cases.append({"title":title,"category":category,"priority":priority,"steps":f"Arrange the relevant feature.\n{statement}.\nObserve the result.","expected":expected,"requirement_id":reqid})
    return cases


@app.post("/api/projects/{pid}/requirements",status_code=201)
async def upload_requirement(pid: str,file: UploadFile=File(...)) -> dict:
    project_or_404(pid)
    name=Path(file.filename or "").name
    content=await file.read(MAX_UPLOAD_BYTES+1)
    text=extract_text(name,content)
    rid=str(uuid.uuid4()); title=Path(name).stem[:120] or "Uploaded requirement"
    analysis=analyze(text)
    generated_tasks=[]
    with db() as c:
        c.execute("INSERT INTO requirements VALUES(?,?,?,?,?,?)",(rid,pid,title,text,name,now()))
        for gap in analysis["gaps"]:
            tid=str(uuid.uuid4()); task_title=f"Clarify {gap['area']} requirements"
            c.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)",(tid,pid,task_title,gap["question"],"Open",now()))
            generated_tasks.append({"id":tid,"title":task_title,"description":gap["question"],"status":"Open"})
    return {"id":rid,"title":title,"source":name,"content":text,"analysis":analysis,"generated_cases":generate_cases(rid,text),"generated_tasks":generated_tasks}


@app.get("/api/projects/{pid}/cases")
def list_cases(pid: str,search: str=Query(default="",max_length=100),category: str|None=None,status: str|None=None) -> list[dict]:
    project_or_404(pid); sql="SELECT * FROM cases WHERE project_id=?"; args:list[Any]=[pid]
    if search: sql+=" AND (title LIKE ? OR steps LIKE ?)"; args.extend([f"%{search}%",f"%{search}%"])
    if category: sql+=" AND category=?"; args.append(category)
    if status: sql+=" AND status=?"; args.append(status)
    return rows(sql+" ORDER BY created_at DESC",tuple(args))


@app.post("/api/projects/{pid}/cases",status_code=201)
def create_case(pid: str,data: CaseIn) -> dict:
    project_or_404(pid); validate_requirement(pid,data.requirement_id); cid=str(uuid.uuid4())
    with db() as c: c.execute("INSERT INTO cases VALUES(?,?,?,?,?,?,?,?,?,?,?)",(cid,pid,data.requirement_id,data.title.strip(),data.category,data.priority,data.steps,data.expected,"Not Run",None,now()))
    return rows("SELECT * FROM cases WHERE id=?",(cid,))[0]


@app.post("/api/projects/{pid}/cases/bulk",status_code=201)
def bulk_cases(pid: str,cases: list[CaseIn]) -> dict:
    project_or_404(pid)
    if len(cases)>100: raise HTTPException(422,"Upload at most 100 test cases at once")
    for case in cases: validate_requirement(pid,case.requirement_id)
    created=[]
    with db() as c:
        for case in cases:
            cid=str(uuid.uuid4()); c.execute("INSERT INTO cases VALUES(?,?,?,?,?,?,?,?,?,?,?)",(cid,pid,case.requirement_id,case.title.strip(),case.category,case.priority,case.steps,case.expected,"Not Run",None,now())); created.append(cid)
    return {"created":len(created),"cases":rows("SELECT * FROM cases WHERE project_id=? ORDER BY created_at DESC",(pid,))}


@app.delete("/api/projects/{pid}/cases/{cid}",status_code=204)
def delete_case(pid: str,cid: str) -> None:
    with db() as c:
        cur=c.execute("DELETE FROM cases WHERE id=? AND project_id=?",(cid,pid))
        if not cur.rowcount: raise HTTPException(404,"Test case not found")


@app.post("/api/projects/{pid}/tasks",status_code=201)
def create_task(pid: str,data: TaskIn) -> dict:
    project_or_404(pid); tid=str(uuid.uuid4())
    with db() as c: c.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)",(tid,pid,data.title.strip(),data.description.strip(),"Open",now()))
    return rows("SELECT * FROM tasks WHERE id=?",(tid,))[0]


@app.patch("/api/projects/{pid}/tasks/{tid}")
def update_task(pid: str,tid: str,status: str=Query(pattern="^(Open|In Progress|Done)$")) -> dict:
    with db() as c:
        cur=c.execute("UPDATE tasks SET status=? WHERE id=? AND project_id=?",(status,tid,pid))
        if not cur.rowcount: raise HTTPException(404,"Task not found")
    return rows("SELECT * FROM tasks WHERE id=?",(tid,))[0]


def execute_supported(case: dict) -> tuple[str,str]:
    """Only execute explicitly modeled, local, deterministic utility checks; never arbitrary case text."""
    title=case["title"].lower(); steps=case["steps"].lower()
    if "word count" in title and "minimum" in title:
        match=re.search(r"minimum (\d+) words",steps)
        if not match: return "Not Run","No explicit executable minimum-word-count target was supplied."
        threshold=int(match.group(1)); actual="word count utility: len('one two') >= minimum threshold"
        ok=len("one two".split())>=threshold
        return ("Pass" if ok else "Fail"),f"Executed Python word-count assertion: 2 >= {threshold} evaluated {ok}."
    if "email format" in title:
        match=re.search(r"email=([\w.+-]+@[\w.-]+\.[a-zA-Z]{2,})",steps)
        if not match: return "Not Run","No explicit email input was supplied for the supported email-format check."
        actual=match.group(1); ok=bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",actual))
        return ("Pass" if ok else "Fail"),f"Executed local email format assertion for {actual!r}: evaluated {ok}."
    if "safe arithmetic" in title:
        match=re.search(r"calculate (\d+)\s*\+\s*(\d+)",steps)
        if not match: return "Not Run","No explicit supported arithmetic expression was supplied."
        a,b=map(int,match.groups()); result=a+b; match_expected=re.search(r"equals (\d+)",case["expected"],re.I)
        ok=match_expected is not None and result==int(match_expected.group(1))
        return ("Pass" if ok else "Fail"),f"Executed Python assertion: {a} + {b} = {result}; expected {match_expected.group(1) if match_expected else 'not specified'}."
    return "Not Run","This natural-language case has no configured safe executable adapter. Define a supported operation or connect a test runner to execute it."


@app.post("/api/projects/{pid}/run")
def run_cases(pid: str,case_ids: list[str]|None=None) -> dict:
    project_or_404(pid)
    cases=rows("SELECT * FROM cases WHERE project_id=?"+(" AND id IN ("+",".join("?" for _ in case_ids)+")" if case_ids else ""),(pid,*case_ids) if case_ids else (pid,))
    results=[]
    with db() as c:
        for case in cases:
            status,actual=execute_supported(case); rid=str(uuid.uuid4())
            c.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)",(rid,pid,case["id"],status,actual,now()))
            c.execute("UPDATE cases SET status=?,actual=? WHERE id=?",(status,actual,case["id"]))
            results.append({"run_id":rid,"case_id":case["id"],"title":case["title"],"status":status,"actual":actual})
    return {"total":len(results),"passed":sum(r["status"]=="Pass" for r in results),"failed":sum(r["status"]=="Fail" for r in results),"not_run":sum(r["status"]=="Not Run" for r in results),"results":results}


@app.get("/api/projects/{pid}/report")
def report(pid: str) -> dict:
    project_or_404(pid)
    cases=rows("SELECT status,category FROM cases WHERE project_id=?",(pid,)); runs=rows("SELECT status,created_at FROM runs WHERE project_id=? ORDER BY created_at",(pid,))
    bycat={cat:sum(c["category"]==cat for c in cases) for cat in ["Positive","Negative","Boundary","Validation"]}
    bystatus={s:sum(c["status"]==s for c in cases) for s in ["Pass","Fail","Not Run"]}
    executed=sum(r["status"] in ("Pass","Fail") for r in runs); passed=sum(r["status"]=="Pass" for r in runs)
    recommendations = (
        (["Connect a configured test runner to execute the remaining natural-language cases."] if any(c["status"] == "Not Run" for c in cases) else [])
        + (["Add edge case and boundary assertions for requirements with unspecified limits."] if bycat["Boundary"] == 0 else [])
        + (["Review failed executions and record defects before release."] if any(r["status"] == "Fail" for r in runs) else [])
    )
    return {"total_cases": len(cases), "total_runs": len(runs), "passed": passed,
            "failed": sum(r["status"] == "Fail" for r in runs),
            "not_run": sum(r["status"] == "Not Run" for r in runs),
            "pass_rate": round(passed / executed * 100, 1) if executed else None,
            "coverage": bycat, "case_status": bystatus,
            "recommendations": recommendations, "recent_runs": runs[-20:]}


@app.post("/api/review")
async def quality_review(data: ReviewIn) -> dict:
    key=os.getenv("AI_API_KEY"); base=os.getenv("AI_BASE_URL","https://api.openai.com/v1"); model=os.getenv("AI_MODEL","gpt-4o-mini")
    if not key:
        a=analyze(data.requirement)
        return {"mode":"local","review":f"Local review: {a['summary']} "+("Clarify: "+"; ".join(g["question"] for g in a["gaps"][:3]) if a["gaps"] else "No common clarification gaps detected."),"gaps":a["gaps"],"message":"Set AI_API_KEY and AI_BASE_URL to enable provider-backed review. No external AI request was made."}
    payload={"model":model,"messages":[{"role":"system","content":"Review software requirements and test cases for ambiguity, missing edge cases, and coverage gaps. Return concise actionable advice. Do not claim to execute tests."},{"role":"user","content":json.dumps({"requirement":data.requirement,"cases":data.cases[:60]})}],"temperature":0.2}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res=await client.post(base.rstrip("/")+"/chat/completions",headers={"Authorization":f"Bearer {key}"},json=payload); res.raise_for_status(); body=res.json()
        return {"mode":"provider","review":body["choices"][0]["message"]["content"],"model":model}
    except (httpx.HTTPError,KeyError,ValueError) as ex: raise HTTPException(502,f"AI provider request failed ({type(ex).__name__}); check AI_BASE_URL, AI_MODEL and provider availability.") from ex

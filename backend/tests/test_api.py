import os
import tempfile
import unittest
from pathlib import Path

os.environ["QA_DB_PATH"] = str(Path(tempfile.gettempdir()) / f"ai_qa_testing_agent_test_{os.getpid()}.sqlite")
os.environ["QA_AUTH_SECRET"] = "unit-test-only-secret-never-use-in-production"

from fastapi import Response
from fastapi.responses import JSONResponse

from app.main import (
    CaseIn, LoginIn, ProjectIn, SessionAuthMiddleware, TaskIn, app, create_case,
    create_project, create_task, create_user, db, execute_supported, extract_text,
    health, init_db, issue_session, list_projects, login, logout, report, startup,
    run_cases, session_user, update_task, upload_requirement, verify_password,
)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        startup()
        cls.user = create_user("qa.test@example.test", "test-password-with-12-chars")
        cls.pid = list_projects()[0]["id"]

    @staticmethod
    def run_coroutine_without_io(coroutine):
        try:
            coroutine.send(None)
        except StopIteration as completed:
            return completed.value
        finally:
            if coroutine.cr_await is not None:
                coroutine.close()
        raise AssertionError("Unexpected asynchronous I/O in this local unit test")

    def test_auth_hash_login_cookie_session_logout_and_bad_password(self):
        with db() as conn:
            password_hash = conn.execute("SELECT password_hash FROM users WHERE id=?", (self.user["id"],)).fetchone()[0]
        self.assertNotEqual(password_hash, "test-password-with-12-chars")
        self.assertTrue(verify_password("test-password-with-12-chars", password_hash))
        self.assertFalse(verify_password("incorrect password", password_hash))
        response = Response()
        body = LoginIn(email=self.user["email"], password="test-password-with-12-chars")
        self.assertEqual(login(body, response)["email"], self.user["email"])
        self.assertIn("httponly", response.headers["set-cookie"].lower())
        self.assertIn("samesite=lax", response.headers["set-cookie"].lower())
        token = issue_session(self.user["id"])
        self.assertEqual(session_user(token)["id"], self.user["id"])
        self.assertIsNone(session_user(token + "tampered"))
        expired = Response()
        self.assertEqual(logout(expired), {"ok": True})
        self.assertIn("max-age=0", expired.headers["set-cookie"].lower())

    def test_project_routes_are_protected_by_session_middleware(self):
        def exercise(cookie):
            captured = []
            async def protected_app(scope, receive, send):
                await JSONResponse({"user": scope["state"]["user"]["email"]})(scope, receive, send)
            middleware = SessionAuthMiddleware(protected_app)
            scope = {"type":"http", "asgi":{"version":"3.0"}, "http_version":"1.1", "method":"GET", "scheme":"http", "path":"/api/projects", "raw_path":b"/api/projects", "query_string":b"", "headers":([(b"cookie", cookie.encode())] if cookie else []), "client":("local",1), "server":("local",80)}
            async def receive(): return {"type":"http.request", "body":b"", "more_body":False}
            async def send(message): captured.append(message)
            self.run_coroutine_without_io(middleware(scope, receive, send))
            return captured
        anonymous = exercise("")
        self.assertEqual(anonymous[0]["status"], 401)
        token = issue_session(self.user["id"])
        authenticated = exercise(f"qa_session={token}")
        self.assertEqual(authenticated[0]["status"], 200)
        self.assertIn(self.user["email"].encode(), authenticated[-1]["body"])

    def test_cors_allows_lan_and_localhost_frontends(self):
        cors = next(m for m in app.user_middleware if m.cls.__name__ == "CORSMiddleware")
        origins = cors.kwargs["allow_origins"]
        self.assertIn("http://192.168.1.3:5173", origins)
        self.assertIn("http://localhost:5173", origins)
        self.assertTrue(cors.kwargs["allow_credentials"])

    def test_project_case_execution_and_report(self):
        created_project = create_project(ProjectIn(name="Auth test project", description=""))
        self.assertEqual(created_project["name"], "Auth test project")
        case = create_case(created_project["id"], CaseIn(title="Safe arithmetic", category="Positive", steps="Calculate 2 + 2", expected="Result equals 4"))
        self.assertEqual(case["status"], "Not Run")
        result = run_cases(created_project["id"])
        self.assertEqual(result["passed"], 1)
        self.assertEqual(report(created_project["id"])["passed"], 1)
        status, _ = execute_supported({"title":"Safe arithmetic", "steps":"Calculate 2 + 2", "expected":"Result equals 5"})
        self.assertEqual(status, "Fail")

    def test_tasks_and_requirement_upload(self):
        task = create_task(self.pid, TaskIn(title="Review requirements", description="Check edge cases"))
        self.assertEqual(update_task(self.pid, task["id"], "Done")["status"], "Done")
        class MemoryUpload:
            filename = "spec.txt"
            async def read(self, limit): return b"The user can create a profile."
        upload = MemoryUpload()
        result = self.run_coroutine_without_io(upload_requirement(self.pid, upload))
        self.assertGreater(len(result["generated_cases"]), 0)
        self.assertGreater(len(result["generated_tasks"]), 0)

    def test_input_validation_and_local_review(self):
        with self.assertRaises(Exception): CaseIn(title="", category="made up", steps="", expected="")
        with self.assertRaises(Exception): extract_text("bad.exe", b"x")
        from app.main import ReviewIn, quality_review
        review = self.run_coroutine_without_io(quality_review(ReviewIn(requirement="Users can create a profile.", cases=[])))
        self.assertEqual(review["mode"], "local")
        self.assertTrue(review["gaps"])


if __name__ == "__main__": unittest.main()

import base64
import io
import json
import threading
import time
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from app import workflow
from app.main import create_app

HEADERS = {"X-Intake-Request": "1"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("DEMO_PASSWORD", "")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return create_app(tmp_path)


@pytest.fixture
def client(app):
    with TestClient(app, headers=HEADERS) as client:
        assert client.post("/api/login", json={}).status_code == 200
        yield client


def wait_for_job(client):
    for _ in range(100):
        state = client.get("/api/state").json()
        if state["job"]["status"] not in {"queued", "running"}:
            return state
        time.sleep(.01)
    raise AssertionError("Job did not finish")


def review(client, phase="initial", mode="rehearsal"):
    assert client.post("/api/review", json={"phase": phase, "mode": mode}).status_code == 202
    result = wait_for_job(client)
    assert result["job"]["status"] == "completed", result
    return result


def test_full_rehearsal_and_session_isolation(client, app):
    initial = review(client)
    assert initial["current"]["counts"] == {"found": 19, "external": 5, "internal": 1, "conflicts": 1}
    assert len(initial["sources"]) == 5
    assert all("06-" not in s["name"] for s in initial["sources"])
    updated = review(client, "updated")
    assert updated["current"]["counts"] == {"found": 22, "external": 1, "internal": 1, "conflicts": 0}
    assert updated["current"]["escrow_email"].startswith("No follow-up needed")
    assert len(updated["revisions"]) == 2
    with zipfile.ZipFile(io.BytesIO(client.get("/api/download").content)) as archive:
        assert "revision-1-initial/assessment.md" in archive.namelist()
        assert "revision-2-updated/assessment.md" in archive.namelist()
    other = TestClient(app, headers=HEADERS)
    other.post("/api/login", json={})
    assert other.get("/api/state").json()["current"] is None
    assert client.post("/api/reset", json={}).status_code == 200
    assert client.get("/api/state").json()["current"] is None
    assert client.get("/api/download").status_code == 404


def test_auth_csrf_and_invalid_transitions(app, client):
    anonymous = TestClient(app)
    assert anonymous.get("/api/state").status_code == 401
    assert anonymous.get("/api/download").status_code == 401
    assert anonymous.post("/api/login", json={}).status_code == 403
    assert client.post("/api/review", json={"phase":"updated","mode":"rehearsal"}).status_code == 409
    assert client.post("/api/chat", json={"message":"hello"}).status_code == 409
    assert client.post("/api/review", json={"phase":"initial","mode":"live"}).status_code == 503
    assert client.post("/api/review", json={"phase":"../../etc","mode":"rehearsal"}).status_code == 422
    assert client.get("/skill/SKILL.md").status_code == 404
    assert client.get("/.env").status_code == 404
    review(client)
    assert client.post("/api/review", json={"phase":"initial","mode":"rehearsal"}).status_code == 409
    assert client.post("/api/chat", json={"message":"My ID is 123-45-6789"}).status_code == 400
    assert client.post("/api/logout", json={}).status_code == 200
    assert client.get("/api/state").status_code == 401


def test_password_and_production_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV","production")
    monkeypatch.setenv("DEMO_PASSWORD","short")
    with pytest.raises(RuntimeError):
        create_app(tmp_path)
    monkeypatch.setenv("DEMO_PASSWORD","a-long-demo-password")
    with TestClient(create_app(tmp_path), base_url="https://demo.example", headers=HEADERS) as client:
        assert client.post("/api/login",json={"password":"wrong"}).status_code == 401
        result=client.post("/api/login",json={"password":"a-long-demo-password"})
        assert result.status_code == 200
        assert "Secure" in result.headers["set-cookie"]
        assert "HttpOnly" in result.headers["set-cookie"]
        assert client.get("/api/state").status_code == 200


def test_no_future_evidence_or_rehearsals_in_skill_bundle():
    for phase, count in [("initial",5),("updated",7)]:
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(workflow.bundle(phase)))) as archive:
            names=archive.namelist()
            assert sum("/records/" in n for n in names) == count
            assert not any("rehearsal" in n or ".env" in n for n in names)
            if phase=="initial":
                assert not any("06-escrow" in n or "07-operating" in n for n in names)


@pytest.mark.parametrize("mutation",["missing_row","future_source","tax_id","wrong_case","invented_email"])
def test_invalid_ai_results_rejected(mutation):
    result=workflow.rehearsal("initial")
    if mutation=="missing_row":
        result["assessment"]="\n".join(l for l in result["assessment"].splitlines() if not l.startswith("| entity |"))
    elif mutation=="future_source":
        result["assessment"]=result["assessment"].replace("01-client-brief.md","06-escrow-confirmation.md")
    elif mutation=="tax_id":
        result["client_email"]+="\n123-45-6789"
    elif mutation=="wrong_case":
        result["assessment"]=result["assessment"].replace("DEMO-RIVERA-001","OTHER-CASE")
    else:
        result["client_email"]="To: invented@example.org\nSubject: Hello\n"
    with pytest.raises(workflow.WorkflowError):
        workflow.validate_result(result,"initial","live")


def test_active_job_prevents_reset_and_duplicate(client, monkeypatch):
    started=threading.Event(); release=threading.Event()
    original=workflow.rehearsal
    def slow(phase):
        started.set();release.wait(5);return original(phase)
    monkeypatch.setattr(workflow,"rehearsal",slow)
    client.post("/api/review",json={"phase":"initial","mode":"rehearsal"})
    assert started.wait(2)
    try:
        assert client.post("/api/reset",json={}).status_code == 409
        assert client.post("/api/review",json={"phase":"initial","mode":"rehearsal"}).status_code == 409
    finally:
        release.set()
    assert wait_for_job(client)["job"]["status"]=="completed"


def test_failure_keeps_prior_revision(client, monkeypatch):
    review(client)
    def fail(phase):
        raise workflow.WorkflowError("Example failure")
    monkeypatch.setattr(workflow,"rehearsal",fail)
    client.post("/api/review",json={"phase":"updated","mode":"rehearsal"})
    result=wait_for_job(client)
    assert result["job"]["status"]=="failed"
    assert result["current"]["phase"]=="initial"
    assert len(result["revisions"])==1


def test_live_api_contract_and_cleanup(monkeypatch):
    result=workflow.rehearsal("initial")
    calls=[]
    def handler(request):
        calls.append(request)
        if request.method=="DELETE":
            return httpx.Response(200,json={"deleted":True})
        payload=json.loads(request.content)
        if request.url.path.endswith("/containers"):
            assert payload["skills"][0]["type"]=="inline"
            return httpx.Response(200,json={"id":"cntr_test"})
        assert payload["store"] is False
        assert payload["tools"][0]["environment"]["container_id"]=="cntr_test"
        assert payload["text"]["format"]["strict"] is True
        return httpx.Response(200,json={"id":"resp_test","status":"completed","output":[
            {"type":"shell_call"},{"type":"message","content":[{"type":"output_text",
            "text":json.dumps({k:result[k] for k in workflow.FIELDS})}]}]})
    real=httpx.Client
    monkeypatch.setattr(workflow.httpx,"Client",lambda **kwargs:real(transport=httpx.MockTransport(handler),**kwargs))
    actual=workflow.live_review("test-key","test-model","initial",None)
    assert actual["mode"]=="live"
    assert actual["execution"]["shell_calls"]==1
    assert calls[-1].method=="DELETE"


def test_api_failure_cleanup_and_no_secret_in_error(monkeypatch):
    calls=[]
    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/containers"):
            return httpx.Response(200,json={"id":"cntr_test"})
        if request.method=="DELETE":
            return httpx.Response(200,json={})
        return httpx.Response(401,json={"error":{"message":"secret-provider-debug"}})
    real=httpx.Client
    monkeypatch.setattr(workflow.httpx,"Client",lambda **kwargs:real(transport=httpx.MockTransport(handler),**kwargs))
    with pytest.raises(workflow.WorkflowError,match="access was declined") as exc:
        workflow.live_review("test-key","test-model","initial",None)
    assert "secret-provider-debug" not in str(exc.value)
    assert calls[-1].method=="DELETE"


def test_live_limit(tmp_path,monkeypatch):
    monkeypatch.setenv("APP_ENV","local");monkeypatch.setenv("DEMO_PASSWORD","")
    monkeypatch.setenv("OPENAI_API_KEY","test-key");monkeypatch.setenv("MAX_LIVE_CALLS_PER_DAY","0")
    with TestClient(create_app(tmp_path),headers=HEADERS) as client:
        client.post("/api/login",json={})
        assert client.post("/api/review",json={"phase":"initial","mode":"live"}).status_code==429

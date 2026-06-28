"""
End-to-end local integration test.

Requires backend/.env with MONGODB_URI and GEMINI_API_KEY.

Run from repo root:
  .venv/bin/python -m backend.tests.test_e2e_local
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

API_KEY = "test_key_abc123"
BASE_URL = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8765")
USE_LIVE_SERVER = os.environ.get("E2E_LIVE_SERVER", "1") == "1"
SUPER_ADMIN_EMAIL = os.environ.get("SUPER_ADMIN_EMAIL", "admin@alpha.dev")
SUPER_ADMIN_PASSWORD = os.environ.get("SUPER_ADMIN_PASSWORD", "Admin123!change")


def _headers(api_key: str = API_KEY):
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _jwt_headers(token: str):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def _http(method: str, path: str, **kwargs):
    import httpx

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60.0) as client:
        return await client.request(method, path, headers=_headers(), **kwargs)


async def test_auth_flow():
    import httpx

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        # Legacy API key login (machine / voice)
        bad = await client.post("/api/auth/login", json={"api_key": "bad_key"})
        assert bad.status_code == 401, bad.text

        ok = await client.post("/api/auth/login", json={"api_key": API_KEY})
        assert ok.status_code == 200, ok.text
        data = ok.json()
        assert data["tenant_id"] == "alpha_default"
        assert data["org_name"]

        # /me requires JWT — API key bearer should fail
        me_bad = await client.get("/api/auth/me", headers=_headers())
        assert me_bad.status_code == 401, me_bad.text

        # Super admin email/password login
        admin = await client.post(
            "/api/auth/login",
            json={"email": SUPER_ADMIN_EMAIL, "password": SUPER_ADMIN_PASSWORD},
        )
        assert admin.status_code == 200, admin.text
        admin_data = admin.json()
        assert admin_data["user"]["role"] == "super_admin"
        admin_token = admin_data["access_token"]

        me_admin = await client.get("/api/auth/me", headers=_jwt_headers(admin_token))
        assert me_admin.status_code == 200
        assert me_admin.json()["role"] == "super_admin"

        stats = await client.get("/api/superadmin/stats", headers=_jwt_headers(admin_token))
        assert stats.status_code == 200, stats.text
        assert "tenant_count" in stats.json()

        tenants = await client.get("/api/superadmin/tenants", headers=_jwt_headers(admin_token))
        assert tenants.status_code == 200
        assert isinstance(tenants.json().get("tenants"), list)

        users = await client.get("/api/superadmin/users", headers=_jwt_headers(admin_token))
        assert users.status_code == 200
        assert isinstance(users.json().get("users"), list)

        # Client registration
        suffix = int(time.time())
        reg = await client.post(
            "/api/auth/register",
            json={
                "org_name": f"E2E Org {suffix}",
                "name": "E2E User",
                "email": f"e2e_{suffix}@example.com",
                "password": "E2EPass123!",
            },
        )
        assert reg.status_code == 200, reg.text
        reg_data = reg.json()
        assert reg_data.get("api_key")
        assert reg_data["user"]["tenant_id"]
        tenant_token = reg_data["access_token"]
        new_api_key = reg_data["api_key"]

        me_tenant = await client.get("/api/auth/me", headers=_jwt_headers(tenant_token))
        assert me_tenant.status_code == 200
        assert me_tenant.json()["tenant_id"] == reg_data["user"]["tenant_id"]

        # JWT works on tenant-scoped routes
        leads = await client.get("/api/leads", headers=_jwt_headers(tenant_token))
        assert leads.status_code == 200, leads.text

        # Regenerate API key
        regen = await client.post(
            "/api/auth/regenerate-api-key",
            headers=_jwt_headers(tenant_token),
        )
        assert regen.status_code == 200, regen.text
        assert regen.json().get("api_key")
        assert regen.json()["api_key"] != new_api_key

        # Super admin cannot use tenant dashboard routes
        tenant_route = await client.get("/api/leads", headers=_jwt_headers(admin_token))
        assert tenant_route.status_code == 403, tenant_route.text

    print("✓ auth: API key, JWT login, register, /me, superadmin, regenerate")


async def test_tenant_scoped_leads():
    thread_id = f"e2e_thread_{int(time.time())}"
    r = await _http("POST", f"/api/leads/{thread_id}", json={"company": "E2E Corp", "status": "New"})
    assert r.status_code == 200, r.text

    r2 = await _http("GET", f"/api/leads/{thread_id}")
    assert r2.status_code == 200
    assert r2.json()["company"] == "E2E Corp"

    r3 = await _http("GET", "/api/leads")
    assert r3.status_code == 200
    assert any(l.get("thread_id") == thread_id for l in r3.json())
    print("✓ tenant-scoped leads CRUD")


async def test_admin_integrations():
    r = await _http("GET", "/api/admin/integration-schemas")
    assert r.status_code == 200, r.text
    cats = {c["id"] for c in r.json()["categories"]}
    assert "inventory" in cats and "crm" in cats

    r2 = await _http("GET", "/api/admin/tenant")
    assert r2.status_code == 200
    assert r2.json()["tenant_id"] == "alpha_default"
    print("✓ admin integration schemas + tenant view")


async def test_conversations_and_typed():
    thread_id = f"e2e_conv_{int(time.time())}"
    r = await _http(
        "POST",
        f"/api/conversations/{thread_id}/typed",
        json={"message": "john@example.com typed during call"},
    )
    assert r.status_code == 200, r.text

    r2 = await _http("GET", f"/api/conversations/{thread_id}")
    assert r2.status_code == 200
    msgs = r2.json().get("messages") or []
    assert any("john@example.com" in m.get("content", "") for m in msgs)
    print("✓ typed conversation append")


async def test_websocket_agent_stream():
    import websockets

    thread_id = f"e2e_ws_{int(time.time())}"
    ws_url = BASE_URL.replace("http://", "ws://").replace("https://", "wss://")
    uri = f"{ws_url}/ws/chat/{thread_id}?api_key={API_KEY}"

    tokens = []
    got_idle = False

    async with websockets.connect(uri, open_timeout=15) as ws:
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                msg = json.loads(raw)
                if msg.get("type") == "history":
                    continue
                break
        except asyncio.TimeoutError:
            pass

        await ws.send(json.dumps({"message": "What packages do you sell? Keep it brief."}))

        deadline = time.time() + 90
        while time.time() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.time())
            msg = json.loads(raw)
            t = msg.get("type")
            if t == "response":
                tokens.append(msg.get("token", ""))
            elif t == "stream_start":
                pass
            elif t == "error":
                raise AssertionError(f"WebSocket agent error: {msg.get('message')}")
            elif t == "status" and msg.get("status") == "Idle":
                got_idle = True
                break

    full = "".join(tokens)
    assert got_idle, "never reached Idle status"
    assert len(full) > 10, f"expected streamed response, got: {full!r}"
    print(f"✓ websocket agent stream (api_key) ({len(full)} chars): {full[:120]}...")


async def test_websocket_jwt_auth():
    import httpx
    import websockets

    suffix = int(time.time())
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        reg = await client.post(
            "/api/auth/register",
            json={
                "org_name": f"WS Org {suffix}",
                "name": "WS User",
                "email": f"ws_{suffix}@example.com",
                "password": "WSPass123!",
            },
        )
        assert reg.status_code == 200, reg.text
        token = reg.json()["access_token"]

    thread_id = f"e2e_ws_jwt_{suffix}"
    ws_url = BASE_URL.replace("http://", "ws://").replace("https://", "wss://")
    uri = f"{ws_url}/ws/chat/{thread_id}?token={token}"

    async with websockets.connect(uri, open_timeout=15) as ws:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            msg = json.loads(raw)
            assert msg.get("type") != "unauthorized", msg
        except asyncio.TimeoutError:
            pass

    print("✓ websocket JWT auth connects")


async def test_voice_public_key():
    r = await _http("GET", "/api/voice/public-key")
    assert r.status_code == 200
    assert r.json().get("tenant_id") == "alpha_default"
    print("✓ voice public-key returns tenant_id")


async def run_all():
    await test_auth_flow()
    await test_tenant_scoped_leads()
    await test_admin_integrations()
    await test_conversations_and_typed()
    await test_voice_public_key()
    await test_websocket_jwt_auth()
    await test_websocket_agent_stream()
    print("\n✅ All end-to-end local tests passed.")


def _start_server():
    import subprocess
    import httpx

    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    venv_python = os.path.join(repo_root, ".venv", "bin", "python")
    python = venv_python if os.path.isfile(venv_python) else sys.executable
    env = {**os.environ, "PYTHONPATH": repo_root}

    proc = subprocess.Popen(
        [python, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "8765"],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    for _ in range(45):
        time.sleep(1)
        try:
            r = httpx.post(f"{BASE_URL}/api/auth/login", json={"api_key": API_KEY}, timeout=3.0)
            if r.status_code == 200:
                return proc
        except Exception:
            continue

    proc.terminate()
    out = proc.stdout.read().decode() if proc.stdout else ""
    raise RuntimeError(f"Server failed to start within 45s:\n{out[-3000:]}")


if __name__ == "__main__":
    proc = None
    try:
        if USE_LIVE_SERVER:
            print("Starting local server on :8765...")
            proc = _start_server()
            print("Server ready.")
        asyncio.run(run_all())
    finally:
        if proc:
            proc.terminate()
            proc.wait(timeout=5)

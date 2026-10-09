"""验证浏览器接口、并发任务、取消、导出和本地访问保护。"""

import asyncio
import json

import httpx
import pytest

from model_connect.app import create_app
from model_connect.jobs import JobManager
from model_connect.schemas import Connection, ModelInfo, ProbeRequest


async def wait_job(client, job_id):
    for _ in range(100):
        job = (await client.get(f"/api/jobs/{job_id}")).json()
        if job["status"] != "running":
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("检测任务未完成")


async def test_list_probe_both_and_exports():
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(
                200, json={"data": [{"id": "demo"}, {"id": "text-embedding-3-small"}]}
            )
        if request.url.path.endswith("/responses"):
            return httpx.Response(404, json={"error": {"message": "Responses not implemented"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi secret-key"}}]})

    app = create_app(httpx.MockTransport(handler))
    config = {
        "provider": "openai_compatible",
        "base_url": "http://provider.test/v1",
        "api_key": "secret-key",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        listing = await client.post("/api/models", json={"connection": config})
        assert listing.status_code == 200
        assert len(calls) == 1
        response = await client.post(
            "/api/jobs",
            json={
                "connection": config,
                "models": ["demo", "text-embedding-3-small"],
                "protocol": "both",
            },
        )
        assert response.status_code == 201
        job_id = response.json()["id"]
        job = await wait_job(client, job_id)
        assert job["counts"] == {"success": 1, "failed": 1, "skipped": 2}
        assert job["completed"] == 4
        assert len(calls) == 3
        exported = await client.get(f"/api/jobs/{job_id}/export")
        assert "secret-key" not in exported.text
        assert "[已隐藏]" in exported.text
        csv = await client.get(f"/api/jobs/{job_id}/export?format=csv")
        assert csv.text.startswith("\ufeff模型,协议")
        assert "secret-key" not in csv.text
        assert (await client.get(f"/api/jobs/{job_id}/export?format=html")).status_code == 400
        assert (await client.get("/api/jobs")).json()["jobs"][0]["id"] == job_id
        await app.state.jobs.shutdown()


async def test_cancel_limits_inflight_calls():
    started = asyncio.Event()
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 2:
            started.set()
        await asyncio.sleep(30)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    app = create_app(httpx.MockTransport(handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/api/jobs",
            json={
                "connection": {"base_url": "http://provider.test/v1"},
                "models": [f"demo-{i}" for i in range(10)],
                "concurrency": 2,
            },
        )
        await asyncio.wait_for(started.wait(), 2)
        job = (await client.post(f"/api/jobs/{response.json()['id']}/cancel")).json()
    assert calls == 2
    assert job["status"] == "cancelled"
    assert job["counts"] == {"cancelled": 10}
    assert job["completed"] == 10


async def test_metadata_skip_force_and_immediate_cancel():
    calls = []
    transport = httpx.MockTransport(
        lambda request: (
            calls.append(request)
            or httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "hi"}]}}]})
        )
    )
    manager = JobManager(transport)
    request = ProbeRequest(
        connection=Connection(provider="google"),
        models=["unknown"],
        model_details=[ModelInfo(id="unknown", supported=False)],
    )
    job = manager.create(request)
    task = job.task
    await task
    assert job.results[0].status == "skipped"
    assert not calls
    request.force = True
    job = manager.create(request)
    await job.task
    assert job.results[0].status == "success"
    job = manager.create(request)
    await manager.cancel(job)
    assert job.status == "cancelled"
    assert job.results[0].status == "cancelled"


async def test_invalid_input_does_not_echo_key_and_origins_blocked():
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        result = await client.post(
            "/api/models", json={"connection": {"base_url": "invalid", "api_key": "secret-key"}}
        )
        assert result.status_code == 422
        assert "secret-key" not in result.text
        assert (
            await client.post("/api/models", json={}, headers={"Origin": "https://external.test"})
        ).status_code == 403
        assert (await client.get("/api/health", headers={"Host": "evil.test"})).status_code == 403
        assert (
            await client.get("/api/health", headers={"Origin": "http://localhost"})
        ).status_code == 200
        page = await client.get("/")
        assert "模型列出来" in page.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]


async def test_filter_csv_formula_and_secret_error():
    app = create_app(
        httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": {"message": "bad secret-key"}})
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        filtered = await client.post(
            "/api/filter",
            json={
                "models": [{"id": "a_flash_b"}, {"id": "other"}],
                "filter": {"mode": "glob", "pattern": "**_flash_**"},
            },
        )
        assert [item["id"] for item in filtered.json()["models"]] == ["a_flash_b"]
        response = await client.post(
            "/api/jobs",
            json={
                "connection": {"base_url": "http://provider.test", "api_key": "secret-key"},
                "models": ["=FORMULA"],
            },
        )
        job_id = response.json()["id"]
        await wait_job(client, job_id)
        csv = await client.get(f"/api/jobs/{job_id}/export?format=csv")
        assert "'=FORMULA" in csv.text
        assert "secret-key" not in csv.text


async def test_job_limit_and_bounded_history():
    manager = JobManager(
        httpx.MockTransport(
            lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})
        )
    )
    request = ProbeRequest(connection=Connection(base_url="http://provider.test"), models=["demo"])
    for _ in range(25):
        job = manager.create(request)
        await job.task
    assert len(manager.jobs) == 20
    for _ in range(3):
        manager.create(request)
    with pytest.raises(ValueError, match="3 个任务"):
        manager.create(request)
    await manager.shutdown()
    assert all(job.status != "running" for job in manager.jobs.values())


async def test_gateway_paths_and_header_override():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer alternate-key"
        assert request.url.path == "/gateway/custom/list"
        return httpx.Response(200, json={"data": [{"id": "claude-proxy"}]})

    app = create_app(httpx.MockTransport(handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/api/models",
            json={
                "connection": {
                    "base_url": "https://provider.test/gateway",
                    "list_path": "/custom/list",
                    "headers": {"Authorization": "Bearer alternate-key"},
                }
            },
        )
        assert response.status_code == 200
        assert json.loads(response.content)["models"][0]["id"] == "claude-proxy"

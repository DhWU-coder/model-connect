"""提供本地中文界面、检测接口和报告下载。"""

import asyncio
import csv
import io
import ipaddress
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from model_connect import __version__
from model_connect.jobs import JobManager
from model_connect.providers import Adapter, ProviderError
from model_connect.schemas import PROVIDERS, FilterRequest, ListRequest, ProbeRequest

STATIC_DIR = Path(__file__).parent / "static"


def csv_cell(value: object) -> str:
    """避免模型名称或上游回复被电子表格当作公式执行。"""
    text = str(value) if value is not None else ""
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def create_app(
    transport: httpx.AsyncBaseTransport | None = None, host: str = "127.0.0.1"
) -> FastAPI:
    manager = JobManager(transport)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await manager.shutdown()

    app = FastAPI(
        title="Model Connect",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.jobs = manager

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        hostname = request.url.hostname or ""
        allowed = hostname in {"localhost", "127.0.0.1", "::1", host}
        if host in {"0.0.0.0", "::"}:
            try:
                ipaddress.ip_address(hostname)
                allowed = True
            except ValueError:
                pass
        if not allowed:
            return JSONResponse({"detail": "访问域名不在本地服务允许范围内"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and (origin == "null" or urlsplit(origin).netloc != request.url.netloc):
            return JSONResponse({"detail": "不允许其他网页跨站访问本地接口"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "不允许跨站访问本地接口"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # FastAPI 默认验证错误会包含原始输入，密钥字段必须从响应中去除。
        return JSONResponse(
            {
                "detail": [
                    {"loc": list(error["loc"]), "msg": error["msg"]} for error in exc.errors()
                ]
            },
            status_code=422,
        )

    @app.exception_handler(ProviderError)
    async def provider_error(request: Request, exc: ProviderError):
        return JSONResponse(
            {"detail": str(exc), "error_code": exc.code, "http_status": exc.http_status},
            status_code=502,
        )

    @app.get("/api/health")
    async def health():
        return {
            "service": "model-connect",
            "version": __version__,
            "pid": os.getpid(),
            "instance": os.environ.get("MODEL_CONNECT_INSTANCE", ""),
        }

    @app.get("/api/providers")
    async def providers():
        return PROVIDERS

    @app.post("/api/models")
    async def list_models(request: ListRequest):
        try:
            async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
                async with asyncio.timeout(request.timeout):
                    models = await Adapter(request.connection, client).list_models(request.timeout)
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise ProviderError("获取模型列表超时，请检查地址或增加超时设置", "timeout") from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                f"获取模型列表时网络连接失败：{type(exc).__name__}", "network_error"
            ) from exc
        return {
            "models": [model.model_dump() for model in models],
            "total": len(models),
            "base_url": request.connection.base_url,
        }

    @app.post("/api/filter")
    async def filter_models(request: FilterRequest):
        return {
            "models": [
                model.model_dump() for model in request.models if request.filter.matches(model.id)
            ]
        }

    @app.post("/api/jobs", status_code=201)
    async def create_job(request: ProbeRequest):
        try:
            return manager.create(request).snapshot()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    def get_job(job_id: str):
        if job_id not in manager.jobs:
            raise HTTPException(404, "任务不存在或已过期，服务重启后检测历史会清空")
        return manager.jobs[job_id]

    @app.get("/api/jobs")
    async def list_jobs():
        return {
            "jobs": [
                {key: value for key, value in job.snapshot().items() if key != "results"}
                for job in reversed(list(manager.jobs.values()))
            ]
        }

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str):
        return get_job(job_id).snapshot()

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        job = get_job(job_id)
        await manager.cancel(job)
        return job.snapshot()

    @app.get("/api/jobs/{job_id}/export")
    async def export_job(job_id: str, format: str = "json"):
        snapshot = get_job(job_id).snapshot()
        filename = f"model-connect-{job_id[:8]}.{format}"
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        if format == "json":
            return Response(
                json.dumps(snapshot, ensure_ascii=False, indent=2),
                media_type="application/json",
                headers=headers,
            )
        if format != "csv":
            raise HTTPException(400, "仅支持 json 和 csv 导出格式")
        output = io.StringIO()
        writer = csv.writer(output)
        columns = [
            "model",
            "protocol",
            "status",
            "latency_ms",
            "http_status",
            "text",
            "error",
            "error_code",
            "returned_model",
            "attempts",
        ]
        writer.writerow(
            [
                "模型",
                "协议",
                "状态",
                "耗时毫秒",
                "HTTP 状态",
                "回复",
                "错误",
                "错误类型",
                "返回模型",
                "请求次数",
            ]
        )
        for result in snapshot["results"]:
            writer.writerow([csv_cell(result.get(column)) for column in columns])
        return Response(
            "\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8", headers=headers
        )

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app

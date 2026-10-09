"""管理有界异步检测任务，取消时释放正在使用的连接。"""

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from model_connect.providers import Adapter, model_info
from model_connect.schemas import ProbeRequest, Result


def now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Job:
    id: str
    provider: str
    base_url: str
    created_at: str = field(default_factory=now)
    finished_at: str | None = None
    status: str = "running"
    results: list[Result] = field(default_factory=list)
    task: asyncio.Task | None = field(default=None, repr=False)

    def snapshot(self) -> dict:
        counts = Counter(result.status for result in self.results)
        completed = sum(counts[state] for state in ("success", "failed", "skipped", "cancelled"))
        return {
            "id": self.id,
            "provider": self.provider,
            "base_url": self.base_url,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "total": len(self.results),
            "completed": completed,
            "counts": dict(counts),
            "results": [result.model_dump() for result in self.results],
        }


class JobManager:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.jobs: dict[str, Job] = {}
        self.transport = transport

    def create(self, request: ProbeRequest) -> Job:
        if sum(job.status == "running" for job in self.jobs.values()) >= 3:
            raise ValueError("已有 3 个任务正在运行，请等待完成或取消后再开始")
        # 完成的旧任务不持有密钥，限制数量以控制本地内存占用。
        while len(self.jobs) >= 20:
            oldest = next((key for key, job in self.jobs.items() if job.status != "running"), None)
            if oldest is None:
                break
            del self.jobs[oldest]
        job = Job(
            id=uuid4().hex,
            provider=request.connection.provider,
            base_url=request.connection.base_url,
        )
        details = {item.id: item for item in request.model_details}
        for name in request.models:
            info = details.get(name) or model_info(name, request.connection.provider)
            for protocol in request.protocols():
                result = Result(model=name, protocol=protocol)
                if info.supported is False and not request.force:
                    result.status, result.error_code, result.error = (
                        "skipped",
                        "not_applicable",
                        info.reason or "不适用文本生成检测",
                    )
                job.results.append(result)
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job, request), name=f"probe-{job.id}")
        return job

    async def _run(self, job: Job, request: ProbeRequest) -> None:
        queue: asyncio.Queue[int] = asyncio.Queue()
        for index, result in enumerate(job.results):
            if result.status == "pending":
                queue.put_nowait(index)
        workers: list[asyncio.Task] = []
        try:
            async with httpx.AsyncClient(
                transport=self.transport, follow_redirects=False
            ) as client:
                adapter = Adapter(request.connection, client)

                async def worker() -> None:
                    while not queue.empty():
                        index = queue.get_nowait()
                        result = job.results[index]
                        result.status = "running"
                        try:
                            job.results[index] = await adapter.probe(
                                result.model, result.protocol, request
                            )
                        finally:
                            queue.task_done()

                workers = [
                    asyncio.create_task(worker())
                    for _ in range(min(request.concurrency, queue.qsize()))
                ]
                if workers:
                    await asyncio.gather(*workers)
            job.status = "completed"
        except asyncio.CancelledError:
            job.status = "cancelled"
        except Exception:
            # 内部异常不输出请求配置，以免将凭据写入日志。
            job.status = "failed"
            for result in job.results:
                if result.status in {"pending", "running"}:
                    result.status, result.error_code, result.error = (
                        "failed",
                        "internal_error",
                        "任务执行发生内部错误",
                    )
        finally:
            for task in workers:
                if not task.done():
                    task.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            for result in job.results:
                if result.status in {"pending", "running"}:
                    result.status = "cancelled"
            job.finished_at = now()
            job.task = None

    async def cancel(self, job: Job) -> None:
        if job.task and not job.task.done():
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
            # 尚未获得运行机会的协程不会执行 finally，由取消入口补全。
            if job.status == "running":
                job.status, job.finished_at, job.task = "cancelled", now(), None
                for result in job.results:
                    if result.status in {"pending", "running"}:
                        result.status = "cancelled"

    async def shutdown(self) -> None:
        await asyncio.gather(*(self.cancel(job) for job in list(self.jobs.values())))

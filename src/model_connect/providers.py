"""按 provider 原生协议获取模型并发送真实文本生成请求。"""

import asyncio
import re
import time
from urllib.parse import quote

import httpx

from model_connect.schemas import Connection, ModelInfo, ProbeRequest, Result


class ProviderError(Exception):
    """保存可向本地界面展示的上游错误。"""

    def __init__(self, message: str, code: str = "provider_error", http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def classify_error(status: int, code: str, message: str) -> str:
    detail = (code + " " + message).lower()
    if any(
        word in detail
        for word in (
            "insufficient_quota",
            "credit balance",
            "billing",
            "quota exceeded",
            "resource_exhausted",
        )
    ):
        # Google RESOURCE_EXHAUSTED 也可能表示速率限制，额度信息优先。
        if "resource_exhausted" in detail and not any(
            w in detail for w in ("quota", "billing", "credit")
        ):
            return "rate_limited"
        return "quota_exceeded"
    if status == 401:
        return "authentication_failed"
    if status == 403:
        return "permission_denied"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "server_error"
    if status in {400, 405, 422}:
        return "unsupported_request"
    return "provider_error"


def unpack(response: httpx.Response, connection: Connection) -> dict:
    """先解析错误，再验证成功响应的结构。"""
    try:
        data = response.json()
    except ValueError:
        data = None
    if not response.is_success or (isinstance(data, dict) and data.get("error")):
        error = data.get("error", data) if isinstance(data, dict) else response.text
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("detail") or error)
            code = str(error.get("code") or error.get("type") or error.get("status") or "")
        else:
            message, code = str(error), ""
        raise ProviderError(
            connection.redact(message) or f"上游返回 HTTP {response.status_code}",
            classify_error(response.status_code, code, message),
            response.status_code,
        )
    if not isinstance(data, dict):
        raise ProviderError(
            "上游响应不是有效的 JSON 对象", "invalid_response", response.status_code
        )
    return data


def model_info(model_id: str, provider: str, item: dict | None = None) -> ModelInfo:
    """已知专用模型不使用文本提示词探测，未知名称仍允许调用。"""
    item = item or {}
    model_id = model_id.removeprefix("models/") if provider == "google" else model_id
    methods = item.get("supportedGenerationMethods", [])
    methods = methods if isinstance(methods, list) else []
    supported, reason = None, ""
    if provider == "google" and "supportedGenerationMethods" in item:
        supported = "generateContent" in methods
        if not supported:
            reason = "模型未声明支持 generateContent 文本生成"
    special = re.search(
        r"(^|[/_.-])(embedding|embeddings|embed|whisper|tts|dall-e|imagen|veo|sora|"
        r"moderation|realtime|transcribe|transcription|audio|image)([/_.-]|$)",
        model_id.lower(),
    )
    if special:
        supported, reason = False, "专用模型需要嵌入、音频、图像或实时检测方式"
    return ModelInfo(
        id=model_id,
        name=str(item.get("displayName") or item.get("display_name") or model_id),
        supported=supported,
        reason=reason,
        methods=[str(method) for method in methods],
    )


class Adapter:
    def __init__(self, connection: Connection, client: httpx.AsyncClient):
        self.connection = connection
        self.client = client

    def headers(self) -> dict[str, str]:
        headers = {"accept": "application/json", "content-type": "application/json"}
        key = self.connection.api_key.get_secret_value()
        if self.connection.provider == "anthropic":
            if key:
                headers["x-api-key"] = key
            headers["anthropic-version"] = self.connection.anthropic_version
        elif self.connection.provider == "google":
            if key:
                headers["x-goog-api-key"] = key
        elif key:
            headers["authorization"] = f"Bearer {key}"
        headers.update({name.lower(): value for name, value in self.connection.headers.items()})
        return headers

    def url(self, path: str) -> str:
        return self.connection.base_url + "/" + path.lstrip("/")

    async def list_models(self, request_timeout: float = 30) -> list[ModelInfo]:
        models: dict[str, ModelInfo] = {}
        params: dict[str, str | int] = {}
        provider = self.connection.provider
        if provider == "anthropic":
            params["limit"] = 1000
        elif provider == "google":
            params["pageSize"] = 1000
        seen: set[str] = set()
        # 限制总页数和模型数量，防止异常网关无限返回重复分页。
        for _ in range(100):
            response = await self.client.get(
                self.url(self.connection.list_path or "models"),
                headers=self.headers(),
                params=params,
                timeout=request_timeout,
            )
            data = unpack(response, self.connection)
            items = data.get("models" if provider == "google" else "data")
            if not isinstance(items, list):
                raise ProviderError("模型列表响应缺少有效的 models/data 数组", "invalid_response")
            for item in items:
                if not isinstance(item, dict):
                    raise ProviderError("模型列表包含无效条目", "invalid_response")
                model_id = item.get("name" if provider == "google" else "id")
                if not isinstance(model_id, str) or not model_id:
                    raise ProviderError("模型列表条目缺少名称或 ID", "invalid_response")
                info = model_info(model_id, provider, item)
                models[info.id] = info
            if len(models) > 3000:
                raise ProviderError("模型数量超过 3000，请缩小上游列表范围", "too_many_models")
            cursor = ""
            if provider == "google":
                cursor = str(data.get("nextPageToken") or "")
                if cursor:
                    params["pageToken"] = cursor
            elif provider == "anthropic" and data.get("has_more"):
                cursor = str(data.get("last_id") or "")
                if not cursor:
                    raise ProviderError(
                        "模型列表声明还有下一页，但缺少 last_id", "invalid_response"
                    )
                params["after_id"] = cursor
            if not cursor:
                return sorted(models.values(), key=lambda model: model.id.casefold())
            if cursor in seen:
                raise ProviderError("上游重复返回分页游标，已停止获取", "invalid_response")
            seen.add(cursor)
        raise ProviderError("模型列表分页超过 100 页，已停止获取", "too_many_pages")

    def request_body(self, model: str, protocol: str, request: ProbeRequest) -> tuple[str, dict]:
        if protocol == "responses":
            path = "responses"
            body = {
                "model": model,
                "input": request.prompt,
                "max_output_tokens": request.max_tokens,
                "store": False,
            }
        elif protocol == "chat":
            path = "chat/completions"
            token_key = (
                "max_completion_tokens" if self.connection.provider == "openai" else "max_tokens"
            )
            body = {
                "model": model,
                "messages": [{"role": "user", "content": request.prompt}],
                token_key: request.max_tokens,
            }
        elif protocol == "messages":
            path = "messages"
            body = {
                "model": model,
                "messages": [{"role": "user", "content": request.prompt}],
                "max_tokens": request.max_tokens,
            }
        else:
            encoded = quote(model.removeprefix("models/"), safe="")
            path = f"models/{encoded}:generateContent"
            body = {
                "contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
                "generationConfig": {"maxOutputTokens": request.max_tokens},
            }
        override = self.connection.probe_path
        if override:
            path = override.replace("{model}", quote(model.removeprefix("models/"), safe=""))
        return path, body

    def extract(self, data: dict, protocol: str) -> tuple[str, dict, str]:
        """只提取用户可见文本，思考内容不能代替成功回复。"""
        if protocol == "chat":
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ProviderError("Chat Completions 响应缺少 choices", "invalid_response")
            message = choices[0].get("message", {})
            if message.get("refusal") or choices[0].get("finish_reason") == "content_filter":
                raise ProviderError("模型拒绝或过滤了测试提示词", "refused")
            content = message.get("content")
            text = content if isinstance(content, str) else self.text_blocks(content)
            stop = choices[0].get("finish_reason", "")
        elif protocol == "responses":
            if not isinstance(data.get("output"), list):
                raise ProviderError("Responses 响应缺少 output", "invalid_response")
            if data.get("status") in {"failed", "cancelled", "queued", "in_progress"}:
                raise ProviderError("Responses 请求未完成", "incomplete_response")
            contents = [
                block
                for item in data["output"]
                if isinstance(item, dict) and item.get("type") == "message"
                for block in item.get("content", [])
            ]
            if any(
                isinstance(block, dict) and block.get("type") == "refusal" for block in contents
            ):
                raise ProviderError("模型拒绝了测试提示词", "refused")
            text = self.text_blocks(contents)
            stop = data.get("incomplete_details") or ""
        elif protocol == "messages":
            if not isinstance(data.get("content"), list):
                raise ProviderError("Messages 响应缺少 content", "invalid_response")
            if data.get("stop_reason") == "refusal":
                raise ProviderError("模型拒绝了测试提示词", "refused")
            text = self.text_blocks(data["content"])
            stop = data.get("stop_reason", "")
        else:
            candidates = data.get("candidates")
            if data.get("promptFeedback", {}).get("blockReason"):
                raise ProviderError("Google 拦截了测试提示词", "refused")
            if not isinstance(candidates, list) or not candidates:
                raise ProviderError("Google 响应缺少 candidates", "invalid_response")
            if candidates[0].get("finishReason") in {
                "SAFETY",
                "RECITATION",
                "BLOCKLIST",
                "PROHIBITED_CONTENT",
            }:
                raise ProviderError("Google 拒绝或过滤了测试提示词", "refused")
            text = self.text_blocks(candidates[0].get("content", {}).get("parts", []))
            stop = candidates[0].get("finishReason", "")
        if not text.strip():
            detail = "返回了空文本"
            if any(
                term in str(stop).lower() for term in ("length", "max_tokens", "max_output_tokens")
            ):
                detail += "，输出 token 预算可能已被思考过程耗尽，请提高上限后重试"
            raise ProviderError(detail, "empty_response")
        usage = data.get("usage") or data.get("usageMetadata") or {}
        return (
            text.strip(),
            usage if isinstance(usage, dict) else {},
            str(data.get("model") or data.get("modelVersion") or ""),
        )

    @staticmethod
    def text_blocks(blocks: object) -> str:
        if not isinstance(blocks, list):
            return ""
        return "\n".join(
            block["text"]
            for block in blocks
            if isinstance(block, dict)
            and isinstance(block.get("text"), str)
            and not block.get("thought")
            and block.get("type", "text") in {"text", "output_text"}
        )

    async def probe(self, model: str, protocol: str, request: ProbeRequest) -> Result:
        result = Result(model=model, protocol=protocol)
        start = time.monotonic()
        path, body = self.request_body(model, protocol, request)
        token_fallback = False
        retries = 0
        while True:
            result.attempts += 1
            try:
                # 同时约束总请求时间和 HTTPX 的连接/读取时间。
                async with asyncio.timeout(request.timeout):
                    response = await self.client.post(
                        self.url(path), headers=self.headers(), json=body, timeout=request.timeout
                    )
                result.http_status = response.status_code
                data = unpack(response, self.connection)
                text, usage, returned_model = self.extract(data, protocol)
                result.status = "success"
                result.text = self.connection.redact(text)
                result.returned_model = self.connection.redact(returned_model)
                result.usage = self.connection.sanitize(usage)
                result.error = result.error_code = ""
                break
            except (httpx.TimeoutException, TimeoutError):
                error = ProviderError(f"请求超过 {request.timeout:g} 秒", "timeout")
            except httpx.RequestError as exc:
                error = ProviderError(
                    self.connection.redact(
                        f"网络连接失败：{type(exc).__name__}；请检查地址、代理和网络"
                    ),
                    "network_error",
                )
            except ProviderError as exc:
                error = exc
            except (TypeError, KeyError, AttributeError, ValueError) as exc:
                error = ProviderError(f"响应结构异常：{type(exc).__name__}", "invalid_response")
            # 只在服务明确拒绝 token 参数时切换参数，不切换检测协议。
            detail = str(error).lower()
            if (
                protocol == "chat"
                and not token_fallback
                and error.code == "unsupported_request"
                and "max_tokens" in detail
                and any(
                    term in detail for term in ("unsupported", "not supported", "use", "不支持")
                )
            ):
                if "max_tokens" in body:
                    body["max_completion_tokens"] = body.pop("max_tokens")
                    token_fallback = True
                    continue
            result.error, result.error_code = self.connection.redact(str(error)), error.code
            if error.http_status:
                result.http_status = error.http_status
            if (
                error.code not in {"timeout", "network_error", "rate_limited", "server_error"}
                or retries >= request.retries
            ):
                result.status = "failed"
                break
            retries += 1
            await asyncio.sleep(min(2 ** (retries - 1), 4))
        result.latency_ms = round((time.monotonic() - start) * 1000)
        return result

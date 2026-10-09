"""定义连接配置、筛选规则、检测任务与统一结果。"""

import fnmatch
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

Provider = Literal["openai", "openai_compatible", "anthropic", "google"]
Protocol = Literal["chat", "responses", "messages", "generateContent"]

PROVIDERS = {
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1"},
    "openai_compatible": {"label": "OpenAI 兼容 / 中转", "base_url": ""},
    "anthropic": {"label": "Anthropic", "base_url": "https://api.anthropic.com/v1"},
    "google": {
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
    },
}


class Connection(BaseModel):
    """连接信息仅用于当前请求或任务，不持久化密钥。"""

    model_config = ConfigDict(extra="forbid")
    provider: Provider = "openai_compatible"
    base_url: str = Field(default="", max_length=2048)
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    list_path: str = Field(default="models", max_length=512)
    probe_path: str = Field(default="", max_length=512)
    headers: dict[str, str] = Field(default_factory=dict)
    anthropic_version: str = "2023-06-01"

    @field_validator("list_path", "probe_path")
    @classmethod
    def check_path(cls, value: str) -> str:
        """路径相对于 API 根目录，不能覆盖为其他域名。"""
        value = value.strip().lstrip("/")
        if "://" in value or "?" in value or "#" in value or ".." in value.split("/"):
            raise ValueError("接口路径必须是相对路径，不能包含域名、查询参数或上级目录")
        return value

    @field_validator("headers")
    @classmethod
    def check_headers(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 30:
            raise ValueError("附加请求头不能超过 30 项")
        for name, content in value.items():
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+", name) or any(
                ord(c) < 32 or ord(c) == 127 for c in content
            ):
                raise ValueError("请求头名称不能为空，也不能包含换行")
            if name.lower() in {"host", "content-length", "connection"}:
                raise ValueError("不能覆盖 Host、Content-Length 或 Connection 请求头")
            try:
                name.encode("ascii")
                content.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError("请求头只能包含 ASCII 字符") from exc
        return value

    @field_validator("api_key")
    @classmethod
    def check_key(cls, value: SecretStr) -> SecretStr:
        key = value.get_secret_value().strip()
        if any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise ValueError("API Key 不能包含空白、控制字符或非 ASCII 字符")
        return SecretStr(key)

    @model_validator(mode="after")
    def normalize(self) -> "Connection":
        raw = self.base_url.strip() or PROVIDERS[self.provider]["base_url"]
        parts = urlsplit(raw)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("请填写有效的 http:// 或 https:// API 根地址")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("API 根地址不能包含用户名、密码、查询参数或片段")
        try:
            _ = parts.port
        except ValueError as exc:
            raise ValueError("API 根地址的端口无效") from exc
        raw = raw.rstrip("/")
        # 仅在根路径为空时补版本，网关已有前缀保持原样。
        if parts.path in {"", "/"}:
            raw += "/v1beta" if self.provider == "google" else "/v1"
        self.base_url = raw
        self.api_key = SecretStr(self.api_key.get_secret_value().strip())
        return self

    def redact(self, text: str) -> str:
        """上游错误和回复也可能回显凭据，统一脱敏后再展示。"""
        secrets = [self.api_key.get_secret_value(), *self.headers.values()]
        for name, value in self.headers.items():
            if name.lower() == "authorization" and " " in value:
                secrets.append(value.split(" ", 1)[1])
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                text = text.replace(secret, "[已隐藏]")
        return text[:6000]

    def sanitize(self, value: object, depth: int = 0) -> object:
        """对用量等嵌套结构逐项脱敏，避免截断 JSON 后无法解析。"""
        if depth > 5:
            return "[内容过深，已省略]"
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {
                self.redact(str(key)): self.sanitize(item, depth + 1)
                for key, item in list(value.items())[:40]
            }
        if isinstance(value, list):
            return [self.sanitize(item, depth + 1) for item in value[:40]]
        return value


class ModelInfo(BaseModel):
    id: str
    name: str = ""
    supported: bool | None = None
    reason: str = ""
    methods: list[str] = Field(default_factory=list)


class ModelFilter(BaseModel):
    mode: Literal["contains", "prefix", "suffix", "glob", "exact"] = "contains"
    pattern: str = Field(default="", max_length=4096)
    case_sensitive: bool = False

    def matches(self, name: str) -> bool:
        """多行规则按或组合，空规则匹配全部模型。"""
        patterns = [line.strip() for line in self.pattern.splitlines() if line.strip()]
        if not patterns:
            return True
        if not self.case_sensitive:
            name = name.casefold()
            patterns = [pattern.casefold() for pattern in patterns]
        for pattern in patterns:
            if self.mode == "contains" and pattern in name:
                return True
            if self.mode == "prefix" and name.startswith(pattern):
                return True
            if self.mode == "suffix" and name.endswith(pattern):
                return True
            if self.mode == "glob" and fnmatch.fnmatchcase(name, pattern):
                return True
            if self.mode == "exact" and name == pattern:
                return True
        return False


class ListRequest(BaseModel):
    connection: Connection
    timeout: float = Field(default=30, ge=1, le=300)


class FilterRequest(BaseModel):
    models: list[ModelInfo] = Field(max_length=3000)
    filter: ModelFilter = Field(default_factory=ModelFilter)


class ProbeRequest(BaseModel):
    connection: Connection
    models: list[str] = Field(min_length=1, max_length=1000)
    model_details: list[ModelInfo] = Field(default_factory=list, max_length=1000)
    protocol: Literal["default", "chat", "responses", "both"] = "default"
    prompt: str = Field(default="只回复hi", min_length=1, max_length=4000)
    concurrency: int = Field(default=5, ge=1, le=20)
    timeout: float = Field(default=30, ge=1, le=300)
    max_tokens: int = Field(default=64, ge=16, le=8192)
    retries: int = Field(default=0, ge=0, le=3)
    force: bool = False

    @field_validator("models")
    @classmethod
    def check_models(cls, values: list[str]) -> list[str]:
        result = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not result or any(len(value) > 512 for value in result):
            raise ValueError("请至少选择一个模型，每个模型名称不能超过 512 字符")
        return result

    @model_validator(mode="after")
    def check_protocol(self) -> "ProbeRequest":
        if self.connection.provider in {"anthropic", "google"} and self.protocol != "default":
            raise ValueError("Anthropic 与 Google 使用原生协议，请选择默认调用方式")
        return self

    def protocols(self) -> list[Protocol]:
        if self.connection.provider == "anthropic":
            return ["messages"]
        if self.connection.provider == "google":
            return ["generateContent"]
        if self.protocol == "both":
            return ["chat", "responses"]
        if self.protocol in {"chat", "responses"}:
            return [self.protocol]
        # OpenAI 与兼容网关统一默认检测 Chat Completions。
        return ["chat"]


class Result(BaseModel):
    model: str
    protocol: str
    status: str = "pending"
    latency_ms: int | None = None
    http_status: int | None = None
    text: str = ""
    error: str = ""
    error_code: str = ""
    returned_model: str = ""
    attempts: int = 0
    usage: dict = Field(default_factory=dict)

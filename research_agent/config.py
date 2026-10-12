"""Read and atomically save local AI settings without executing dotenv content."""
from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Mapping
from urllib.parse import urlsplit


_ENV_FIELDS = {
    "AI_PROVIDER": "provider",
    "AI_BASE_URL": "base_url",
    "AI_MODEL": "model",
    "AI_API_KEY": "api_key",
    "AI_TIMEOUT_SECONDS": "timeout_seconds",
    "AI_MAX_STEPS": "max_steps",
    "AI_MAX_TOOL_CALLS": "max_tool_calls",
    "AI_MAX_OUTPUT_TOKENS": "max_output_tokens",
    "AI_CONNECTION_MODE": "connection_mode",
    "AI_LOCAL_RUNTIME": "local_runtime",
}
_INTEGER_FIELDS = {"max_steps", "max_tool_calls", "max_output_tokens"}
_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


class ConfigError(ValueError):
    """Invalid local AI configuration; messages never include saved secrets."""


def is_loopback_url(base_url: str) -> bool:
    try:
        host = urlsplit(base_url).hostname or ""
        if host.lower() == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_base_url(base_url: str) -> str:
    """Allow HTTPS providers and HTTP loopback servers, without URL credentials."""
    value = str(base_url).strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        port = parts.port
        del port
        if (
            not parts.hostname or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or "?" in value or "#" in value
            or any(character.isspace() or ord(character) < 32 for character in value)
            or "\\" in value
        ):
            raise ValueError
        if parts.scheme != "https" and not (parts.scheme == "http" and is_loopback_url(value)):
            raise ValueError
    except (ValueError, TypeError):
        raise ConfigError("模型服务地址必须使用 HTTPS；本机 localhost 或回环 IP 可使用 HTTP，地址中不能包含账号、密码、查询参数或片段。") from None
    return value.rstrip("/")


@dataclass(frozen=True)
class AgentConfig:
    provider: str = "openai_compatible"
    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    timeout_seconds: float = 45
    max_steps: int = 6
    max_tool_calls: int = 12
    max_output_tokens: int = 1600
    connection_mode: str = "auto"
    local_runtime: str = "custom"

    @property
    def effective_mode(self) -> str:
        """Keep old configs working while making local/cloud selection explicit."""
        if self.connection_mode == "auto":
            return "local" if is_loopback_url(self.base_url) else "cloud"
        return self.connection_mode

    @property
    def missing(self) -> list[str]:
        missing = []
        if not self.base_url.strip():
            missing.append("服务地址")
        if not self.model.strip():
            missing.append("模型名称")
        if self.base_url.strip() and self.effective_mode == "cloud" and not self.api_key.strip():
            missing.append("API Key")
        return missing

    @property
    def missing_fields(self) -> list[str]:
        return self.missing

    @property
    def ready(self) -> bool:
        try:
            validate_config(self)
            return not self.missing
        except (ConfigError, TypeError, AttributeError):
            return False


def validate_config(config: AgentConfig) -> AgentConfig:
    if config.provider != "openai_compatible":
        raise ConfigError("当前版本支持 OpenAI 兼容接口，请选择 openai_compatible。")
    for name in ("base_url", "model", "api_key"):
        if not isinstance(getattr(config, name), str):
            raise ConfigError("模型地址、名称和密钥必须是文本。")
    if not isinstance(config.connection_mode, str) or config.connection_mode not in ("auto", "local", "cloud"):
        raise ConfigError("模型连接方式必须选择自动、本地模型或云端服务。")
    if not isinstance(config.local_runtime, str) or config.local_runtime not in ("custom", "ollama", "lm_studio"):
        raise ConfigError("本地运行方式必须选择自定义、Ollama 或 LM Studio。")
    validate_base_url(config.base_url)
    if config.base_url.strip():
        loopback = is_loopback_url(config.base_url)
        if config.connection_mode == "local" and not loopback:
            raise ConfigError("本地模型地址必须使用 localhost 或本机回环 IP。")
        if config.connection_mode == "cloud" and loopback:
            raise ConfigError("云端服务不能使用本机回环地址，请选择本地模型。")
    if any(ord(char) < 32 for char in config.api_key):
        raise ConfigError("API Key 不能包含换行或控制字符。")
    if any(ord(char) < 32 for char in config.model) or len(config.model) > 256:
        raise ConfigError("模型名称不合法。")
    if isinstance(config.timeout_seconds, bool) or not isinstance(config.timeout_seconds, (int, float)) or not math.isfinite(config.timeout_seconds) or not 1 <= config.timeout_seconds <= 180:
        raise ConfigError("请求超时必须在 1～180 秒之间。")
    for name, minimum, maximum in (
        ("max_steps", 1, 12), ("max_tool_calls", 1, 32), ("max_output_tokens", 128, 8192)
    ):
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ConfigError(f"{name} 必须是 {minimum}～{maximum} 之间的整数。")
    return config


def _decode_value(raw: str) -> str:
    value = raw.strip()
    try:
        if value.startswith('"'):
            parsed = json.loads(value)
            if not isinstance(parsed, str):
                raise ValueError
            return parsed
        if value.startswith("'"):
            if len(value) < 2 or not value.endswith("'"):
                raise ValueError
            return value[1:-1]
        return value
    except (ValueError, TypeError):
        raise ConfigError(".env.local 中的 AI 配置格式不正确，请在模型设置中重新保存。") from None


def load_agent_config(root, environ: Mapping[str, str] | None = None) -> AgentConfig:
    """Read only root/.env.local; nonempty process AI_* values take precedence."""
    path = Path(root) / ".env.local"
    values = {}
    if path.is_file():
        try:
            source = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            raise ConfigError("无法读取本地 AI 配置文件，请检查文件权限和编码。") from None
        for line in source.splitlines():
            matched = _ASSIGNMENT.match(line)
            if matched and matched.group(1) in _ENV_FIELDS:
                values[_ENV_FIELDS[matched.group(1)]] = _decode_value(matched.group(2))
    source_env = os.environ if environ is None else environ
    for env_name, field_name in _ENV_FIELDS.items():
        value = source_env.get(env_name, "")
        if value and str(value).strip():
            values[field_name] = str(value)
    try:
        for name in _INTEGER_FIELDS:
            if name in values:
                values[name] = int(values[name])
        if "timeout_seconds" in values:
            values["timeout_seconds"] = float(values["timeout_seconds"])
    except (TypeError, ValueError):
        raise ConfigError("AI 超时、步数、工具次数和输出长度必须填写数值。") from None
    for name in ("provider", "base_url", "model", "connection_mode", "local_runtime"):
        if name in values:
            values[name] = values[name].strip()
    return validate_config(AgentConfig(**values))


def save_agent_config(root, config: AgentConfig) -> AgentConfig:
    """Atomically save owned AI keys, retaining unrelated local settings verbatim."""
    validate_config(config)
    root_path = Path(root)
    path = root_path / ".env.local"
    try:
        old_text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
        kept = []
        for line in old_text.splitlines(keepends=True):
            matched = _ASSIGNMENT.match(line.rstrip("\r\n"))
            if not matched or matched.group(1) not in _ENV_FIELDS:
                kept.append(line)
        text = "".join(kept)
        if text and not text.endswith(("\n", "\r")):
            text += "\n"
        for env_name, field_name in _ENV_FIELDS.items():
            value = getattr(config, field_name)
            # JSON string syntax safely round-trips spaces, quotes, #, $, and backslashes.
            encoded = json.dumps(str(value), ensure_ascii=False)
            text += f"{env_name}={encoded}\n"
        root_path.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".env.local.", suffix=".tmp", dir=root_path)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                output.write(text)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except (OSError, UnicodeError):
        raise ConfigError("无法保存本地 AI 配置，请检查项目目录的写入权限。") from None
    return config

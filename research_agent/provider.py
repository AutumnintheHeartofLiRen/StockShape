"""Bounded, cancellable OpenAI-compatible Chat Completions transport."""
from __future__ import annotations

import json
import queue
import re
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .config import AgentConfig, ConfigError, validate_base_url, validate_config


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ProviderError(RuntimeError):
    """User-readable transport/protocol failure, without remote response details."""


class AgentCancelled(RuntimeError):
    """The user cancelled the current research request."""


class _RetryableError(ProviderError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _is_cancelled(cancel) -> bool:
    if cancel is None:
        return False
    if callable(cancel):
        return bool(cancel())
    if hasattr(cancel, "is_set"):
        return bool(cancel.is_set())
    return bool(cancel)


def _check_cancel(cancel) -> None:
    if _is_cancelled(cancel):
        raise AgentCancelled("研究已停止。")


def _endpoint(base_url: str) -> str:
    base = validate_base_url(base_url)
    parts = urlsplit(base)
    if parts.path.rstrip("/").endswith("/chat/completions"):
        return base
    if not parts.path or parts.path == "/":
        return base + "/v1/chat/completions"
    return base + "/chat/completions"


class ChatCompletionsProvider:
    def __init__(self, config: AgentConfig):
        try:
            validate_config(config)
        except ConfigError as error:
            raise ProviderError(str(error)) from None
        if not config.ready:
            raise ProviderError("请先在模型设置中填写：" + "、".join(config.missing) + "。")
        self.config = config
        self.endpoint = _endpoint(config.base_url)

    def complete(self, messages, tools, cancel=None) -> dict:
        """Return one validated assistant message; no tools are executed here."""
        _check_cancel(cancel)
        payload = {
            "model": self.config.model.strip(), "messages": messages,
            "max_tokens": self.config.max_output_tokens,
        }
        if tools:
            payload["tools"] = tools
            # Local OpenAI-compatible servers vary in tool_choice support (Ollama
            # accepts tools but uses its own default selection). Keep the tools
            # available without sending an optional field they may reject.
            if self.config.effective_mode == "cloud":
                payload["tool_choice"] = "auto"
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError):
            raise ProviderError("研究请求格式不正确，无法发送到模型服务。") from None
        for attempt in range(2):
            _check_cancel(cancel)
            try:
                result = self._cancellable_request(body, cancel)
                _check_cancel(cancel)
                return self._normalize(result)
            except _RetryableError as error:
                if attempt:
                    raise ProviderError(str(error)) from None
                deadline = time.monotonic() + 0.4
                while time.monotonic() < deadline:
                    _check_cancel(cancel)
                    time.sleep(0.04)
        raise ProviderError("模型服务暂时不可用，请稍后重试。")

    def _cancellable_request(self, body: bytes, cancel) -> dict:
        # urllib is blocking. Keep the UI/agent cancellation responsive while the bounded
        # daemon transport exits on its socket timeout; cancelled results are discarded.
        results = queue.Queue(maxsize=1)
        abandoned = threading.Event()

        def request():
            try:
                result = self._request(body, abandoned)
                results.put((True, result))
            except Exception as error:
                results.put((False, error))

        threading.Thread(target=request, name="stockshape-model-request", daemon=True).start()
        deadline = time.monotonic() + self.config.timeout_seconds
        while True:
            if _is_cancelled(cancel):
                abandoned.set()
                raise AgentCancelled("研究已停止。")
            if time.monotonic() >= deadline:
                abandoned.set()
                raise _RetryableError("模型请求超时，请检查网络连接或增加请求超时。")
            try:
                success, result = results.get(timeout=0.05)
            except queue.Empty:
                continue
            if success:
                return result
            if isinstance(result, (ProviderError, AgentCancelled)):
                raise result from None
            raise ProviderError("模型请求未完成，请检查服务地址与网络连接。") from None

    def _request(self, body: bytes, abandoned: threading.Event) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = "Bearer " + self.config.api_key
        request = Request(self.endpoint, data=body, headers=headers, method="POST")
        response = None
        try:
            _check_cancel(abandoned)
            handlers = [_NoRedirect()]
            if self.config.effective_mode == "local":
                # A configured system/environment proxy must never receive local
                # research payloads or credentials meant for a loopback service.
                handlers.append(ProxyHandler({}))
            response = build_opener(*handlers).open(request, timeout=self.config.timeout_seconds)
            _check_cancel(abandoned)
            length = response.headers.get("Content-Length")
            if length:
                try:
                    if int(length) > MAX_RESPONSE_BYTES:
                        raise ProviderError("模型响应过大，已停止读取；请减少请求内容或输出长度。")
                except ValueError:
                    pass
            output = bytearray()
            while True:
                _check_cancel(abandoned)
                chunk = response.read(min(65536, MAX_RESPONSE_BYTES + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > MAX_RESPONSE_BYTES:
                    raise ProviderError("模型响应过大，已停止读取；请减少请求内容或输出长度。")
            try:
                result = json.loads(output.decode("utf-8"))
            except (UnicodeError, ValueError):
                raise ProviderError("模型服务返回了无法解析的响应，请检查是否为 Chat Completions 兼容接口。") from None
            if not isinstance(result, dict):
                raise ProviderError("模型服务响应格式不正确。")
            return result
        except HTTPError as error:
            code = error.code
            error.close()
            if code in (401, 403):
                raise ProviderError("模型服务拒绝认证，请检查 API Key 和账户权限。") from None
            if code == 404:
                raise ProviderError("模型接口不存在，请检查服务地址和模型名称。") from None
            if 300 <= code < 400:
                raise ProviderError("模型服务要求跳转地址，已停止请求；请直接填写最终 HTTPS 接口地址。") from None
            if code == 429:
                raise _RetryableError("模型服务请求过于频繁或额度不足，请稍后重试或检查账户额度。") from None
            if code in (408, 500, 502, 503, 504):
                raise _RetryableError("模型服务暂时不可用，重试后仍未成功，请稍后再试。") from None
            raise ProviderError("模型服务拒绝了请求，请检查接口兼容性、模型名称与请求限制。") from None
        except (TimeoutError, socket.timeout):
            raise _RetryableError("模型请求超时，请检查网络连接或增加请求超时。") from None
        except URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise _RetryableError("模型请求超时，请检查网络连接或增加请求超时。") from None
            raise _RetryableError("无法连接模型服务，请检查网络和服务地址。") from None
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def _normalize(result: dict) -> dict:
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError("模型响应缺少有效的 choices。")
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            raise ProviderError("模型输出达到长度上限，未执行本轮工具；请增加输出长度或缩小问题范围。")
        if choice.get("finish_reason") == "content_filter":
            raise ProviderError("模型服务未完成这次回答，请调整问题后重试。")
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
            raise ProviderError("模型响应缺少有效的 assistant 消息。")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ProviderError("模型返回的文本格式不受支持。")
        raw_calls = message.get("tool_calls")
        calls = [] if raw_calls is None else raw_calls
        if not isinstance(calls, list) or len(calls) > 32:
            raise ProviderError("模型返回的工具调用格式不正确。")
        normalized = []
        ids = set()
        for call in calls:
            if not isinstance(call, dict):
                raise ProviderError("模型返回的工具调用格式不正确。")
            call_id = call.get("id")
            function = call.get("function")
            if (
                not isinstance(call_id, str) or not call_id or len(call_id) > 256
                or call_id in ids or call.get("type") != "function"
                or not isinstance(function, dict)
                or not isinstance(function.get("name"), str)
                or not _TOOL_NAME.fullmatch(function["name"])
                or not isinstance(function.get("arguments"), str)
            ):
                raise ProviderError("模型返回的工具调用缺少有效的 ID、名称或参数。")
            ids.add(call_id)
            normalized.append({
                "id": call_id, "type": "function",
                "function": {"name": function["name"], "arguments": function["arguments"]},
            })
        if not normalized and (content is None or not content.strip()):
            raise ProviderError("模型返回了空回答，请重试或检查接口兼容性。")
        if choice.get("finish_reason") == "tool_calls" and not normalized:
            raise ProviderError("模型声明了工具调用，但未返回可用的工具信息。")
        assistant = {"role": "assistant", "content": content}
        if normalized:
            assistant["tool_calls"] = normalized
        raw_usage = result.get("usage")
        usage = {}
        if isinstance(raw_usage, dict):
            usage = {name: value for name, value in raw_usage.items() if name in ("prompt_tokens", "completion_tokens", "total_tokens") and isinstance(value, int) and not isinstance(value, bool) and value >= 0}
        return {"message": assistant, "usage": usage}

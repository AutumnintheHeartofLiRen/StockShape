"""Discover model IDs from a bounded, cancellable loopback-only HTTP service."""
from __future__ import annotations

import json
import math
import queue
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .config import ConfigError, is_loopback_url, validate_base_url
from .provider import AgentCancelled


MAX_RESPONSE_BYTES = 512 * 1024
MAX_MODELS = 100
MAX_MODEL_ID_LENGTH = 256


class DiscoveryError(RuntimeError):
    """A safe local-service error; never include remote bodies or credentials."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _cancelled(cancel) -> bool:
    if cancel is None:
        return False
    if callable(cancel):
        return bool(cancel())
    if hasattr(cancel, "is_set"):
        return bool(cancel.is_set())
    return bool(cancel)


def _check_cancel(cancel) -> None:
    if _cancelled(cancel):
        raise AgentCancelled("本地模型检测已停止。")


def _models_endpoint(base_url: str) -> str:
    try:
        if not isinstance(base_url, str):
            raise ConfigError()
        base = validate_base_url(base_url)
    except ConfigError:
        raise DiscoveryError("本地服务地址不合法，请填写本机 localhost 或回环 IP 的 HTTP / HTTPS 地址。") from None
    if not base or not is_loopback_url(base):
        raise DiscoveryError("模型列表检测仅支持本机 localhost 或回环 IP 地址。")
    path = urlsplit(base).path.rstrip("/")
    if not path:
        return base + "/v1/models"
    if path.endswith("/chat/completions"):
        return base[:-len("/chat/completions")] + "/models"
    if path.endswith("/models"):
        return base
    return base + "/models"


def _normalize_models(payload) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise DiscoveryError("本地服务未返回 OpenAI 兼容的模型列表，请检查服务地址。")
    models = []
    seen = set()
    for entry in payload["data"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        raw_model = entry["id"]
        model = raw_model.strip()
        if (
            not model or len(model) > MAX_MODEL_ID_LENGTH
            or any(ord(char) < 32 or ord(char) == 127 for char in raw_model)
            or model in seen
        ):
            continue
        models.append(model)
        seen.add(model)
        if len(models) >= MAX_MODELS:
            break
    if payload["data"] and not models:
        raise DiscoveryError("本地服务返回的模型名称无效，请手动填写模型名称。")
    return models


def _request_models(endpoint: str, api_key: str, timeout: float, abandoned) -> list[str]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    request = Request(endpoint, headers=headers, method="GET")
    response = None
    try:
        _check_cancel(abandoned)
        # A loopback-only probe must not traverse a configured HTTP(S) proxy.
        response = build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout)
        _check_cancel(abandoned)
        length = response.headers.get("Content-Length")
        if length:
            try:
                if int(length) > MAX_RESPONSE_BYTES:
                    raise DiscoveryError("本地模型列表响应过大，已停止读取。")
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
                raise DiscoveryError("本地模型列表响应过大，已停止读取。")
        try:
            payload = json.loads(output.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise DiscoveryError("本地服务返回了无法解析的模型列表，请检查是否启用了 OpenAI 兼容接口。") from None
        return _normalize_models(payload)
    except HTTPError as error:
        code = error.code
        error.close()
        if code in (401, 403):
            raise DiscoveryError("本地服务要求认证，请填写该服务的 API Key。") from None
        if code == 404:
            raise DiscoveryError("本地服务没有模型列表接口，请检查地址，或手动填写模型名称。") from None
        if 300 <= code < 400:
            raise DiscoveryError("本地服务要求跳转，已停止检测；请直接填写最终的本机接口地址。") from None
        raise DiscoveryError("本地服务未能返回模型列表，请检查服务状态。") from None
    except (TimeoutError, socket.timeout):
        raise DiscoveryError("本地模型检测超时，请确认服务已经启动。") from None
    except URLError:
        raise DiscoveryError("无法连接本地模型服务，请确认服务已经启动，并检查端口和地址。") from None
    finally:
        if response is not None:
            response.close()


def discover_local_models(base_url, api_key="", timeout_seconds=5, cancel=None) -> list[str]:
    """GET /models from a local OpenAI-compatible server without blocking cancellation.

    This does not start a model server, download weights, or test tool calling. An
    empty list is a valid response and permits callers to offer manual model entry.
    Caller cancellation returns promptly; an abandoned daemon request exits within
    its socket timeout and cannot update the caller's result.
    """
    _check_cancel(cancel)
    endpoint = _models_endpoint(base_url)
    if (
        not isinstance(api_key, str) or len(api_key) > 8192
        or any(ord(char) < 32 or ord(char) == 127 for char in api_key)
    ):
        raise DiscoveryError("本地服务 API Key 格式不正确。")
    if (
        isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds) or not 0.1 <= timeout_seconds <= 30
    ):
        raise DiscoveryError("本地模型检测超时必须在 0.1～30 秒之间。")
    results = queue.Queue(maxsize=1)
    abandoned = threading.Event()

    def request():
        try:
            results.put((True, _request_models(endpoint, api_key, timeout_seconds, abandoned)))
        except Exception as error:
            results.put((False, error))

    threading.Thread(target=request, name="stockshape-local-model-discovery", daemon=True).start()
    deadline = time.monotonic() + timeout_seconds
    while True:
        if _cancelled(cancel):
            abandoned.set()
            raise AgentCancelled("本地模型检测已停止。")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            abandoned.set()
            raise DiscoveryError("本地模型检测超时，请确认服务已经启动。")
        try:
            success, result = results.get(timeout=min(0.05, remaining))
        except queue.Empty:
            continue
        _check_cancel(cancel)
        if success:
            return result
        if isinstance(result, (DiscoveryError, AgentCancelled)):
            raise result from None
        raise DiscoveryError("本地模型检测未完成，请检查服务地址与接口兼容性。") from None

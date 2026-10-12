"""Synthetic, offline tests for AI configuration and the generic HTTP protocol."""
from __future__ import annotations

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.request import ProxyHandler

from research_agent.config import (
    AgentConfig, ConfigError, load_agent_config, save_agent_config, validate_base_url,
    validate_config,
)
from research_agent.provider import (
    AgentCancelled, ChatCompletionsProvider, ProviderError,
)


def completion(content="合成测试回答", **message_fields):
    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content, **message_fields}}], "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}}


class LocalProviderServer:
    """A loopback fake service; no real model service or credentials are used."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length", "0"))
                payload = self.rfile.read(size)
                outer.requests.append({"path": self.path, "authorization": self.headers.get("Authorization"), "body": json.loads(payload)})
                response = outer.responses.pop(0) if outer.responses else {"json": completion()}
                if response.get("delay"):
                    time.sleep(response["delay"])
                data = response.get("body")
                if data is None:
                    data = json.dumps(response.get("json", completion()), ensure_ascii=False).encode("utf-8")
                self.send_response(response.get("status", 200))
                self.send_header("Content-Length", str(response.get("length", len(data))))
                for key, value in response.get("headers", {}).items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class ConfigTests(unittest.TestCase):
    def test_missing_file_does_not_create_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = load_agent_config(directory, environ={})
            self.assertEqual(config, AgentConfig())
            self.assertFalse(config.ready)
            self.assertIn("服务地址", config.missing)
            self.assertFalse((Path(directory) / ".env.local").exists())

    def test_local_model_does_not_need_key_remote_does(self):
        self.assertTrue(AgentConfig(base_url="http://localhost:11434/v1", model="synthetic").ready)
        self.assertTrue(AgentConfig(base_url="http://[::1]:1234/v1", model="synthetic").ready)
        self.assertFalse(AgentConfig(base_url="https://example.invalid/v1", model="synthetic").ready)
        self.assertIn("API Key", AgentConfig(base_url="https://example.invalid/v1", model="synthetic").missing)
        self.assertTrue(AgentConfig(base_url="https://example.invalid/v1", model="synthetic", api_key="synthetic-key").ready)

    def test_legacy_configs_infer_local_or_cloud_without_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env.local"
            for address, mode in (("http://localhost:11434/v1", "local"), ("https://example.invalid/v1", "cloud")):
                original = f'AI_BASE_URL="{address}"\nAI_MODEL="legacy-model"\n'
                path.write_text(original, encoding="utf-8")
                config = load_agent_config(directory, environ={})
                self.assertEqual(config.connection_mode, "auto")
                self.assertEqual(config.local_runtime, "custom")
                self.assertEqual(config.effective_mode, mode)
                self.assertEqual(config.ready, mode == "local")
                self.assertEqual(path.read_text(encoding="utf-8"), original)
        # New options are appended so existing positional callers retain meaning.
        old_call = AgentConfig("openai_compatible", "http://localhost:1234/v1", "legacy", "", 20, 3, 4, 512)
        self.assertTrue(old_call.ready)
        self.assertEqual(old_call.connection_mode, "auto")

    def test_explicit_local_is_loopback_only_and_cloud_requires_key(self):
        for address in ("http://localhost:11434/v1", "http://127.0.0.1:1234/v1", "http://[::1]:1234/v1", "https://127.0.0.1/v1"):
            with self.subTest(address=address):
                config = AgentConfig(base_url=address, model="synthetic", connection_mode="local", local_runtime="ollama")
                self.assertTrue(config.ready)
                self.assertEqual(config.missing, [])
        remote = AgentConfig(base_url="https://example.invalid/v1", model="synthetic", connection_mode="cloud")
        self.assertFalse(remote.ready)
        self.assertIn("API Key", remote.missing)
        self.assertTrue(replace(remote, api_key="synthetic-key").ready)
        for config in (
            replace(remote, connection_mode="local", api_key="synthetic-key"),
            replace(remote, base_url="http://localhost:11434/v1", api_key="synthetic-key"),
        ):
            with self.subTest(config=config), self.assertRaises(ConfigError):
                validate_config(config)
            self.assertFalse(config.ready)

    def test_connection_and_runtime_round_trip_preserves_unknown_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env.local"
            path.write_text('# Keep my comment\nAI_LOCAL_RUNTIME="custom"\nAI_LOCAL_RUNTIME="ollama"\nAI_FUTURE_SETTING="keep-as-is"\n', encoding="utf-8")
            config = AgentConfig(base_url="http://localhost:1234/v1", model="local-synthetic", connection_mode="local", local_runtime="lm_studio")
            save_agent_config(directory, config)
            loaded = load_agent_config(directory, environ={})
            self.assertEqual(loaded, config)
            self.assertEqual(loaded.provider, "openai_compatible")
            output = path.read_text(encoding="utf-8")
            self.assertEqual(output.count("AI_LOCAL_RUNTIME="), 1)
            self.assertEqual(output.count("AI_CONNECTION_MODE="), 1)
            self.assertIn('AI_FUTURE_SETTING="keep-as-is"', output)
            self.assertIn("# Keep my comment", output)

    def test_mode_environment_override_and_incomplete_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            local = AgentConfig(base_url="http://localhost:1234/v1", model="local-synthetic", connection_mode="local", local_runtime="ollama")
            save_agent_config(directory, local)
            cloud = load_agent_config(directory, environ={"AI_CONNECTION_MODE": " cloud ", "AI_BASE_URL": "https://example.invalid/v1", "AI_API_KEY": "synthetic-key", "AI_LOCAL_RUNTIME": " lm_studio "})
            self.assertEqual(cloud.effective_mode, "cloud")
            self.assertEqual(cloud.local_runtime, "lm_studio")
            self.assertTrue(cloud.ready)
            self.assertEqual(load_agent_config(directory, environ={"AI_CONNECTION_MODE": "", "AI_LOCAL_RUNTIME": ""}), local)
            for mode in ("auto", "cloud", "local"):
                incomplete = AgentConfig(connection_mode=mode, local_runtime="custom")
                save_agent_config(directory, incomplete)
                self.assertEqual(load_agent_config(directory, environ={}), incomplete)
                self.assertFalse(incomplete.ready)
                self.assertIn("服务地址", incomplete.missing)

    def test_invalid_modes_do_not_overwrite_or_echo_private_values(self):
        with tempfile.TemporaryDirectory() as directory:
            good = AgentConfig(base_url="http://localhost:1234/v1", model="synthetic", connection_mode="local", local_runtime="ollama")
            save_agent_config(directory, good)
            path = Path(directory) / ".env.local"
            previous = path.read_bytes()
            for config in (
                replace(good, connection_mode="private-invalid-mode"),
                replace(good, connection_mode=["private-invalid-mode"]),
                replace(good, local_runtime="private-invalid-runtime"),
                replace(good, local_runtime=None),
            ):
                with self.subTest(config=config), self.assertRaises(ConfigError) as raised:
                    save_agent_config(directory, config)
                self.assertNotIn("private-invalid", str(raised.exception))
                self.assertEqual(path.read_bytes(), previous)
            for name in ("AI_CONNECTION_MODE", "AI_LOCAL_RUNTIME"):
                with self.subTest(name=name), self.assertRaises(ConfigError) as raised:
                    load_agent_config(directory, environ={name: "private-invalid-choice"})
                self.assertNotIn("private-invalid", str(raised.exception))

    def test_url_restrictions(self):
        for value in (
            "http://example.invalid/v1", "http://localhost.example.invalid/v1",
            "https://user:secret@example.invalid/v1", "https://example.invalid/v1?secret=1",
            "https://example.invalid/v1#fragment", "https://example.invalid/v1?",
            "file:///tmp/model", "https://example.invalid:broken/v1", "https://[::1/v1",
            "https://example.invalid/\nheader", "https://example.invalid\\@localhost/v1",
        ):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                validate_base_url(value)
        self.assertEqual(validate_base_url(" https://example.invalid/v1/ "), "https://example.invalid/v1")

    def test_safe_round_trip_and_preserve_unrelated_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env.local"
            path.write_text('# Personal setting\nOTHER_TOKEN="keep-this-synthetic-value"\nAI_API_KEY="old"\nAI_API_KEY="duplicate"\nAI_OTHER_SECRET="do-not-touch"\n', encoding="utf-8")
            config = AgentConfig(base_url="https://example.invalid/v1", model="model # one", api_key='synthetic-"\\$# key', timeout_seconds=13, max_steps=4, max_tool_calls=7, max_output_tokens=2500)
            save_agent_config(directory, config)
            self.assertEqual(load_agent_config(directory, environ={}), config)
            output = path.read_text(encoding="utf-8")
            self.assertIn('OTHER_TOKEN="keep-this-synthetic-value"', output)
            self.assertIn('AI_OTHER_SECRET="do-not-touch"', output)
            self.assertEqual(output.count("AI_API_KEY="), 1)
            self.assertNotIn(config.api_key, repr(config))
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_environment_nonempty_precedence_and_literal_no_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env.local"
            path.write_text("AI_BASE_URL='http://localhost:1234/v1'\nAI_MODEL='file-model'\nAI_API_KEY='$(whoami)'\nAI_MAX_STEPS=3\n", encoding="utf-8")
            config = load_agent_config(directory, environ={"AI_MODEL": "env-model", "AI_API_KEY": "", "AI_MAX_STEPS": "5"})
            self.assertEqual(config.model, "env-model")
            self.assertEqual(config.api_key, "$(whoami)")
            self.assertEqual(config.max_steps, 5)

    def test_clear_key_and_invalid_values_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            good = AgentConfig(base_url="http://localhost:1234/v1", model="fake", api_key="synthetic")
            save_agent_config(directory, good)
            save_agent_config(directory, replace(good, api_key=""))
            self.assertEqual(load_agent_config(directory, environ={}).api_key, "")
            path = Path(directory) / ".env.local"
            previous = path.read_bytes()
            for config in (
                replace(good, timeout_seconds=float("nan")), replace(good, max_steps=0),
                replace(good, max_tool_calls=True), replace(good, max_output_tokens=99),
                replace(good, api_key="synthetic\nkey"), replace(good, provider="unknown"),
            ):
                with self.subTest(config=config), self.assertRaises(ConfigError):
                    save_agent_config(directory, config)
                self.assertEqual(path.read_bytes(), previous)

    def test_malformed_config_does_not_echo_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env.local"
            for line in ('AI_API_KEY="sensitive-unclosed\n', 'AI_TIMEOUT_SECONDS="sensitive-number"\n'):
                path.write_text(line, encoding="utf-8")
                with self.assertRaises(ConfigError) as raised:
                    load_agent_config(directory, environ={})
                self.assertNotIn("sensitive", str(raised.exception))


class ProviderTests(unittest.TestCase):
    @staticmethod
    def make_provider(server, **kwargs):
        return ChatCompletionsProvider(AgentConfig(base_url=server.url + "/v1", model="synthetic-model", **kwargs))

    def test_wire_schema_and_usage(self):
        tool = {"type": "function", "function": {"name": "synthetic_tool", "parameters": {"type": "object"}}}
        messages = [{"role": "user", "content": "synthetic prompt"}]
        with LocalProviderServer([{"json": completion()}]) as server:
            result = self.make_provider(server, api_key="synthetic-key").complete(messages, [tool])
            self.assertEqual(result["message"], {"role": "assistant", "content": "合成测试回答"})
            self.assertEqual(result["usage"]["total_tokens"], 20)
            request = server.requests[0]
            self.assertEqual(request["path"], "/v1/chat/completions")
            self.assertEqual(request["authorization"], "Bearer synthetic-key")
            self.assertEqual(request["body"], {"model": "synthetic-model", "messages": messages, "max_tokens": 1600, "tools": [tool]})

    def test_ollama_and_legacy_loopback_keep_tools_without_tool_choice(self):
        tool = {"type": "function", "function": {"name": "synthetic_tool", "parameters": {"type": "object"}}}
        calls = [{"id": "local_call", "type": "function", "function": {"name": "synthetic_tool", "arguments": "{}"}}]
        with LocalProviderServer([{"json": completion(None, tool_calls=calls)}] * 2) as server:
            for options in ({}, {"connection_mode": "local", "local_runtime": "ollama"}):
                result = self.make_provider(server, **options).complete([], [tool])
                self.assertEqual(result["message"]["tool_calls"], calls)
            for request in server.requests:
                self.assertEqual(request["body"]["tools"], [tool])
                self.assertNotIn("tool_choice", request["body"])

    def test_cloud_inferred_and_explicit_modes_keep_auto_tool_choice(self):
        tool = {"type": "function", "function": {"name": "synthetic_tool", "parameters": {"type": "object"}}}
        for mode in ("auto", "cloud"):
            with self.subTest(mode=mode):
                provider = ChatCompletionsProvider(AgentConfig(
                    base_url="https://example.invalid/v1", model="synthetic-model",
                    api_key="synthetic-key", connection_mode=mode,
                ))
                # Capture bytes before the transport; this must never contact a cloud.
                with mock.patch.object(provider, "_cancellable_request", return_value=completion()) as request:
                    provider.complete([], [tool])
                payload = json.loads(request.call_args.args[0])
                self.assertEqual(payload["tools"], [tool])
                self.assertEqual(payload["tool_choice"], "auto")
                with mock.patch.object(provider, "_cancellable_request", return_value=completion()) as request:
                    provider.complete([], [])
                self.assertNotIn("tool_choice", json.loads(request.call_args.args[0]))

    def test_local_requests_bypass_environment_proxy(self):
        with LocalProviderServer([{}]) as proxy, LocalProviderServer([{}]) as local:
            environment = {"HTTP_PROXY": proxy.url, "HTTPS_PROXY": proxy.url, "ALL_PROXY": proxy.url,
                           "http_proxy": proxy.url, "https_proxy": proxy.url, "all_proxy": proxy.url,
                           "NO_PROXY": "", "no_proxy": ""}
            with mock.patch.dict(os.environ, environment):
                self.make_provider(local, api_key="synthetic-local-key").complete([
                    {"role": "user", "content": "synthetic-local-research"}], [])
            self.assertEqual(len(local.requests), 1)
            self.assertEqual(local.requests[0]["authorization"], "Bearer synthetic-local-key")
            self.assertEqual(proxy.requests, [])

    def test_cloud_keeps_default_proxy_behavior_without_real_request(self):
        provider = ChatCompletionsProvider(AgentConfig(
            base_url="https://example.invalid/v1", model="synthetic-model",
            api_key="synthetic-key", connection_mode="cloud",
        ))
        response = mock.Mock()
        response.headers = {}
        response.read.side_effect = [json.dumps(completion()).encode("utf-8"), b""]
        with mock.patch("research_agent.provider.build_opener") as build:
            build.return_value.open.return_value = response
            provider._request(b"{}", threading.Event())
        self.assertFalse(any(isinstance(handler, ProxyHandler) for handler in build.call_args.args))
        response.close.assert_called_once()

    def test_root_and_complete_endpoint_and_no_key_local(self):
        with LocalProviderServer([{}, {}]) as server:
            for suffix in ("", "/v1/chat/completions"):
                provider = ChatCompletionsProvider(AgentConfig(base_url=server.url + suffix, model="synthetic"))
                provider.complete([], [])
            self.assertEqual([item["path"] for item in server.requests], ["/v1/chat/completions"] * 2)
            self.assertIsNone(server.requests[0]["authorization"])
            self.assertNotIn("tools", server.requests[0]["body"])

    def test_tool_call_normalization(self):
        calls = [{"id": "call_fake_1", "type": "function", "function": {"name": "synthetic_tool", "arguments": '{"code":"000001"}'}}]
        result = ChatCompletionsProvider._normalize(completion(None, tool_calls=calls))
        self.assertEqual(result["message"]["tool_calls"], calls)
        self.assertIsNone(result["message"]["content"])

    def test_invalid_protocol_and_truncation_are_not_silent(self):
        cases = [
            {}, {"choices": []}, completion(""), completion(None), completion([{"text": "bad"}]),
            completion(None, tool_calls="bad"), completion(None, tool_calls=[{"id": "fake", "type": "function", "function": {"name": "tool", "arguments": {}}}]),
            completion("text", tool_calls={}), completion("text", tool_calls=""),
            {"choices": [{"finish_reason": "length", "message": {"content": "truncated private text"}}]},
            {"choices": [{"finish_reason": "tool_calls", "message": {"content": "still missing tools"}}]},
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ProviderError) as raised:
                ChatCompletionsProvider._normalize(data)
            self.assertNotIn("truncated private text", str(raised.exception))

    def test_auth_errors_do_not_retry_or_leak_body_key(self):
        with LocalProviderServer([{"status": 401, "body": b'synthetic-key server-private-data'}]) as server:
            with self.assertRaises(ProviderError) as raised:
                self.make_provider(server, api_key="synthetic-key").complete([], [])
            self.assertEqual(len(server.requests), 1)
            self.assertIn("认证", str(raised.exception))
            self.assertNotIn("synthetic-key", str(raised.exception))
            self.assertNotIn("server-private-data", str(raised.exception))

    def test_retry_once_for_transient_error(self):
        with LocalProviderServer([{"status": 503}, {"json": completion("recovered")}]) as server:
            result = self.make_provider(server).complete([], [])
            self.assertEqual(result["message"]["content"], "recovered")
            self.assertEqual(len(server.requests), 2)
        with LocalProviderServer([{"status": 429}, {"status": 429}, {}]) as server:
            with self.assertRaises(ProviderError):
                self.make_provider(server).complete([], [])
            self.assertEqual(len(server.requests), 2)

    def test_redirect_never_forwards_authorization(self):
        with LocalProviderServer([{}]) as target:
            with LocalProviderServer([{"status": 307, "headers": {"Location": target.url + "/v1/chat/completions"}}]) as server:
                with self.assertRaises(ProviderError) as raised:
                    self.make_provider(server, api_key="synthetic-key").complete([], [])
                self.assertIn("跳转", str(raised.exception))
                self.assertEqual(len(server.requests), 1)
                self.assertEqual(target.requests, [])

    def test_response_size_and_invalid_json(self):
        for response in ({"length": 3 * 1024 * 1024, "body": b'{}'}, {"body": b'not-json-synthetic-secret'}):
            with self.subTest(response=response), LocalProviderServer([response]) as server:
                with self.assertRaises(ProviderError) as raised:
                    self.make_provider(server).complete([], [])
                self.assertNotIn("synthetic-secret", str(raised.exception))

    def test_cancel_before_request_and_during_blocking_server(self):
        cancelled = threading.Event()
        cancelled.set()
        with LocalProviderServer([]) as server:
            with self.assertRaises(AgentCancelled):
                self.make_provider(server).complete([], [], cancel=cancelled)
            self.assertEqual(server.requests, [])
        cancelled.clear()
        with LocalProviderServer([{"delay": 1.5}]) as server:
            timer = threading.Timer(0.1, cancelled.set)
            timer.start()
            started = time.monotonic()
            try:
                with self.assertRaises(AgentCancelled):
                    self.make_provider(server).complete([], [], cancel=cancelled)
            finally:
                timer.cancel()
            self.assertLess(time.monotonic() - started, 0.8)
            self.assertEqual(len(server.requests), 1)

    def test_total_timeout_retries_only_once(self):
        with LocalProviderServer([{"delay": 1.6}, {"delay": 1.6}]) as server:
            started = time.monotonic()
            with self.assertRaises(ProviderError) as raised:
                self.make_provider(server, timeout_seconds=1).complete([], [])
            self.assertIn("超时", str(raised.exception))
            self.assertEqual(len(server.requests), 2)
            self.assertLess(time.monotonic() - started, 3.5)

    def test_unexpected_transport_error_is_sanitized(self):
        provider = ChatCompletionsProvider(AgentConfig(base_url="http://localhost:1234/v1", model="synthetic", api_key="synthetic-secret"))
        with mock.patch.object(provider, "_request", side_effect=ValueError("synthetic-secret payload")):
            with self.assertRaises(ProviderError) as raised:
                provider.complete([], [])
        self.assertNotIn("synthetic-secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

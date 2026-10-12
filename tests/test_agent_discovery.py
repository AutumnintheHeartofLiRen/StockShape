"""Offline loopback tests for bounded local-model discovery and safe failures."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
import unittest
from unittest import mock

from research_agent.discovery import (
    AgentCancelled, DiscoveryError, MAX_MODELS, MAX_RESPONSE_BYTES,
    discover_local_models,
)


class FakeLocalModels:
    """An ephemeral test server, unrelated to any installed model service."""

    def __init__(self, responses=None):
        self.responses = list(responses or [{}])
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append({"path": self.path, "authorization": self.headers.get("Authorization")})
                response = outer.responses.pop(0) if outer.responses else {}
                if response.get("delay"):
                    time.sleep(response["delay"])
                body = response.get("body")
                if body is None:
                    body = json.dumps(response.get("json", {"data": [{"id": "synthetic-local-model"}]})).encode("utf-8")
                try:
                    self.send_response(response.get("status", 200))
                    if not response.get("omit_length"):
                        self.send_header("Content-Length", str(response.get("length", len(body))))
                    for name, value in response.get("headers", {}).items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)


class DiscoveryTests(unittest.TestCase):
    def test_root_prefix_and_complete_endpoint_paths(self):
        suffixes = ("", "/", "/v1/", "/custom/v2", "/v1/chat/completions", "/v1/models")
        expected = ("/v1/models", "/v1/models", "/v1/models", "/custom/v2/models", "/v1/models", "/v1/models")
        with FakeLocalModels([{} for _ in suffixes]) as server:
            for suffix in suffixes:
                self.assertEqual(discover_local_models(server.url + suffix), ["synthetic-local-model"])
            self.assertEqual([request["path"] for request in server.requests], list(expected))
            self.assertTrue(all(request["authorization"] is None for request in server.requests))

    def test_authentication_is_optional_and_sent_only_when_supplied(self):
        with FakeLocalModels([{}, {}]) as server:
            discover_local_models(server.url)
            discover_local_models(server.url, api_key="synthetic-local-secret")
            self.assertEqual([item["authorization"] for item in server.requests], [None, "Bearer synthetic-local-secret"])

    def test_proxy_environment_cannot_route_loopback_probe(self):
        with FakeLocalModels() as proxy, FakeLocalModels() as target:
            with mock.patch.dict(os.environ, {
                "HTTP_PROXY": proxy.url, "http_proxy": proxy.url,
                "HTTPS_PROXY": proxy.url, "https_proxy": proxy.url,
                "NO_PROXY": "", "no_proxy": "",
            }):
                self.assertEqual(discover_local_models(target.url, api_key="synthetic-only-local"), ["synthetic-local-model"])
            self.assertEqual(len(target.requests), 1)
            self.assertEqual(proxy.requests, [])

    def test_remote_and_unsafe_urls_fail_before_any_network(self):
        for url in (
            "https://example.invalid/v1", "http://localhost.example.invalid/v1",
            "https://user:synthetic-secret@localhost/v1", "http://127.0.0.1/v1?key=synthetic-secret",
            "file:///local/model", "http://127.0.0.1:bad", "http://127.0.0.1/v1#fragment",
            "http://192.168.1.2:1234/v1", "http://[::1", "", None,
        ):
            with self.subTest(url=url), mock.patch("research_agent.discovery.build_opener") as opener:
                with self.assertRaises(DiscoveryError) as raised:
                    discover_local_models(url)
                opener.assert_not_called()
                self.assertNotIn("synthetic-secret", str(raised.exception))

    def test_redirect_never_forwards_authentication(self):
        with FakeLocalModels() as destination:
            with FakeLocalModels([{"status": 302, "headers": {"Location": destination.url + "/v1/models?secret=synthetic-url-secret"}, "body": b"synthetic-body-secret"}]) as origin:
                with self.assertRaises(DiscoveryError) as raised:
                    discover_local_models(origin.url, api_key="synthetic-auth-secret")
                message = str(raised.exception)
                self.assertIn("跳转", message)
                for secret in ("synthetic-auth-secret", "synthetic-url-secret", "synthetic-body-secret"):
                    self.assertNotIn(secret, message)
                self.assertEqual(len(origin.requests), 1)
                self.assertEqual(destination.requests, [])

    def test_http_errors_are_user_readable_and_sanitized(self):
        responses = [{"status": code, "body": b"sensitive-response-key"} for code in (401, 403, 404, 500)]
        with FakeLocalModels(responses) as server:
            for code in (401, 403, 404, 500):
                with self.subTest(code=code), self.assertRaises(DiscoveryError) as raised:
                    discover_local_models(server.url, api_key="sensitive-request-key")
                self.assertNotIn("sensitive", str(raised.exception))
                self.assertNotIn(server.url, str(raised.exception))

    def test_models_are_deduplicated_bounded_and_invalid_ids_filtered(self):
        items = [{"id": " model-a "}, {"id": "model-a"}, {"id": "模型乙"}, {"id": "bad\nname"}, {"id": "bad\x7fname"}, {"id": "bad-trailing\n"}, {"id": "x" * 257}, {"id": 7}, None]
        items.extend({"id": f"model-{index}"} for index in range(MAX_MODELS + 10))
        with FakeLocalModels([{"json": {"data": items}}]) as server:
            models = discover_local_models(server.url)
            self.assertEqual(models[:2], ["model-a", "模型乙"])
            self.assertEqual(len(models), MAX_MODELS)
            self.assertEqual(len(models), len(set(models)))
            self.assertNotIn("bad\nname", models)
            self.assertNotIn("bad-trailing", models)

    def test_empty_model_list_is_valid(self):
        with FakeLocalModels([{"json": {"data": []}}]) as server:
            self.assertEqual(discover_local_models(server.url), [])

    def test_malformed_responses_are_sanitized(self):
        responses = [
            {"body": b"sensitive-invalid-json"}, {"body": b"\xff"},
            {"json": []}, {"json": {"models": []}}, {"json": {"data": "sensitive-value"}},
            {"json": {"data": [{"id": ""}, {"id": 9}]}},
        ]
        with FakeLocalModels(responses) as server:
            for _ in responses:
                with self.assertRaises(DiscoveryError) as raised:
                    discover_local_models(server.url)
                self.assertNotIn("sensitive", str(raised.exception))

    def test_response_size_guard_with_and_without_content_length(self):
        responses = [
            {"length": MAX_RESPONSE_BYTES + 1, "body": b"{}"},
            {"omit_length": True, "body": b"x" * (MAX_RESPONSE_BYTES + 1)},
        ]
        with FakeLocalModels(responses) as server:
            for _ in responses:
                with self.assertRaises(DiscoveryError) as raised:
                    discover_local_models(server.url)
                self.assertIn("过大", str(raised.exception))

    def test_cancellation_does_not_wait_for_socket_timeout(self):
        cancel = threading.Event()
        with FakeLocalModels([{"delay": 0.8}]) as server:
            timer = threading.Timer(0.06, cancel.set)
            timer.start()
            started = time.monotonic()
            try:
                with self.assertRaises(AgentCancelled):
                    discover_local_models(server.url, timeout_seconds=5, cancel=cancel)
                self.assertLess(time.monotonic() - started, 0.3)
                self.assertEqual(len(server.requests), 1)
            finally:
                timer.cancel()

    def test_cancelled_before_start_does_not_connect(self):
        cancel = threading.Event()
        cancel.set()
        with mock.patch("research_agent.discovery.build_opener") as opener:
            with self.assertRaises(AgentCancelled):
                discover_local_models("http://localhost:1234/v1", cancel=cancel)
            opener.assert_not_called()

    def test_timeout_has_overall_deadline(self):
        with FakeLocalModels([{"delay": 0.8}]) as server:
            started = time.monotonic()
            with self.assertRaises(DiscoveryError) as raised:
                discover_local_models(server.url, timeout_seconds=0.1)
            self.assertIn("超时", str(raised.exception))
            self.assertLess(time.monotonic() - started, 0.4)

    def test_invalid_arguments_fail_before_network_and_do_not_echo_key(self):
        for values in (
            {"api_key": "sensitive\nkey"}, {"api_key": 1}, {"api_key": "sensitive" * 2000},
            {"timeout_seconds": 0}, {"timeout_seconds": True}, {"timeout_seconds": float("nan")},
            {"timeout_seconds": float("inf")}, {"timeout_seconds": 31},
        ):
            with self.subTest(values=values), mock.patch("research_agent.discovery.build_opener") as opener:
                with self.assertRaises(DiscoveryError) as raised:
                    discover_local_models("http://localhost:1234/v1", **values)
                opener.assert_not_called()
                self.assertNotIn("sensitive", str(raised.exception))

    def test_unexpected_transport_error_is_sanitized(self):
        with mock.patch("research_agent.discovery.build_opener", side_effect=RuntimeError("sensitive-internal-key")):
            with self.assertRaises(DiscoveryError) as raised:
                discover_local_models("http://localhost:1234/v1")
            self.assertNotIn("sensitive", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

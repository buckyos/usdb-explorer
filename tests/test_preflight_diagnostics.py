#!/usr/bin/env python3
"""Exercise capability diagnostics through RPC envelopes and the CLI startup gate."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import socket
import ssl
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error

KIT = Path(__file__).resolve().parents[1] / "explorer"
sys.path.insert(0, str(KIT))
import public_checks as CHECK
import public_config as CONFIG
import usdb_public as PUBLIC
from common.public_services import PublicRpcFixture

SECRET = "do-not-print-upstream-secret"


class PreflightDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.identity = CONFIG.read_json(KIT / "networks/usdb-testnet-v0.json")
        self.rpc = PublicRpcFixture(self.identity)
        self.config = CONFIG.read_json(KIT / "config.example.json")
        self.config["rpc"] = {"mode": "external", "historical_block": 35, "transaction": self.rpc.transaction,
                              **{name: f"http://{name.replace('_', '-')}.internal/{SECRET}"
                                 for name in ("read_url", "trace_url", "broadcast_url")}}
        self.errors = {}

    def fetch(self, url, request):
        failure = self.errors.get((url, request["method"]))
        if failure is not None:
            return {"jsonrpc": "2.0", "id": 1, "error": failure}
        return {"jsonrpc": "2.0", "id": 1, "result": self.rpc(request["method"], request["params"])}

    def run_preflight(self):
        return CHECK.preflight(self.config, self.identity, rpc_factory=lambda url: CHECK.ReadRpc(url, fetcher=self.fetch))

    def assert_diagnostic(self, failure, category, *parts):
        self.assertIsInstance(failure, CHECK.RpcFailure)
        self.assertEqual(failure.category, category)
        message = str(failure)
        self.assertNotIn(SECRET, message)
        self.assertNotIn(".internal", message)
        for part in parts:
            self.assertIn(part, message)

    def test_missing_history_identifies_route_method_block_and_data_recovery(self):
        for method in (*sorted(CHECK.STATE_METHODS), "debug_traceTransaction"):
            route = "trace_url" if method in CHECK.TRACE_METHODS else "read_url"
            self.errors = {(self.config["rpc"][route], method):
                           {"code": -32000, "message": f"missing trie node {SECRET}", "data": SECRET}}
            with self.subTest(method=method), self.assertRaises(CHECK.RpcFailure) as caught:
                self.run_preflight()
            self.assert_diagnostic(caught.exception, "HISTORICAL_STATE_UNAVAILABLE", route, method,
                                   "set-query-mode --state-mode archive", "does not restore pruned history", "preserve existing data")
            if method in CHECK.STATE_METHODS:
                self.assertIn("block 35 (0x23)", str(caught.exception))

    def test_missing_trace_methods_are_distinct_from_missing_history(self):
        for method in sorted(CHECK.TRACE_METHODS):
            self.errors = {(self.config["rpc"]["trace_url"], method):
                           {"code": -32601, "message": f"the method does not exist/is not available {SECRET}"}}
            with self.subTest(method=method), self.assertRaises(CHECK.RpcFailure) as caught:
                self.run_preflight()
            self.assert_diagnostic(caught.exception, "TRACING_UNAVAILABLE", "trace_url", method,
                                   "set-query-mode --tracing on", "proxy", "Keep debug RPC private")

    def test_rpc_execution_failures_do_not_claim_missing_capabilities(self):
        cases = [
            ("debug_traceTransaction", -32000, "execution timeout", "TRACING_TIMEOUT"),
            ("eth_getBalance", -32000, "context deadline exceeded", "RPC_TIMEOUT"),
            ("debug_traceTransaction", -32000, "tracer not found", "TRACER_UNSUPPORTED"),
            ("eth_getBalance", -32601, "method unavailable", "RPC_METHOD_UNAVAILABLE"),
            ("eth_getBalance", -32000, "unauthorized", "RPC_AUTH"),
            ("eth_call", 3, "execution reverted: forbidden missing trie node", "RPC_EXECUTION_ERROR"),
            ("eth_getBalance", -32000, "unknown server failure", "RPC_ERROR"),
        ]
        for method, code, message, category in cases:
            route = "trace_url" if method in CHECK.TRACE_METHODS else "read_url"
            self.errors = {(self.config["rpc"][route], method): {"code": code, "message": message + SECRET}}
            with self.subTest(category=category), self.assertRaises(CHECK.RpcFailure) as caught:
                self.run_preflight()
            self.assert_diagnostic(caught.exception, category, route, method)
            self.assertNotIn("set-query-mode", str(caught.exception))

    def test_malformed_envelopes_never_become_capability_findings(self):
        valid_error = {"code": -32601, "message": SECRET}
        values = [None, [], {"jsonrpc": "2.0", "id": True, "error": valid_error},
                  {"jsonrpc": "2.0", "id": 1.0, "error": valid_error},
                  {"jsonrpc": "2.0", "id": 2, "error": valid_error},
                  {"jsonrpc": "2.0", "id": 1, "error": valid_error, "result": None},
                  {"jsonrpc": "2.0", "id": 1},
                  *[{"jsonrpc": "2.0", "id": 1, "error": error} for error in
                    (None, SECRET, {"code": True, "message": SECRET},
                     {"code": -32601, "message": {"token": SECRET}}, {"code": 2**40, "message": SECRET})]]
        for value in values:
            client = CHECK.ReadRpc(self.config["rpc"]["trace_url"], fetcher=lambda *_: value)
            with self.subTest(value=value), self.assertRaises(CHECK.RpcFailure) as caught:
                client("debug_traceTransaction", [self.rpc.transaction, {"tracer": "callTracer"}])
            self.assert_diagnostic(caught.exception, "RPC_INVALID_RESPONSE")
            self.assertNotIn("set-query-mode", str(caught.exception))

    def test_per_transaction_block_trace_errors_are_classified_but_evm_reverts_are_valid(self):
        for message, category in (("required historical state unavailable (reexec=128)", "HISTORICAL_STATE_UNAVAILABLE"),
                                  ("execution timeout", "TRACING_TIMEOUT")):
            self.rpc.failures = {"debug_traceBlockByNumber": [{"error": message + SECRET}]}
            with self.subTest(category=category), self.assertRaises(CHECK.RpcFailure) as caught:
                self.run_preflight()
            self.assert_diagnostic(caught.exception, category, "trace_url", "debug_traceBlockByNumber", "block 10 (0xa)")
        self.rpc.failures = {"debug_traceTransaction": {"type": "CALL", "error": "execution reverted"},
                             "debug_traceBlockByNumber": [{"result": {"type": "CALL", "error": "execution reverted"}}]}
        report = self.run_preflight()
        self.assertEqual(report["status"], "PREFLIGHT_PASSED")
        self.assertEqual(report["archive_replay_qualification"], "not_run")

    def test_invalid_results_do_not_claim_archive_or_tracing_is_disabled(self):
        for method, result in (("eth_getBalance", None), ("eth_getCode", SECRET),
                               ("eth_call", {}), ("debug_traceTransaction", None), ("debug_traceBlockByNumber", [])):
            self.rpc.failures = {method: result}
            with self.subTest(method=method), self.assertRaises(CHECK.RpcFailure) as caught:
                self.run_preflight()
            self.assert_diagnostic(caught.exception, "RPC_INVALID_RESPONSE", method)
            self.assertNotIn("set-query-mode", str(caught.exception))

    def test_invalid_http_body_is_sanitized(self):
        for body in (SECRET.encode(), json.dumps({"error": SECRET}).encode()):
            with self.subTest(body=body), mock.patch.object(CHECK.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value = io.BytesIO(body)
                with self.assertRaises(CHECK.RpcFailure) as caught:
                    CHECK.ReadRpc(self.config["rpc"]["read_url"])("eth_chainId", [])
                self.assert_diagnostic(caught.exception, "RPC_INVALID_RESPONSE", "eth_chainId")

    def test_http_and_connection_errors_include_safe_method_context(self):
        cases = [(urllib.error.HTTPError("http://" + SECRET, status, SECRET, {}, None), category)
                 for status, category in ((401, "RPC_AUTH"), (403, "RPC_AUTH"), (429, "RPC_RATE_LIMIT"),
                                          (404, "RPC_HTTP"), (503, "RPC_HTTP"), (504, "RPC_TIMEOUT"))]
        cases += [(urllib.error.URLError(reason), category) for reason, category in
                  ((socket.gaierror(-2, SECRET), "RPC_DNS"), (ConnectionRefusedError(SECRET), "RPC_CONNECTION_REFUSED"),
                   (TimeoutError(SECRET), "RPC_TIMEOUT"), (ssl.SSLError(SECRET), "RPC_TLS"), (OSError(SECRET), "RPC_CONNECTION"))]
        for reason, category in cases:
            with self.subTest(category=category), mock.patch.object(CHECK.urllib.request, "build_opener") as opener:
                opener.return_value.open.side_effect = reason
                with self.assertRaises(CHECK.RpcFailure) as caught:
                    CHECK.ReadRpc(self.config["rpc"]["read_url"])("eth_getBalance", ["0x" + "00" * 20, "0x23"])
                self.assert_diagnostic(caught.exception, category, "eth_getBalance", "block 35 (0x23)")
                self.assertNotIn("set-query-mode", str(caught.exception))

    def test_route_labels_cover_identity_checks_without_duplicate_prefixes(self):
        self.config["rpc"]["reference_url"] = f"http://reference.internal/{SECRET}"
        for route in ("read_url", "trace_url", "broadcast_url", "reference_url"):
            self.errors = {(self.config["rpc"][route], "eth_chainId"): {"code": -32000, "message": "unauthorized " + SECRET}}
            with self.subTest(route=route), self.assertRaises(CHECK.RpcFailure) as caught:
                self.run_preflight()
            self.assert_diagnostic(caught.exception, "RPC_AUTH", route)
            self.assertEqual(str(caught.exception).count(route), 1)

    def test_cli_keeps_full_mode_and_stops_before_starting_services(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path, state = root / "config.json", root / "state"
            config_path.write_text(json.dumps(self.config))
            with redirect_stdout(io.StringIO()):
                PUBLIC.prepare(config_path, state)
            before = (state / "deployment.json").read_bytes()

            def open_response(request, **_kwargs):
                if connection_failure:
                    raise urllib.error.URLError(ConnectionRefusedError(SECRET))
                return io.BytesIO(json.dumps(self.fetch(request.full_url, json.loads(request.data))).encode())

            for route, method, code, message, category in (
                    ("read_url", "eth_getBalance", -32000, "missing trie node", "HISTORICAL_STATE_UNAVAILABLE"),
                    ("trace_url", "debug_traceTransaction", -32601, "method unavailable", "TRACING_UNAVAILABLE"),
                    ("read_url", "eth_getBlockByNumber", None, "", "RPC_CONNECTION_REFUSED")):
                connection_failure = code is None
                self.errors = {} if connection_failure else {(self.config["rpc"][route], method): {"code": code, "message": message + SECRET}}
                for command in ("preflight", "up"):
                    output = io.StringIO()
                    with self.subTest(category=category, command=command), \
                            mock.patch.object(CHECK.urllib.request, "build_opener") as opener, \
                            mock.patch.object(PUBLIC, "docker", return_value=SimpleNamespace(stdout="1.55")), \
                            mock.patch.object(PUBLIC, "require_resources"), mock.patch.object(PUBLIC, "require_database_identity"), \
                            mock.patch.object(PUBLIC, "compose") as compose, redirect_stderr(output), redirect_stdout(io.StringIO()):
                        opener.return_value.open.side_effect = open_response
                        status = PUBLIC.main([command, "--state-dir", str(state)])
                        self.assertEqual(status, 1)
                        compose.assert_not_called()
                    self.assertIn(category, output.getvalue())
                    self.assertIn(route, output.getvalue())
                    self.assertNotIn(SECRET, output.getvalue())
                    self.assertEqual((state / "deployment.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

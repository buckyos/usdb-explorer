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
from common.public_services import PublicRpcFixture, RpcFixtureError, rpc_response

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


class BootstrapPreflightTests(unittest.TestCase):
    def setUp(self):
        self.identity = CONFIG.read_json(KIT / "networks/usdb-testnet-v0.json")
        self.config = CONFIG.read_json(KIT / "config.example.json")
        self.config["rpc"] = {"mode": "external", **{name: f"http://{name.replace('_', '-')}.internal"
                              for name in ("read_url", "trace_url", "broadcast_url")}}
        self.rpc = PublicRpcFixture(self.identity)
        self.rpc.height = 0

    def client(self, _url):
        return CHECK.ReadRpc("http://fixture.internal", fetcher=lambda _, request: rpc_response(self.rpc, request))

    def test_genesis_and_empty_blocks_keep_full_configuration_with_pending_samples(self):
        for height in (0, 4, 300):
            self.rpc.height = height
            with self.subTest(height=height):
                report = CHECK.preflight(self.config, self.identity, rpc_factory=self.client)
                self.assertEqual(report["status"], "PREFLIGHT_READY_NO_TRANSACTION_SAMPLE")
                self.assertEqual(report["trace_sample"], "pending_no_transaction_sample")
                self.assertEqual(report["trace_methods"], "available")
                self.assertIsNone(report["transaction"])
                self.assertEqual(report["historical_state_sample"], "genesis_only" if height == 0 else "passed")
                self.assertEqual(report["chain_state"], "genesis_only" if height == 0 else "no_recent_transaction_sample")
                self.assertEqual(report["archive_replay_qualification"], "not_run")
                self.assertTrue(report["warnings"])
                self.assertEqual({method for method, _ in self.rpc.calls} - CHECK.READ_METHODS, set())

    def test_first_mined_transaction_automatically_enables_real_acceptance(self):
        self.assertEqual(CHECK.preflight(self.config, self.identity, rpc_factory=self.client)["trace_sample"], "pending_no_transaction_sample")
        self.rpc.height = self.rpc.transaction_block = 1
        report = CHECK.check_explorer(self.config, self.identity, rpc_factory=self.client, api_fetch=self.rpc.api)
        self.assertEqual(report["status"], "CHECKED")
        self.assertEqual(report["trace_sample"], "passed")
        self.assertEqual(report["transaction"], self.rpc.transaction)
        self.assertNotIn("warnings", report)
        self.rpc.failures["debug_traceTransaction"] = RpcFixtureError("missing trie node")
        with self.assertRaisesRegex(CHECK.RpcFailure, "HISTORICAL_STATE_UNAVAILABLE"):
            CHECK.preflight(self.config, self.identity, rpc_factory=self.client)

    def test_unavailable_methods_and_other_errors_never_become_pending_samples(self):
        failures = [(method, RpcFixtureError("method unavailable", -32601), "TRACING_UNAVAILABLE")
                    for method in sorted(CHECK.TRACE_METHODS)]
        failures += [("debug_traceTransaction", RpcFixtureError("execution timeout"), "TRACING_TIMEOUT"),
                     ("debug_traceBlockByNumber", RpcFixtureError("missing trie node"), "HISTORICAL_STATE_UNAVAILABLE"),
                     ("debug_traceTransaction", RpcFixtureError("transaction not found", -32603), "RPC_ERROR"),
                     ("debug_traceTransaction", None, "RPC_INVALID_RESPONSE"),
                     ("debug_traceBlockByNumber", [], "RPC_INVALID_RESPONSE"),
                     ("eth_getBalance", RpcFixtureError("missing trie node"), "HISTORICAL_STATE_UNAVAILABLE")]
        for method, failure, category in failures:
            self.rpc.failures = {method: failure}
            with self.subTest(method=method, category=category), self.assertRaisesRegex(CHECK.RpcFailure, category):
                CHECK.preflight(self.config, self.identity, rpc_factory=self.client)
        self.rpc.height = 1
        self.rpc.failures = {"debug_traceBlockByNumber": RpcFixtureError("genesis is not traceable")}
        with self.assertRaisesRegex(CHECK.RpcFailure, "RPC_ERROR"):
            CHECK.preflight(self.config, self.identity, rpc_factory=self.client)

    def test_explicit_samples_are_not_silently_ignored(self):
        self.config["rpc"]["historical_block"] = 35
        with self.assertRaisesRegex(CHECK.RpcFailure, "SAMPLE_ABOVE_HEAD.*35.*head 0.*--auto-samples"):
            CHECK.preflight(self.config, self.identity, rpc_factory=self.client)
        self.config["rpc"].pop("historical_block")
        self.config["rpc"]["transaction"] = self.rpc.transaction
        with self.assertRaisesRegex(ValueError, "sample receipt is unavailable"):
            CHECK.preflight(self.config, self.identity, rpc_factory=self.client)

    def test_genesis_probe_detects_disagreeing_or_advancing_heads(self):
        alternate = PublicRpcFixture(self.identity)
        alternate.height = 1
        self.config["rpc"]["reference_url"] = "http://reference.internal"
        def factory(url):
            fixture = alternate if url == self.config["rpc"]["read_url"] else self.rpc
            return CHECK.ReadRpc(url, fetcher=lambda _, request: rpc_response(fixture, request))
        with self.assertRaisesRegex(ValueError, "advanced or endpoints disagree"):
            CHECK.preflight(self.config, self.identity, rpc_factory=factory)
        self.config["rpc"].pop("reference_url")
        def changing_fetch(_, request):
            response = rpc_response(self.rpc, request)
            if request["method"] == "debug_traceBlockByNumber":
                self.rpc.height = 1
            return response
        with self.assertRaisesRegex(ValueError, "advanced or endpoints disagree"):
            CHECK.preflight(self.config, self.identity, rpc_factory=lambda url: CHECK.ReadRpc(url, fetcher=changing_fetch))

    def test_empty_explorer_check_accepts_optional_genesis_but_rejects_stale_or_broken_api(self):
        report = CHECK.check_explorer(self.config, self.identity, rpc_factory=self.client, api_fetch=self.rpc.api)
        self.assertEqual(report["status"], "CHECKED_NO_TRANSACTION_SAMPLE")
        def genesis_api(url):
            if url.endswith("/blocks"):
                return {"items": [{"height": 0, "hash": self.identity["genesis_block_hash"]}]}
            return self.rpc.api(url)
        self.assertEqual(CHECK.check_explorer(self.config, self.identity, rpc_factory=self.client, api_fetch=genesis_api)["status"],
                         "CHECKED_NO_TRANSACTION_SAMPLE")
        for resource, value in (("/blocks", {"error": "unavailable"}),
                                ("/blocks", {"items": [{"height": 1, "hash": "0x" + "ab" * 32}]}),
                                ("/transactions?filter=validated", {"items": [{"hash": self.rpc.transaction}]})):
            def bad_api(url):
                return value if url.endswith(resource) else self.rpc.api(url)
            with self.subTest(resource=resource, value=value), self.assertRaises(ValueError):
                CHECK.check_explorer(self.config, self.identity, rpc_factory=self.client, api_fetch=bad_api)
        with self.assertRaisesRegex(ValueError, "API unavailable"):
            CHECK.check_explorer(self.config, self.identity, rpc_factory=self.client, api_fetch=mock.Mock(side_effect=ValueError("API unavailable")))

    def test_cli_starts_full_services_at_genesis_without_modifying_prepared_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, state = root / "config.json", root / "state"
            source.write_text(json.dumps(self.config))
            with redirect_stdout(io.StringIO()):
                PUBLIC.prepare(source, state)
            before = {path.name: path.read_bytes() for path in state.iterdir() if path.is_file()}
            document = CONFIG.read_json(state / "compose.json")
            self.assertTrue(document["services"]["backend"]["environment"]["ETHEREUM_JSONRPC_TRACE_URL"])
            def open_response(request, **_kwargs):
                return io.BytesIO(json.dumps(rpc_response(self.rpc, json.loads(request.data))).encode())
            for command in ("preflight", "up"):
                output = io.StringIO()
                with mock.patch.object(CHECK.urllib.request, "build_opener") as opener, \
                        mock.patch.object(PUBLIC, "docker", return_value=SimpleNamespace(stdout="1.55")), \
                        mock.patch.object(PUBLIC, "require_resources"), mock.patch.object(PUBLIC, "require_database_identity"), \
                        mock.patch.object(PUBLIC, "compose") as compose, redirect_stdout(output):
                    opener.return_value.open.side_effect = open_response
                    self.assertEqual(PUBLIC.main([command, "--state-dir", str(state)]), 0)
                    if command == "up":
                        compose.assert_any_call(state, ["up", "-d"])
                        self.assertIn("validation are pending", output.getvalue())
                    else:
                        compose.assert_not_called()
                        self.assertIn("Preflight PASSED WITH WARNINGS: Explorer may start.", output.getvalue())
                        self.assertIn("Transaction tracing: PENDING", output.getvalue())
                self.assertEqual({path.name: path.read_bytes() for path in state.iterdir() if path.is_file()}, before)


class CheckOutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.state = root / "state"
        source = root / "config.json"
        source.write_bytes((KIT / "config.example.json").read_bytes())
        with redirect_stdout(io.StringIO()):
            PUBLIC.prepare(source, self.state)
        self.config = CONFIG.read_json(self.state / "config.json")
        self.identity = CONFIG.read_json(self.state / "identity.json")
        self.rpc = PublicRpcFixture(self.identity)
        self.rpc.height = 0
        self.rpc_urls = []
        self.api_urls = []

    def client(self, url):
        self.rpc_urls.append(url)
        return CHECK.ReadRpc(url, fetcher=lambda _, request: rpc_response(self.rpc, request))

    def api(self, url):
        self.api_urls.append(url)
        return self.rpc.api(url)

    def test_cli_human_and_json_success_report_pending_without_claiming_full_acceptance(self):
        for command in ("preflight", "check"):
            for mined in (False, True):
                self.rpc.height = 10 if mined else 0
                report = (CHECK.preflight(self.config, self.identity, rpc_factory=self.client) if command == "preflight" else
                          CHECK.check_explorer(self.config, self.identity, rpc_factory=self.client, api_fetch=self.api))
                for machine in (False, True):
                    output, errors = io.StringIO(), io.StringIO()
                    with self.subTest(command=command, mined=mined, machine=machine), \
                            mock.patch.object(PUBLIC, "preflight", return_value=report), \
                            mock.patch.object(PUBLIC, "check_explorer", return_value=report), \
                            redirect_stdout(output), redirect_stderr(errors):
                        status = PUBLIC.main([command, "--state-dir", str(self.state), *(["--json"] if machine else [])])
                    self.assertEqual(status, 0)
                    self.assertEqual(errors.getvalue(), "")
                    if machine:
                        self.assertEqual(json.loads(output.getvalue()), report)
                    else:
                        self.assertIn(command.capitalize() + " PASSED" + (":" if mined else " WITH WARNINGS:"), output.getvalue())
                        self.assertIn("Checkpoint: block", output.getvalue())
                        self.assertIn("not certified", output.getvalue())
                        self.assertNotIn('"schema_version"', output.getvalue())

    def test_cli_failures_have_clear_result_exit_code_and_optional_json(self):
        for command in ("preflight", "check"):
            for machine in (False, True):
                output, errors = io.StringIO(), io.StringIO()
                failure = CHECK.RpcFailure("RPC_TIMEOUT", "ingress.explorer_url /rpc: timed out")
                with mock.patch.object(PUBLIC, "preflight", side_effect=failure), \
                        mock.patch.object(PUBLIC, "check_explorer", side_effect=failure), \
                        redirect_stdout(output), redirect_stderr(errors):
                    status = PUBLIC.main([command, "--state-dir", str(self.state), *(["--json"] if machine else [])])
                self.assertEqual(status, 1)
                if machine:
                    report = json.loads(output.getvalue())
                    self.assertEqual(report["status"], command.upper() + "_FAILED")
                    self.assertEqual(report["error"]["category"], "RPC_TIMEOUT")
                    self.assertEqual(errors.getvalue(), "")
                else:
                    self.assertIn(command.capitalize() + " FAILED: [RPC_TIMEOUT]", errors.getvalue())
                    self.assertEqual(output.getvalue(), "")

    def test_check_identifies_public_rpc_and_api_failures_without_echoing_urls(self):
        self.config["ingress"]["explorer_url"] = "http://" + SECRET + ".internal"
        def failing_rpc(url):
            if url.endswith("/rpc"):
                return mock.Mock(side_effect=CHECK.RpcFailure("RPC_TIMEOUT", "connection timed out"))
            return self.client(url)
        with self.assertRaises(CHECK.RpcFailure) as caught:
            CHECK.check_explorer(self.config, self.identity, rpc_factory=failing_rpc, api_fetch=self.api)
        self.assertIn("ingress.explorer_url /rpc", str(caught.exception))
        self.assertIn("Upstream preflight passed", str(caught.exception))
        self.assertIn("No mining is needed", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))
        with self.assertRaises(CHECK.RpcFailure) as caught:
            CHECK.check_explorer(self.config, self.identity, rpc_factory=self.client,
                                 api_fetch=mock.Mock(side_effect=CHECK.RpcFailure("RPC_HTTP", "HTTP endpoint returned status 502")))
        self.assertIn("ingress.explorer_url /api/v2", str(caught.exception))
        self.assertIn("backend readiness", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))

    def test_override_checks_all_ingress_routes_without_changing_upstream_or_saved_config(self):
        before = {p.name: p.read_bytes() for p in self.state.iterdir() if p.is_file()}
        original = json.dumps(self.config, sort_keys=True)
        override = "http://127.0.0.1:38080"
        report = CHECK.check_explorer(self.config, self.identity, rpc_factory=self.client, api_fetch=self.api, explorer_url=override + "/")
        self.assertEqual(report["ingress_check"], "override_origin")
        self.assertIn(override + "/rpc", self.rpc_urls)
        self.assertIn(self.config["rpc"]["read_url"], self.rpc_urls)
        self.assertNotIn(self.config["ingress"]["explorer_url"] + "/rpc", self.rpc_urls)
        self.assertTrue(all(url.startswith(override + "/api/v2/") for url in self.api_urls))
        self.assertEqual(json.dumps(self.config, sort_keys=True), original)
        output = io.StringIO()
        with mock.patch.object(PUBLIC, "check_explorer", return_value=report) as check, redirect_stdout(output):
            self.assertEqual(PUBLIC.main(["check", "--url", override, "--state-dir", str(self.state)]), 0)
            self.assertEqual(check.call_args.kwargs["explorer_url"], override)
        self.assertIn("configured visitor URL was NOT checked", output.getvalue())
        self.assertEqual({p.name: p.read_bytes() for p in self.state.iterdir() if p.is_file()}, before)
        with self.assertRaises(ValueError):
            CHECK.check_explorer(self.config, self.identity, explorer_url="http://user:secret@localhost", rpc_factory=mock.Mock())


if __name__ == "__main__":
    unittest.main()

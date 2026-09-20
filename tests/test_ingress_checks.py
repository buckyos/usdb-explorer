#!/usr/bin/env python3
"""Verify independent ingress results, strict outcomes and real Host/SNI-preserving local probes."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "explorer"))
import ingress_checks as INGRESS
import public_checks as CHECK
import public_config as CONFIG
import usdb_public as PUBLIC
import package_release as RELEASE
from common.public_services import PublicRpcFixture, rpc_response, rpc_server


class IngressComparisonTests(unittest.TestCase):
    def setUp(self):
        self.identity = CONFIG.read_json(PUBLIC.KIT / "networks/usdb-testnet-v0.json")
        self.rpc = PublicRpcFixture(self.identity)
        self.config = CONFIG.read_json(PUBLIC.KIT / "config.example.json")
        self.config["rpc"] = {"mode": "external", "historical_block": 10, "transaction": self.rpc.transaction,
                              **{key: "http://private.internal/do-not-print" for key in ("read_url", "trace_url", "broadcast_url")}}
        self.config["ingress"].update(exposure="public", bind_address="0.0.0.0", explorer_url="http://192.0.2.10:38080")
        self.discovery = mock.Mock(return_value=(["192.168.1.119"], None))
        self.upstream = mock.Mock(side_effect=lambda config, identity: CHECK.preflight(config, identity, rpc_factory=self.client))
        self.errors = {}
        self.seen = []
        self.checkpoints = []

    def client(self, url):
        return CHECK.ReadRpc(url, fetcher=lambda _, request: rpc_response(self.rpc, request))

    def probe(self, target, config, identity, upstream):
        self.seen.append(target["name"])
        self.checkpoints.append(upstream["checkpoint"])
        if target["name"] in self.errors:
            raise self.errors[target["name"]]
        return CHECK.check_explorer(config, identity, rpc_factory=self.client, api_fetch=self.rpc.api,
                                    explorer_url=target["origin"], preflight_report=upstream)

    def check(self):
        return INGRESS.check_ingresses(self.config, self.identity, discover=self.discovery,
                                       upstream_check=self.upstream, probe=self.probe)

    def test_nat_failure_keeps_other_results_and_one_fixed_upstream_checkpoint(self):
        self.errors["configured"] = CHECK.RpcFailure("RPC_TIMEOUT", "connection timed out")
        before = deepcopy(self.config)
        report = self.check()
        self.assertEqual(report["status"], "CHECK_FAILED")
        self.assertEqual(report["upstream"]["status"], "PASSED")
        self.assertEqual([r["status"] for r in report["ingress_results"]], ["PASSED", "PASSED", "FAILED"])
        self.assertEqual(report["error"]["category"], "RPC_TIMEOUT")
        self.assertIn("possible cause, not confirmed", report["diagnosis"])
        self.assertIn("actual visitor URL", report["diagnosis"])
        self.assertEqual(set(self.seen), {"loopback", "lan", "configured"})
        self.assertEqual(self.checkpoints, [report["checkpoint"]] * 3)
        self.upstream.assert_called_once()
        self.assertEqual(self.config, before)
        self.assertNotIn("do-not-print", json.dumps(report))

    def test_local_failure_does_not_prevent_lan_and_configured_checks(self):
        self.errors["loopback"] = CHECK.RpcFailure("RPC_CONNECTION_REFUSED", "connection refused")
        report = self.check()
        self.assertEqual([r["status"] for r in report["ingress_results"]], ["FAILED", "PASSED", "PASSED"])
        self.assertEqual(report["status"], "CHECK_FAILED")
        self.assertIn("configured visitor path passed", report["diagnosis"])

    def test_api_validation_and_tls_failures_are_not_reported_as_nat_failures(self):
        for error in (ValueError("explorer has not indexed the observed upstream checkpoint"),
                      CHECK.RpcFailure("RPC_TLS", "certificate verification failed")):
            with self.subTest(error=error):
                self.errors["configured"] = error
                report = self.check()
                self.assertNotIn("NAT", report["diagnosis"])
                self.assertIn(str(error), report["ingress_results"][-1]["error"]["message"])

    def test_upstream_failure_does_not_downgrade_complete_mode_or_leak_urls(self):
        self.upstream.side_effect = CHECK.RpcFailure("TRACING_UNAVAILABLE", "trace_url: enable private tracing")
        report = self.check()
        self.assertEqual(report["status"], "CHECK_FAILED")
        self.assertEqual(report["upstream"]["error"]["category"], "TRACING_UNAVAILABLE")
        self.assertTrue(all(r["status"] == "SKIPPED" for r in report["ingress_results"]))
        self.assertEqual(self.seen, [])
        self.assertNotIn("do-not-print", json.dumps(report))
        output = io.StringIO()
        with redirect_stdout(output):
            PUBLIC.print_check_report("check", report)
        self.assertIn("Check FAILED", output.getvalue())
        self.assertIn("Upstream preflight: FAILED", output.getvalue())
        self.assertIn("SKIPPED", output.getvalue())
        self.assertNotIn("Checkpoint:", output.getvalue())

    def test_genesis_pending_status_remains_distinct_from_failure(self):
        self.rpc.height = 0
        for key in ("transaction", "historical_block"):
            self.config["rpc"].pop(key)
        report = self.check()
        self.assertEqual(report["status"], "CHECKED_NO_TRANSACTION_SAMPLE")
        self.assertEqual(report["trace_sample"], "pending_no_transaction_sample")
        self.assertTrue(all(r["status"] == "PASSED" for r in report["ingress_results"]))

    def test_loopback_binding_and_external_proxy_skip_inapplicable_paths(self):
        self.config["ingress"]["bind_address"] = "127.0.0.1"
        report = self.check()
        self.assertEqual([r["status"] for r in report["ingress_results"]], ["PASSED", "SKIPPED", "PASSED"])
        self.discovery.assert_not_called()
        self.config["ingress"]["mode"] = "external"
        self.seen.clear()
        report = self.check()
        self.assertEqual([r["status"] for r in report["ingress_results"]], ["SKIPPED", "SKIPPED", "PASSED"])
        self.assertEqual(self.seen, ["configured"])
        self.assertIn("internal backend ports", report["ingress_results"][0]["reason"])

    def test_specific_bind_and_missing_discovery_do_not_invent_a_lan_address(self):
        self.config["ingress"]["bind_address"] = "192.168.10.3"
        report = self.check()
        self.assertEqual(report["ingress_results"][0]["status"], "SKIPPED")
        self.assertEqual(report["ingress_results"][1]["connect_to"], ["192.168.10.3", 28080])
        self.discovery.assert_not_called()
        self.config["ingress"]["bind_address"] = "0.0.0.0"
        self.discovery.return_value = ([], "iproute2 unavailable")
        report = self.check()
        self.assertEqual(report["ingress_results"][1]["status"], "SKIPPED")
        self.assertIn("iproute2 unavailable", report["warnings"])

    def test_https_local_targets_use_tls_port_and_original_visitor_origin(self):
        self.config["ingress"].update(explorer_url="https://explorer.example.test", http_port=28080, https_port=28443)
        targets, _ = INGRESS.ingress_targets(self.config, discover=self.discovery)
        self.assertEqual(targets[0]["url"], "https://127.0.0.1:28443")
        self.assertEqual(targets[0]["origin"], "https://explorer.example.test")
        self.assertEqual(targets[1]["connect_to"], ["192.168.1.119", 28443])
        self.assertNotIn("connect_to", targets[-1])

    def test_dual_stack_checks_ipv6_independently_and_keeps_tls_origin(self):
        self.config["ingress"].update(bind_address_ipv6="::", explorer_url="https://explorer.example.test")
        discovery6 = mock.Mock(return_value=(["2001:db8::123"], None))
        self.errors["loopback-ipv6"] = CHECK.RpcFailure("RPC_CONNECTION_REFUSED", "connection refused")
        report = INGRESS.check_ingresses(self.config, self.identity, discover=self.discovery,
                                        discover_ipv6=discovery6, upstream_check=self.upstream, probe=self.probe)
        rows = {row["name"]: row for row in report["ingress_results"]}
        self.assertEqual(report["status"], "CHECK_FAILED")
        self.assertEqual(rows["loopback"]["status"], "PASSED")
        self.assertEqual(rows["loopback-ipv6"]["status"], "FAILED")
        self.assertEqual(rows["host-ipv6"]["url"], "https://[2001:db8::123]:28443")
        self.assertEqual(rows["host-ipv6"]["connect_to"], ["2001:db8::123", 28443])
        self.assertEqual(rows["host-ipv6"]["origin"], "https://explorer.example.test")
        self.assertEqual(rows["configured"]["status"], "PASSED")

    def test_ipv6_loopback_specific_and_missing_host_addresses_are_distinguished(self):
        discovery6 = mock.Mock(return_value=([], "No usable IPv6 address found."))
        for binding in ("::1", "2001:db8::123", "::"):
            self.config["ingress"]["bind_address_ipv6"] = binding
            targets, warnings = INGRESS.ingress_targets(self.config, discover=self.discovery, discover_ipv6=discovery6)
            rows = {row["name"]: row for row in targets}
            if binding == "::1":
                self.assertEqual(rows["loopback-ipv6"]["url"], "http://[::1]:28080")
                self.assertEqual(rows["host-ipv6"]["status"], "SKIPPED")
                discovery6.assert_not_called()
            elif binding == "::":
                self.assertEqual(rows["host-ipv6"]["status"], "SKIPPED")
                self.assertEqual(warnings, ["No usable IPv6 address found."])
            else:
                self.assertEqual(rows["loopback-ipv6"]["status"], "SKIPPED")
                self.assertEqual(rows["host-ipv6"]["connect_to"], [binding, 28080])
                discovery6.assert_not_called()

    def test_reachable_endpoints_and_fresh_block_index_lag_are_reported_separately(self):
        upstream = self.upstream(self.config, self.identity)
        target = {"name": "configured", "origin": self.config["ingress"]["explorer_url"]}
        def transport(url, payload=None, **kwargs):
            if payload is not None:
                return rpc_response(self.rpc, payload)
            if "/api/v2/blocks/0x" in url:
                raise CHECK.RpcFailure("RPC_HTTP", "HTTP endpoint returned status 404; check route")
            return {"items": []}
        def canonical(config, identity, **kwargs):
            return CHECK.check_explorer(config, identity, rpc_factory=self.client, **kwargs)
        with mock.patch.object(INGRESS, "fetch", side_effect=transport), mock.patch.object(INGRESS, "check_explorer", side_effect=canonical):
            result = INGRESS.probe_ingress(target, self.config, self.identity, upstream)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["checks"], {"rpc": "PASSED", "api": "PASSED", "canonical": "FAILED"})
        self.assertEqual(result["error"]["category"], "EXPLORER_SAMPLE_UNAVAILABLE")
        self.assertIn("indexer may not have reached", result["error"]["message"])

    def test_api_reachability_is_checked_even_when_rpc_connection_fails(self):
        upstream = self.upstream(self.config, self.identity)
        def transport(url, payload=None, **kwargs):
            if payload is not None:
                raise CHECK.RpcFailure("RPC_TIMEOUT", "connection timed out")
            return {"items": []}
        with mock.patch.object(INGRESS, "fetch", side_effect=transport) as fetch, mock.patch.object(INGRESS, "check_explorer") as canonical:
            result = INGRESS.probe_ingress({"name": "configured", "origin": "http://explorer.invalid"}, self.config, self.identity, upstream)
        self.assertEqual(result["checks"], {"rpc": "FAILED", "api": "PASSED", "canonical": "NOT_RUN"})
        self.assertEqual(result["error"]["category"], "RPC_TIMEOUT")
        self.assertEqual(fetch.call_count, 2)
        canonical.assert_not_called()

    def test_host_discovery_uses_default_route_interface_including_host_bridges(self):
        routes = [{"dev": "vmbr0"}]
        def interface(name, address, flags=("UP",)):
            return {"ifname": name, "flags": list(flags), "addr_info": [{"family": "inet", "scope": "global", "local": address}]}
        interfaces = [interface("vmbr0", "192.168.1.119"), interface("docker0", "172.17.0.1"),
                      interface("br-random", "172.18.0.1"), interface("eth1", "10.1.0.2")]
        with mock.patch.object(INGRESS.subprocess, "run", side_effect=[mock.Mock(stdout=json.dumps(routes)), mock.Mock(stdout=json.dumps(interfaces))]):
            self.assertEqual(INGRESS.host_addresses(), (["192.168.1.119"], None))
        with mock.patch.object(INGRESS.subprocess, "run", side_effect=FileNotFoundError):
            addresses, reason = INGRESS.host_addresses()
        self.assertEqual(addresses, [])
        self.assertIn("iproute2", reason)

    def test_ipv6_discovery_excludes_tentative_link_local_and_docker_addresses(self):
        def address(value, **flags):
            return {"family": "inet6", "scope": "global", "local": value, **flags}
        interfaces = [{"ifname": "vmbr0", "flags": ["UP"], "addr_info": [
            address("2001:db8::2"), address("2001:db8::3", tentative=True),
            address("2001:db8::4", flags=["dadfailed"]), address("fe80::1") ]},
            {"ifname": "docker0", "flags": ["UP"], "addr_info": [address("fd00::1")]}]
        with mock.patch.object(INGRESS.subprocess, "run", side_effect=[
                mock.Mock(stdout=json.dumps([{"dev": "vmbr0"}])), mock.Mock(stdout=json.dumps(interfaces))]) as run:
            self.assertEqual(INGRESS.host_addresses(version=6), (["2001:db8::2"], None))
            self.assertTrue(all("-6" in call.args[0] for call in run.call_args_list))

    def test_cli_renders_all_results_and_returns_failure_in_human_and_json_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, state = root / "config.json", root / "state"
            PUBLIC.write_json(source, self.config)
            with redirect_stdout(io.StringIO()):
                PUBLIC.prepare(source, state)
            before = PUBLIC.file_hashes(state)
            self.errors["configured"] = CHECK.RpcFailure("RPC_TIMEOUT", "connection timed out")
            report = self.check()
            for machine in (False, True):
                output, errors = io.StringIO(), io.StringIO()
                with mock.patch.object(PUBLIC, "check_ingresses", return_value=report), redirect_stdout(output), redirect_stderr(errors):
                    result = PUBLIC.main(["check", "--state-dir", str(state), *(["--json"] if machine else [])])
                self.assertEqual(result, 1)
                self.assertEqual(errors.getvalue(), "")
                if machine:
                    self.assertEqual(json.loads(output.getvalue()), report)
                else:
                    for value in ("Check FAILED", "Upstream preflight: PASSED", "loopback", "lan", "configured", "RPC_TIMEOUT"):
                        self.assertIn(value, output.getvalue())
            self.assertEqual(PUBLIC.file_hashes(state), before)


class DirectIngressTransportTests(unittest.TestCase):
    def setUp(self):
        self.identity = CONFIG.read_json(PUBLIC.KIT / "networks/usdb-testnet-v0.json")
        self.rpc = PublicRpcFixture(self.identity)

    def test_http_uses_local_socket_original_host_and_no_environment_proxy(self):
        requests = []
        with rpc_server(self.rpc, request_observer=lambda path, headers: requests.append((path, headers["Host"]))) as local:
            port = int(local.rsplit(":", 1)[1])
            opener = INGRESS.direct_opener(("127.0.0.1", port))
            with mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1", "no_proxy": ""}):
                value = CHECK.fetch("http://explorer.invalid:38080/rpc", {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}, opener=opener, timeout=2)
        self.assertEqual(value["result"], hex(self.identity["chain_id"]))
        self.assertEqual(requests, [("/rpc", "explorer.invalid:38080")])

    def test_https_keeps_sni_and_verifies_hostname_and_ca_on_the_local_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory) / "cert.pem", Path(directory) / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-subj", "/CN=explorer.example.test", "-addext", "subjectAltName=DNS:explorer.example.test",
                            "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(cert, key)
            names, hosts = [], []
            server_context.set_servername_callback(lambda sock, name, context: names.append(name))
            trusted = ssl.create_default_context(cafile=str(cert))
            with rpc_server(self.rpc, tls_context=server_context, request_observer=lambda path, headers: hosts.append(headers["Host"])) as local:
                destination = ("127.0.0.1", int(local.rsplit(":", 1)[1]))
                request = {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}
                value = CHECK.fetch("https://explorer.example.test/rpc", request, opener=INGRESS.direct_opener(destination, context=trusted), timeout=2)
                self.assertEqual(value["result"], hex(self.identity["chain_id"]))
                for url, context in (("https://wrong.example.test/rpc", trusted), ("https://explorer.example.test/rpc", None)):
                    with self.assertRaises(CHECK.RpcFailure) as caught:
                        CHECK.fetch(url, request, opener=INGRESS.direct_opener(destination, context=context), timeout=2)
                    self.assertEqual(caught.exception.category, "RPC_TLS")
            self.assertIn("explorer.example.test", names)
            self.assertEqual(hosts, ["explorer.example.test"])

    def test_extracted_release_check_uses_real_http_rpc_and_api_without_node_installation(self):
        with tempfile.TemporaryDirectory() as directory, rpc_server(self.rpc, api=True) as url:
            root = Path(directory)
            archive = RELEASE.package(ROOT, root / "dist", "0.2.8", "ghcr.io/buckyos/usdb-explorer-gateway@sha256:" + "ab" * 32,
                                      frontend_image="ghcr.io/buckyos/usdb-explorer-frontend@sha256:" + "cd" * 32)
            with tarfile.open(archive) as package:
                package.extractall(root / "kit")
            kit = root / "kit/usdb-explorer-v0.2.8"
            config = CONFIG.read_json(kit / "config.example.json")
            config["rpc"] = {"mode": "local-node", "read_url": url, "transaction": self.rpc.transaction}
            config["ingress"].update(explorer_url=url, http_port=int(url.rsplit(":", 1)[1]))
            source, state = root / "config.json", root / "state"
            PUBLIC.write_json(source, config)
            with redirect_stdout(io.StringIO()):
                PUBLIC.prepare(source, state, kit=kit)
            before = PUBLIC.file_hashes(state)
            result = subprocess.run([str(kit / "usdb-explorer"), "check", "--state-dir", str(state), "--json"],
                                    check=False, text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "CHECKED")
            self.assertEqual([r["status"] for r in report["ingress_results"]], ["PASSED", "SKIPPED", "PASSED"])
            self.assertEqual(before, PUBLIC.file_hashes(state))


if __name__ == "__main__":
    unittest.main()

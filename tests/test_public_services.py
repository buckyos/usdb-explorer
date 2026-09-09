#!/usr/bin/env python3
"""Accept standalone lifecycle, optional ingress, upstream gates and independent releases."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "explorer"
sys.path.insert(0, str(KIT))
import public_config as CONFIG
import public_checks as CHECK
import usdb_public as PUBLIC
import package_release as RELEASE
from common.public_services import PublicRpcFixture, rpc_server, fake_docker


class PublicServicesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = CONFIG.read_json(KIT / "config.example.json")
        self.identity = CONFIG.read_json(KIT / "networks/usdb-testnet-v0.json")
        self.rpc = PublicRpcFixture(self.identity)
        self.config["rpc"]["transaction"] = self.rpc.transaction
        self.config["rpc"]["historical_block"] = 10
        self.input = self.root / "operator.json"
        self.state = self.root / "state"

    def save(self):
        self.input.write_text(json.dumps(self.config))

    def prepare(self):
        self.save()
        PUBLIC.prepare(self.input, self.state)
        return CONFIG.read_json(self.state / "compose.json")

    def test_external_mode_has_no_node_or_nginx_dependency(self):
        doc = self.prepare()
        self.assertEqual(set(doc["services"]), {"postgres", "redis", "frontend", "backend", "gateway"})
        self.assertTrue(doc["networks"]["database"]["internal"])
        self.assertFalse(any(n.get("external") for n in doc["networks"].values()))
        self.assertEqual(doc["services"]["frontend"]["ports"], ["127.0.0.1:28080:3000"])
        self.assertEqual(doc["services"]["gateway"]["ports"], ["127.0.0.1:28081:8080"])
        for name in ("backend", "postgres", "redis"):
            self.assertNotIn("ports", doc["services"][name])
        for forbidden in ("node.env", "usdb-indexer", "archive-data"):
            self.assertNotIn(forbidden, json.dumps(doc))

    def test_public_urls_are_independent_of_local_bindings_and_rpc_destinations(self):
        self.config["ingress"]["explorer_url"] = "https://explorer.example.com"
        self.config["rpc"]["broadcast_url"] = "https://writer.internal/rpc"
        doc = self.prepare()
        frontend = doc["services"]["frontend"]["environment"]
        self.assertEqual(frontend["NEXT_PUBLIC_API_HOST"], "explorer.example.com")
        self.assertEqual(frontend["NEXT_PUBLIC_API_PROTOCOL"], "https")
        self.assertEqual(frontend["NEXT_PUBLIC_API_PORT"], "")
        self.assertEqual(doc["services"]["gateway"]["environment"]["BROADCAST_UPSTREAM"], "https://writer.internal/rpc")
        self.assertEqual(CONFIG.read_json(self.state / "network.json")["rpcUrls"], ["https://explorer.example.com/rpc"])
        routes = (self.state / "nginx.locations.conf").read_text()
        self.assertIn("127.0.0.1:28081", routes)
        self.assertIn("location /api/ { proxy_pass $usdb_gateway$request_uri; }", routes)
        self.assertNotIn("backend:4000", routes)

    def test_bundled_tls_mount_and_certificate_rotation(self):
        certificates = self.root / "certificates"
        certificates.mkdir()
        for name in ("fullchain.pem", "privkey.pem"):
            (certificates / name).write_text("fixture certificate")
        self.config["ingress"] = {"mode": "bundled", "explorer_url": "https://explorer.example.com",
                                  "tls": {"certificate_dir": str(certificates)}}
        doc = self.prepare()
        self.assertEqual(doc["services"]["proxy"]["ports"], ["127.0.0.1:28080:8080", "127.0.0.1:28443:8443"])
        self.assertIn(str(certificates) + ":/tls:ro", doc["services"]["proxy"]["volumes"])
        self.assertNotIn("ports", doc["services"]["gateway"])
        self.assertIn("resolver 127.0.0.11", (self.state / "nginx.conf").read_text())
        self.assertIn("return 308 https://explorer.example.com$request_uri", (self.state / "nginx.conf").read_text())
        (certificates / "fullchain.pem").write_text("renewed fixture")
        PUBLIC.verify(self.state)

    def test_config_rejects_typos_injection_unsafe_exposure_and_port_collision(self):
        base = deepcopy(self.config)
        changes = [("mode", "unknown"), ("bind_address", "0.0.0.0"), ("exposure", "public"),
                   ("gateway_port", 28080), ("explorer_url", "https://explorer.example.com/subpath"),
                   ("explorer_url", "https://example.com;return"), ("typo", "ignored")]
        for key, value in changes:
            with self.subTest(key=key):
                self.config = deepcopy(base)
                self.config["ingress"][key] = value
                self.save()
                with self.assertRaises(ValueError):
                    CONFIG.load_config(self.input, KIT)
        self.input.write_text('{"schema_version":1,"schema_version":2}')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            CONFIG.read_json(self.input)

    def test_atomic_prepare_preserves_credentials_and_backup(self):
        self.prepare()
        credentials = (self.state / "credentials.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists"):
            PUBLIC.prepare(self.input, self.state)
        with mock.patch.object(PUBLIC, "require_stopped") as stopped:
            PUBLIC.prepare(self.input, self.state, replace=True)
            stopped.assert_called_once_with(self.config["deployment_id"])
        self.assertEqual((self.state / "credentials.json").read_bytes(), credentials)
        self.assertEqual(len(list(self.root.glob("state.backup-*"))), 1)
        self.assertEqual((self.state / "credentials.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.state / "network.json").stat().st_mode & 0o777, 0o644)
        (self.state / "compose.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "file changed"):
            PUBLIC.verify(self.state)

    def test_prepare_replace_refuses_running_stack_and_retains_files(self):
        self.prepare()
        original = (self.state / "compose.json").read_bytes()
        with mock.patch.object(PUBLIC, "project_containers", return_value=[{}]):
            with self.assertRaisesRegex(ValueError, "down before"):
                PUBLIC.prepare(self.input, self.state, replace=True)
        self.assertEqual((self.state / "compose.json").read_bytes(), original)

    def test_missing_credentials_cannot_silently_claim_existing_database(self):
        document = self.prepare()
        with mock.patch.object(PUBLIC, "docker", side_effect=[SimpleNamespace(stdout="volume\n"), SimpleNamespace(stdout='[{"Labels":{}}]')]):
            with self.assertRaisesRegex(ValueError, "identity or credentials differ"):
                PUBLIC.require_database_identity(self.state, document)

    def test_resource_check_uses_only_own_budget_and_explicit_colocation(self):
        doc = self.prepare()
        config = CONFIG.read_json(self.state / "config.json")
        with mock.patch.object(PUBLIC, "docker", side_effect=[SimpleNamespace(stdout=json.dumps({"MemTotal": 8 * CONFIG.GIB})), SimpleNamespace(stdout="")]):
            PUBLIC.require_resources(config, doc)
        other = {"Config": {"Labels": {"com.docker.compose.project": "unrelated"}}, "HostConfig": {"Memory": 2 * CONFIG.GIB}}
        outputs = [SimpleNamespace(stdout=json.dumps({"MemTotal": 16 * CONFIG.GIB})), SimpleNamespace(stdout="one"), SimpleNamespace(stdout=json.dumps([other]))]
        with mock.patch.object(PUBLIC, "docker", side_effect=outputs):
            with self.assertRaisesRegex(ValueError, "other_services_memory_gib"):
                PUBLIC.require_resources(config, doc)

    def test_preflight_probes_all_endpoints_without_privileged_calls(self):
        self.save()
        config, identity = CONFIG.load_config(self.input, KIT)
        report = CHECK.preflight(config, identity, rpc_factory=lambda _: self.rpc)
        self.assertEqual(report["trace_sample"], "passed")
        self.assertEqual(report["reference_check"], "not_configured")
        self.assertEqual(report["archive_replay_qualification"], "not_run")
        self.assertEqual({method for method, _ in self.rpc.calls} - CHECK.READ_METHODS, set())

    def test_preflight_rejects_missing_history_trace_wrong_network_and_stale_archive(self):
        self.save()
        config, identity = CONFIG.load_config(self.input, KIT)
        for method, failure in (("eth_getBalance", ValueError("missing state")), ("debug_traceTransaction", None), ("debug_traceBlockByNumber", []),
                                ("eth_chainId", "0x1"), ("eth_syncing", {})):
            self.rpc.failures = {method: failure}
            with self.subTest(method=method), self.assertRaises(ValueError):
                CHECK.preflight(config, identity, rpc_factory=lambda _: self.rpc)
        self.rpc.failures = {}
        source = PublicRpcFixture(identity)
        source.height += 1
        config["rpc"]["reference_url"] = "http://reference.internal"
        with self.assertRaises(ValueError):
            CHECK.preflight(config, identity, rpc_factory=lambda url: source if url == "http://reference.internal" else self.rpc)

    def test_preflight_rejects_forked_tracing_endpoint(self):
        self.save()
        config, identity = CONFIG.load_config(self.input, KIT)
        config["rpc"]["trace_url"] = "http://trace.internal"
        trace = PublicRpcFixture(identity)
        trace.fork = True
        with self.assertRaisesRegex(ValueError, "checkpoint"):
            CHECK.preflight(config, identity, rpc_factory=lambda url: trace if url == "http://trace.internal" else self.rpc)

    def test_explorer_check_requires_matching_canonical_block_and_receipt(self):
        self.save()
        config, identity = CONFIG.load_config(self.input, KIT)
        result = CHECK.check_explorer(config, identity, rpc_factory=lambda _: self.rpc, api_fetch=self.rpc.api)
        self.assertEqual(result["status"], "CHECKED")
        with self.assertRaisesRegex(ValueError, "not indexed"):
            CHECK.check_explorer(config, identity, rpc_factory=lambda _: self.rpc, api_fetch=lambda _: {"hash": "0x" + "ff" * 32, "height": 300})

    def test_cli_lifecycle_from_extracted_release_without_node_installation(self):
        image = "ghcr.io/buckyos/usdb-explorer-gateway@sha256:" + "ab" * 32
        archive = RELEASE.package(ROOT, self.root / "dist", "0.1.0", image)
        install = self.root / "installation"
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                self.assertTrue(member.isfile() or member.isdir())
                self.assertTrue((install / member.name).resolve().is_relative_to(install.resolve()))
            tar.extractall(install)
        kit = install / "usdb-explorer-v0.1.0"
        self.assertFalse((kit / "docker").exists())
        self.assertFalse((kit / "node.env").exists())
        PUBLIC.verify_kit(kit)
        with rpc_server(self.rpc) as url:
            for key in ("read_url", "trace_url", "broadcast_url"):
                self.config["rpc"][key] = url
            self.save()
            binary_dir = self.root / "bin"
            binary_dir.mkdir()
            log = self.root / "docker-commands.jsonl"
            fake_docker(binary_dir / "docker", log)
            environment = {**os.environ, "PATH": str(binary_dir) + os.pathsep + os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"}
            for action in ("prepare", "preflight", "up", "status", "down"):
                arguments = [str(kit / "usdb-public"), action, "--state-dir", str(self.state)]
                if action == "prepare":
                    arguments += ["--config", str(self.input)]
                completed = subprocess.run(arguments, cwd=self.root, env=environment, capture_output=True, text=True, timeout=30)
                self.assertEqual(completed.returncode, 0, completed.stderr)
            commands = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertTrue(any(c[-2:] == ["up", "-d"] for c in commands))
            self.assertTrue(any(c[-1:] == ["down"] for c in commands))
            self.assertFalse(any("build" in c for c in commands))
            self.assertFalse(any("-v" in c or "admin_nodeInfo" in c or "usdb-node" in c for c in commands))
            self.assertEqual(CONFIG.read_json(self.state / "compose.json")["services"]["gateway"]["image"], image)

    def test_release_rejects_mutable_image(self):
        with self.assertRaisesRegex(ValueError, "digest"):
            RELEASE.package(ROOT, self.root / "out", "0.1.0", "example:latest")

    def test_public_mutations_take_an_independent_operation_lock(self):
        with PUBLIC.operation_lock(self.state):
            with self.assertRaisesRegex(ValueError, "another operation"):
                with PUBLIC.operation_lock(self.state):
                    pass

    def test_external_mode_cannot_reload_host_nginx(self):
        self.prepare()
        with mock.patch.object(PUBLIC, "docker") as docker:
            with self.assertRaisesRegex(ValueError, "server administrator"):
                PUBLIC.execute(PUBLIC.parser().parse_args(["reload-proxy"]), self.state)
            docker.assert_not_called()

    def test_failed_upstream_preflight_cannot_start_or_build_services(self):
        self.prepare()
        replies = [SimpleNamespace(stdout="1.55"), SimpleNamespace(stdout=json.dumps({"MemTotal": 8 * CONFIG.GIB})),
                   SimpleNamespace(stdout=""), SimpleNamespace(stdout="")]
        with mock.patch.object(PUBLIC, "docker", side_effect=replies) as docker, \
                mock.patch.object(PUBLIC, "preflight", side_effect=ValueError("wrong network")):
            with self.assertRaisesRegex(ValueError, "wrong network"):
                PUBLIC.execute(PUBLIC.parser().parse_args(["up"]), self.state)
        self.assertFalse(any(call.args[0][0] == "compose" for call in docker.call_args_list))


if __name__ == "__main__":
    unittest.main()

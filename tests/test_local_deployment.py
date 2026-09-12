#!/usr/bin/env python3
"""Cover colocated defaults, explicit upgrades, resource accounting and startup gates."""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "explorer"))
import public_config as CONFIG
import public_checks as CHECK
import usdb_public as PUBLIC
from common.public_services import PublicRpcFixture


class LocalDeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.input = self.root / "config.json"
        self.state = self.root / "deployment"
        self.config = CONFIG.read_json(PUBLIC.KIT / "config.example.json")
        self.save()

    def save(self):
        self.input.write_text(json.dumps(self.config))

    def configure(self, *arguments):
        args = PUBLIC.parser().parse_args(["configure", "--local-node", "--config", str(self.input), *arguments])
        PUBLIC.execute(args, self.state)
        return CONFIG.load_config(self.input, PUBLIC.KIT)[0]

    def prepare(self):
        PUBLIC.prepare(self.input, self.state)
        return CONFIG.read_json(self.state / "compose.json")

    def test_new_defaults_connect_host_rpc_through_an_isolated_socket(self):
        document = self.prepare()
        config = CONFIG.read_json(self.state / "config.json")
        self.assertEqual(config["rpc"]["read_url"], "http://127.0.0.1:8545")
        self.assertEqual(config["resources"]["other_services_memory_gib"], "auto")
        self.assertEqual(document["services"]["proxy"]["ports"], ["127.0.0.1:28080:8080"])
        self.assertEqual(document["services"]["rpc-host"]["network_mode"], "host")
        self.assertTrue(document["networks"]["rpc"]["internal"])
        for name in ("frontend", "proxy", "postgres", "redis"):
            self.assertNotIn("rpc", document["services"][name]["networks"])
        for name in ("backend", "gateway"):
            self.assertIn("rpc", document["services"][name]["networks"])
            self.assertNotIn("host.docker.internal", json.dumps(document["services"][name]))
        self.assertEqual(document["services"]["gateway"]["environment"]["RPC_UPSTREAM"], "http://rpc-relay:8080/read")
        for name, service in document["services"].items():
            if name != "proxy":
                self.assertNotIn("ports", service)
        host = (self.state / "rpc-host.conf").read_text()
        self.assertIn("listen unix:/run/usdb-rpc/upstream.sock;", host)
        self.assertEqual(host.count("listen "), 1)
        self.assertIn("proxy_set_header Host 127.0.0.1:8545", host)
        self.assertIn("proxy_pass http://127.0.0.1:8545/;", host)
        PUBLIC.verify(self.state)

    def test_forwarded_http_port_updates_every_advertised_origin(self):
        config = self.configure("--explorer-url", "http://192.0.2.10:38080", "--http-port", "28080")
        self.assertEqual(config["ingress"]["bind_address"], "0.0.0.0")
        self.assertEqual(config["ingress"]["exposure"], "public")
        doc = self.prepare()
        self.assertEqual(doc["services"]["proxy"]["ports"], ["0.0.0.0:28080:8080"])
        frontend = doc["services"]["frontend"]["environment"]
        self.assertEqual(frontend["NEXT_PUBLIC_API_PORT"], "38080")
        self.assertEqual(frontend["NEXT_PUBLIC_API_PROTOCOL"], "http")
        self.assertEqual(frontend["NEXT_PUBLIC_APP_HOST"], "192.0.2.10")
        self.assertEqual(frontend["NEXT_PUBLIC_NETWORK_RPC_URL"], "http://192.0.2.10:38080/rpc")
        self.assertEqual(CONFIG.read_json(self.state / "network.json")["rpcUrls"], ["http://192.0.2.10:38080/rpc"])
        self.assertNotIn("ssl_certificate", (self.state / "nginx.conf").read_text())

    def test_explicit_legacy_conversion_preserves_identity_samples_credentials_and_volumes(self):
        self.config["deployment_id"] = "operator-existing-explorer"
        self.config["rpc"] = {"read_url": "http://archive.internal:8545", "transaction": "0x" + "ab" * 32}
        self.config["ingress"] = {"mode": "external", "explorer_url": "http://127.0.0.1:28082"}
        self.config["resources"]["other_services_memory_gib"] = 0
        self.save()
        before = self.input.read_bytes()
        old = self.prepare()
        credentials = (self.state / "credentials.json").read_bytes()
        config = self.configure("--explorer-url", "http://192.0.2.10:28080")
        self.assertEqual(config["deployment_id"], self.config["deployment_id"])
        self.assertEqual(config["rpc"]["transaction"], self.config["rpc"]["transaction"])
        self.assertEqual(config["rpc"]["trace_url"], "http://127.0.0.1:8545")
        self.assertEqual(list(self.root.glob("config.json.backup-*"))[0].read_bytes(), before)
        self.assertEqual(self.input.stat().st_mode & 0o777, 0o600)
        self.assertEqual(CONFIG.read_json(self.state / "compose.json"), old)
        with mock.patch.object(PUBLIC, "require_stopped"):
            PUBLIC.prepare(self.input, self.state, replace=True)
        updated = CONFIG.read_json(self.state / "compose.json")
        self.assertEqual(updated["name"], old["name"])
        self.assertEqual(updated["volumes"]["postgres-data"], old["volumes"]["postgres-data"])
        self.assertEqual((self.state / "credentials.json").read_bytes(), credentials)

    def test_bad_local_or_ingress_config_is_rejected_without_overwriting_input(self):
        before = self.input.read_bytes()
        for options in (("--rpc-url", "http://archive.internal:8545"),
                        ("--explorer-url", "http://192.0.2.10:38080", "--http-port", "0"),
                        ("--explorer-url", "http://127.0.0.1:28080", "--bind-address", "0.0.0.0")):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.configure(*options)
            self.assertEqual(self.input.read_bytes(), before)
        self.assertFalse(list(self.root.glob("config.json.backup-*")))
        self.config["network"] = "usdb-mainnet-v1"
        self.save()
        with self.assertRaisesRegex(ValueError, "supported USDB testnet"):
            CONFIG.load_config(self.input, PUBLIC.KIT)

    def test_auto_budget_counts_caps_and_still_rejects_overcommit_or_uncapped_services(self):
        doc = self.prepare()
        config = CONFIG.read_json(self.state / "config.json")
        other = {"Config": {"Labels": {"com.docker.compose.project": "usdb-node"}}, "HostConfig": {"Memory": 4 * CONFIG.GIB}}
        for host, cap, error in ((16, 4, None), (8, 4, "exceeds Docker host RAM"), (16, 0, "no memory cap")):
            other["HostConfig"]["Memory"] = cap * CONFIG.GIB
            output = [SimpleNamespace(stdout=json.dumps({"MemTotal": host * CONFIG.GIB})),
                      SimpleNamespace(stdout="node"), SimpleNamespace(stdout=json.dumps([other]))]
            with self.subTest(host=host, cap=cap), mock.patch.object(PUBLIC, "docker", side_effect=output):
                if error:
                    with self.assertRaisesRegex(ValueError, error):
                        PUBLIC.require_resources(config, doc)
                else:
                    PUBLIC.require_resources(config, doc)

    def test_up_checks_private_relay_before_starting_consumers(self):
        self.prepare()
        identity = CONFIG.read_json(self.state / "identity.json")
        fixture = PublicRpcFixture(identity)
        report = {"checkpoint": {"number": 10, "hash": fixture.block(10)["hash"]}}
        commands = []

        def compose(root, arguments, **kwargs):
            commands.append(arguments)
            if "--post-data=" in " ".join(arguments):
                request = json.loads(next(arg.split("=", 1)[1] for arg in arguments if arg.startswith("--post-data=")))
                value = {"jsonrpc": "2.0", "id": 1, "result": fixture(request["method"], request["params"])}
                return SimpleNamespace(stdout=json.dumps(value))
            return SimpleNamespace(stdout="")

        with mock.patch.object(PUBLIC, "docker", return_value=SimpleNamespace(stdout="1.55")), \
                mock.patch.object(PUBLIC, "require_resources"), mock.patch.object(PUBLIC, "require_database_identity"), \
                mock.patch.object(PUBLIC, "require_local_docker"), mock.patch.object(PUBLIC, "preflight", return_value=report), \
                mock.patch.object(PUBLIC, "compose", side_effect=compose):
            PUBLIC.execute(PUBLIC.parser().parse_args(["up"]), self.state)
            self.assertEqual(commands[-1], ["up", "-d"])
            self.assertLess(next(i for i, c in enumerate(commands) if "--wait" in c), next(i for i, c in enumerate(commands) if "exec" in c))
            self.assertEqual(len([c for c in commands if "exec" in c]), 15)
            commands.clear()
            fixture.failures["eth_chainId"] = "0x1"
            with self.assertRaisesRegex(ValueError, "container read_url.*network identity"):
                PUBLIC.execute(PUBLIC.parser().parse_args(["up"]), self.state)
            self.assertNotIn(["up", "-d"], commands)

    def test_local_mode_rejects_a_different_docker_host_or_rootless_network_namespace(self):
        cases = [("unix:///var/run/docker.sock", "Debian GNU/Linux", [], False),
                 ("ssh://remote", "Debian GNU/Linux", [], True),
                 ("unix:///var/run/docker.sock", "Docker Desktop", [], True),
                 ("unix:///run/user/1000/docker.sock", "Debian GNU/Linux", ["name=rootless"], True)]
        for endpoint, operating_system, security, rejected in cases:
            output = [SimpleNamespace(stdout=json.dumps([{"Endpoints": {"docker": {"Host": endpoint}}}])),
                      SimpleNamespace(stdout=json.dumps({"OSType": "linux", "OperatingSystem": operating_system,
                                                         "SecurityOptions": security}))]
            with self.subTest(endpoint=endpoint, operating_system=operating_system), \
                    mock.patch.dict(PUBLIC.os.environ, {}, clear=True), mock.patch.object(PUBLIC, "docker", side_effect=output):
                if rejected:
                    with self.assertRaisesRegex(ValueError, "local, rootful Linux"):
                        PUBLIC.require_local_docker()
                else:
                    PUBLIC.require_local_docker()

    def test_transport_errors_explain_the_cause_without_disclosing_private_url(self):
        cases = [(socket.gaierror(-2, "private-token"), "DNS lookup failed"),
                 (ConnectionRefusedError("private-token"), "connection refused"),
                 (TimeoutError("private-token"), "timed out"), (ssl.SSLError("private-token"), "TLS")]
        for reason, message in cases:
            with self.subTest(message=message), mock.patch.object(CHECK.urllib.request, "build_opener") as opener:
                opener.return_value.open.side_effect = urllib.error.URLError(reason)
                with self.assertRaisesRegex(ValueError, message) as error:
                    CHECK.fetch("http://private.internal/private-token")
                self.assertNotIn("private", str(error.exception))


if __name__ == "__main__":
    unittest.main()

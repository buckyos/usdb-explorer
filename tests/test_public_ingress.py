#!/usr/bin/env python3
"""Exercise generated ingress configurations with real, isolated Nginx containers."""
import json
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from contextlib import ExitStack

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "explorer"))
from public_config import image_lock, nginx_config, read_json
import public_checks as CHECK
import usdb_public as PUBLIC
from common.public_services import PublicRpcFixture, rpc_server


class LocalRpcContainers(unittest.TestCase):
    def test_host_loopback_vhosts_and_separate_routes_from_an_isolated_container(self):
        with tempfile.TemporaryDirectory(prefix="explorer-local-rpc-") as temporary, ExitStack() as stack:
            root = Path(temporary)
            config = read_json(ROOT / "explorer/config.example.json")
            config["deployment_id"] = "explorer-local-test-" + uuid.uuid4().hex[:10]
            identity = read_json(ROOT / "explorer/networks/usdb-testnet-v0.json")
            fixtures, requests = {}, {}
            for route in ("read", "trace", "broadcast"):
                fixtures[route] = PublicRpcFixture(identity)
                requests[route] = []
                url = stack.enter_context(rpc_server(fixtures[route], request_observer=lambda path, headers, r=route:
                                                      requests[r].append((path, headers["Host"]))))
                config["rpc"][route + "_url"] = url + "/node-rpc"
            config["rpc"].update(transaction=fixtures["read"].transaction, historical_block=10)
            path = root / "operator.json"
            path.write_text(json.dumps(config))
            state = root / "deployment"
            PUBLIC.prepare(path, state)
            config = read_json(state / "config.json")
            report = CHECK.preflight(config, identity)
            image = image_lock(ROOT / "explorer")["images"]["nginx"]["reference"]
            if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode:
                IngressContainers.docker("pull", image)
            # Only this random test project is removed; no real node or deployment is operated.
            stack.callback(PUBLIC.compose, state, ["down", "--volumes"], capture=True, timeout=60)
            PUBLIC.compose(state, ["up", "-d", "--wait", "--wait-timeout", "40", "rpc-host", "rpc-relay"], capture=True, timeout=60)
            PUBLIC.check_local_relay(state, config, identity, report)
            for route in fixtures:
                fixtures[route].calls.clear()
                requests[route].clear()
            network = config["deployment_id"] + "_rpc"

            def probe(route, method="eth_chainId"):
                return IngressContainers.docker("run", "--rm", "--network", network, "--memory", "64m", "--cpus", "0.25",
                    "--entrypoint", "wget", image, "-q", "-O", "-", "-T", "3", "--header=Content-Type:application/json",
                    "--post-data=" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": []}),
                    "http://rpc-relay:8080/" + route)

            for route, fixture in fixtures.items():
                self.assertEqual(json.loads(probe(route))["result"], hex(identity["chain_id"]))
                self.assertEqual(fixture.calls, [("eth_chainId", [])])
                self.assertEqual(requests[route], [("/node-rpc", config["rpc"][route + "_url"].split("/")[2])])
            with self.assertRaises(subprocess.CalledProcessError):
                probe("unconfigured-route")
            # Restart the socket owner to exercise stale socket cleanup and relay recovery.
            PUBLIC.compose(state, ["restart", "rpc-host"], capture=True, timeout=30)
            for attempt in range(20):
                try:
                    self.assertEqual(json.loads(probe("read"))["result"], hex(identity["chain_id"]))
                    break
                except subprocess.CalledProcessError:
                    if attempt == 19:
                        raise
                    time.sleep(.1)


class IngressContainers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="usdb-public-ingress-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.prefix = "usdb-public-ingress-" + uuid.uuid4().hex[:10]
        cls.image = image_lock(ROOT / "explorer")["images"]["nginx"]["reference"]
        if subprocess.run(["docker", "image", "inspect", cls.image], capture_output=True).returncode:
            cls.docker("pull", cls.image)
        cls.docker("network", "create", cls.prefix)
        cls.addClassCleanup(cls.docker, "network", "rm", cls.prefix)
        fixture = cls.root / "fixture.conf"
        fixture.write_text('events {}\nhttp { server { listen 8080; location / { return 200 "$request_uri"; } } '
                           'server { listen 3000; location / { return 200 "frontend:$request_uri"; } } }\n')
        cls.fixture = cls.start("fixture", ["--network-alias", "frontend", "--network-alias", "gateway"], fixture)
        cls.fixture_ip = json.loads(cls.docker("inspect", cls.fixture))[0]["NetworkSettings"]["Networks"][cls.prefix]["IPAddress"]
        cls.certificates = cls.root / "tls"
        cls.certificates.mkdir()
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                        "-keyout", str(cls.certificates / "privkey.pem"), "-out", str(cls.certificates / "fullchain.pem")],
                       check=True, capture_output=True)

    @staticmethod
    def docker(*args):
        return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=60).stdout.strip()

    @classmethod
    def start(cls, suffix, extra, configuration):
        name = cls.prefix + "-" + suffix
        cls.docker("run", "--rm", "-d", "--name", name, "--network", cls.prefix,
                   "--memory", "128m", "--cpus", "0.25", "--security-opt", "no-new-privileges:true",
                   "-v", str(configuration) + ":/etc/nginx/nginx.conf:ro", *extra, cls.image)
        cls.addClassCleanup(cls.docker, "rm", "-f", name)
        return name

    def request(self, url, *, method="GET", context=None):
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), NoRedirect)
        request = urllib.request.Request(url, method=method, data=b"{}" if method == "POST" else None)
        for attempt in range(30):
            try:
                with opener.open(request, timeout=3) as response:
                    return response.status, response.read().decode(), response.headers
            except urllib.error.HTTPError as error:
                return error.code, error.read().decode(), error.headers
            except urllib.error.URLError:
                if attempt == 29:
                    raise
                time.sleep(.1)

    def test_bundled_http_resolves_private_services_and_preserves_routes(self):
        config = {"ingress": {"mode": "bundled", "exposure": "public", "bind_address": "0.0.0.0",
                              "explorer_url": "http://192.0.2.10:38080"}}
        path = self.root / "bundled-http.conf"
        path.write_text(nginx_config(config))
        name = self.start("http", ["-p", "127.0.0.1::8080"], path)
        port = self.docker("port", name, "8080/tcp").rsplit(":", 1)[1]
        origin = "http://127.0.0.1:" + port
        self.docker("exec", name, "nginx", "-t")
        for route, expected in (("/rpc", "/"), ("/network.json", "/network.json"),
                                ("/api/v2/blocks?test=1", "/api/v2/blocks?test=1"), ("/blocks/35", "frontend:/blocks/35")):
            status, body, _ = self.request(origin + route, method="POST" if route == "/rpc" else "GET")
            self.assertEqual((status, body), (200, expected))
        self.assertEqual(self.request(origin + "/socket")[0], 404)

    def test_external_locations_work_in_an_existing_server_block(self):
        config = {"ingress": {"mode": "external", "bind_address": self.fixture_ip, "web_port": 3000, "gateway_port": 8080,
                              "explorer_url": "https://explorer.example.com"}}
        path = self.root / "external.conf"
        path.write_text("events {}\nhttp { server { listen 8080;\n" + nginx_config(config, external=True) + "} }\n")
        name = self.start("external", ["-p", "127.0.0.1::8080"], path)
        port = self.docker("port", name, "8080/tcp").rsplit(":", 1)[1]
        self.docker("exec", name, "nginx", "-t")
        self.assertEqual(self.request("http://127.0.0.1:" + port + "/rpc", method="POST")[:2], (200, "/"))
        self.assertEqual(self.request("http://127.0.0.1:" + port + "/api/v2/transactions")[1], "/api/v2/transactions")

    def test_bundled_https_certificate_and_checked_reload(self):
        config = {"ingress": {"mode": "bundled", "bind_address": "127.0.0.1", "explorer_url": "https://localhost:28443"}}
        path = self.root / "bundled-tls.conf"
        path.write_text(nginx_config(config))
        name = self.start("tls", ["-p", "127.0.0.1::8080", "-p", "127.0.0.1::8443", "-v", str(self.certificates) + ":/tls:ro"], path)
        port = self.docker("port", name, "8443/tcp").rsplit(":", 1)[1]
        http_port = self.docker("port", name, "8080/tcp").rsplit(":", 1)[1]
        context = ssl.create_default_context(cafile=str(self.certificates / "fullchain.pem"))
        self.assertEqual(self.request("https://127.0.0.1:" + port + "/network.json", context=context)[:2], (200, "/network.json"))
        status, _, headers = self.request("http://127.0.0.1:" + http_port + "/blocks/35")
        self.assertEqual((status, headers["Location"]), (308, "https://localhost:28443/blocks/35"))
        self.docker("exec", name, "nginx", "-t")
        self.docker("exec", name, "nginx", "-s", "reload")
        self.assertEqual(self.request("https://127.0.0.1:" + port + "/rpc", method="POST", context=context)[:2], (200, "/"))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


if __name__ == "__main__":
    unittest.main()

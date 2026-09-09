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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "explorer"))
from public_config import image_lock, nginx_config


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
        config = {"ingress": {"mode": "bundled", "bind_address": "127.0.0.1", "explorer_url": "http://localhost:28080"}}
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

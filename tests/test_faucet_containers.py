#!/usr/bin/env python3
"""Exercise the release signer image, real Nginx and restart recovery against an isolated fake chain."""
import argparse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "explorer"))
from public_config import image_lock, nginx_config


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=45, **kwargs).stdout.strip()


class ChainFixture:
    """All money and keys are synthetic; broadcasts deliberately lose their response."""
    def __init__(self):
        self.balance = "0x0"
        self.mined = False
        self.nonces = {}
        self.sent = []
        self.mutex = threading.Lock()

    def respond(self, payload):
        method, params = payload["method"], payload["params"]
        with self.mutex:
            values = {"eth_chainId": hex(202608250), "eth_syncing": False, "eth_getBalance": self.balance,
                      "eth_gasPrice": hex(10**9),
                      "eth_getCode": "0x", "eth_blockNumber": "0xc"}
            if method == "eth_getBlockByNumber":
                result = {"hash": "0x" + ("a" if params[0] == "0x0" else "c") * 64}
            elif method == "eth_getTransactionReceipt":
                result = {"transactionHash": params[0], "blockHash": "0x" + "c" * 64,
                          "blockNumber": "0xa", "status": "0x1"} if self.mined else None
            elif method == "eth_getTransactionByHash":
                result = None
            elif method == "eth_getTransactionCount":
                result = hex(self.nonces.get(params[0].lower(), 0))
            elif method == "eth_sendRawTransaction":
                self.sent.append(params[0])
                return {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32000, "message": "injected uncertain broadcast"}}
            else:
                result = values[method]
            return {"jsonrpc": "2.0", "id": payload["id"], "result": result}


def eventually(fn):
    deadline = time.monotonic() + 35
    while True:
        try:
            result = fn()
            if result:
                return result
        except (OSError, urllib.error.URLError, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise AssertionError("isolated faucet did not reach the expected state")
        time.sleep(.3)


def exercise(image):
    name = "explorer-faucet-test-" + uuid.uuid4().hex[:10]
    fixture = ChainFixture()
    with ExitStack() as cleanup, tempfile.TemporaryDirectory(prefix="explorer-faucet-test-") as temporary:
        docker("network", "create", "--internal", name)
        cleanup.callback(docker, "network", "rm", name)
        edge = name + "-edge"
        docker("network", "create", edge)
        cleanup.callback(docker, "network", "rm", edge)
        gateway_ip = json.loads(docker("network", "inspect", name))[0]["IPAM"]["Config"][0]["Gateway"]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                response = json.dumps(fixture.respond(payload)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *args):
                pass

        # Bind only the private test bridge, never a public host address.
        server = ThreadingHTTPServer((gateway_ip, 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        url = f"http://{gateway_ip}:{server.server_port}"
        config = {"chain_id": "202608250", "genesis_hash": "0x" + "a" * 64, "read_url": url, "broadcast_url": url,
                  "origin": "http://explorer.invalid", "proxy_token": "b" * 64, "claim_amount": "1", "daily_budget": "100",
                  "gas_reserve": "0.01", "max_gas_price_gwei": "100", "cooldown_seconds": 86400,
                  "ip_daily_claims": 1, "ip_requests_per_minute": 10, "confirmations": 3}
        volume = name + "-data"
        docker("volume", "create", volume)
        cleanup.callback(docker, "volume", "rm", volume)
        container = docker("run", "-d", "--name", name, "--network", name, "--network-alias", "faucet",
                           "--memory", "256m", "--cpus", "0.5", "--read-only", "--cap-drop", "ALL",
                           "--security-opt", "no-new-privileges:true", "--tmpfs", "/tmp:size=16m",
                           "-v", volume + ":/data", "-e", "FAUCET_CONFIG=" + json.dumps(config), "--entrypoint", "/faucet", image, "serve")
        cleanup.callback(docker, "rm", "-f", container)
        nginx = image_lock(ROOT / "explorer")["images"]["nginx"]["reference"]
        path = Path(temporary) / "nginx.conf"
        path.write_text(nginx_config({"faucet": {"enabled": True}, "ingress": {"mode": "bundled", "explorer_url": config["origin"], "bind_address": "127.0.0.1"}}, proxy_token=config["proxy_token"]))
        proxy = docker("run", "-d", "--network", edge, "--memory", "64m", "-p", "127.0.0.1::8080",
                       "-v", str(path) + ":/etc/nginx/nginx.conf:ro", nginx)
        cleanup.callback(docker, "rm", "-f", proxy)
        docker("network", "connect", name, proxy)
        origin = "http://" + docker("port", proxy, "8080/tcp")

        def request(resource, payload=None, headers=None):
            req = urllib.request.Request(origin + "/api/faucet/v1/" + resource,
                data=json.dumps(payload).encode() if payload else None,
                headers={"Content-Type": "application/json", **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)

        status = eventually(lambda: (value if (value := request("status")[1]).get("status") == "INSUFFICIENT_FUNDS" else None))
        address = status["address"]
        assert status["enabled"] and len(address) == 42
        inspection = json.loads(docker("inspect", container))[0]
        assert inspection["Config"]["User"] == "65532:65532"
        fixture.balance = hex(1000 * 10**18)
        # A funding timeout retains signed bytes in the volume. A retry needs no key.
        command = ["docker", "exec", "-i", container, "/faucet", "fund", "--amount", "10", "--request-id", "fixture-refill"]
        result = subprocess.run(command, input="0" * 63 + "1", capture_output=True, text=True, timeout=100)
        assert result.returncode == 1 and "BROADCAST_UNCERTAIN" in result.stderr, result.stderr
        funded = json.loads(result.stdout)
        retry = ["docker", "exec", container, "/faucet", "retry", "--request-id", "fixture-refill"]
        result = subprocess.run(retry, capture_output=True, text=True, timeout=100)
        assert json.loads(result.stdout)["transaction_hash"] == funded["transaction_hash"]
        assert len(fixture.sent) == 2 and fixture.sent[0] == fixture.sent[1]
        fixture.mined = True
        fixture.nonces["0x7e5f4552091a69125d5dfcb7b8c2659029395bdf"] = 1
        assert json.loads(subprocess.check_output(retry, text=True))["status"] == "confirmed"
        fixture.mined = False
        eventually(lambda: request("status")[1].get("status") == "ready")
        application = {"request_id": "container-claim-012345", "address": "0x" + "11" * 20}
        code, job = request("claims", application)
        assert code == 202, job
        job = eventually(lambda: (value if (value := request("claims/" + job["id"])[1]).get("transaction_hash") else None))
        eventually(lambda: json.loads(docker("exec", container, "/faucet", "status"))["faucet"]["status"] == "BROADCAST_UNCERTAIN")
        before = len(fixture.sent)
        docker("restart", "-t", "10", container)
        eventually(lambda: len(fixture.sent) > before)
        assert request("status")[1]["address"] == address, "wallet rotated during restart"
        assert len(set(fixture.sent[2:])) == 1, "recovery created another claim transaction"
        fixture.mined = True
        fixture.nonces[address.lower()] = 1
        eventually(lambda: request("claims/" + job["id"])[1].get("status") == "confirmed")
        code, duplicate = request("claims", application)
        assert code == 202 and duplicate["transaction_hash"] == job["transaction_hash"]
        # Browser-supplied forwarding headers cannot bypass the trusted ingress's per-IP quota.
        code, rejected = request("claims", {"request_id": "another-claim-012345", "address": "0x" + "22" * 20},
                                 {"X-Forwarded-For": "203.0.113.77", "X-USDB-Faucet-Proxy": "forged"})
        assert code == 429 and rejected["error"]["code"] == "IP_DAILY_LIMIT", rejected
        print("Faucet container checks passed: non-root wallet, funding recovery, nginx trust, durable claims, restart and confirmation.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    exercise(parser.parse_args().image)

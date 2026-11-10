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
        self.mined_hashes = set()
        self.fail_method = ""
        self.nonces = {}
        self.sent = []
        self.mutex = threading.Lock()

    def respond(self, payload):
        method, params = payload["method"], payload["params"]
        with self.mutex:
            if method == self.fail_method:
                return {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32000, "message": "injected read failure"}}
            values = {"eth_chainId": hex(202610030), "eth_syncing": False, "eth_getBalance": self.balance,
                      "eth_gasPrice": hex(10**9),
                      "eth_getCode": "0x", "eth_blockNumber": "0xc"}
            if method == "eth_getBlockByNumber":
                result = {"hash": "0x" + ("a" if params[0] == "0x0" else "c") * 64,
                          "number": "0xc" if params[0] == "latest" else params[0]}
            elif method == "eth_getTransactionReceipt":
                result = {"transactionHash": params[0], "blockHash": "0x" + "c" * 64,
                          "blockNumber": "0xa", "status": "0x1"} if self.mined or params[0] in self.mined_hashes else None
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
        config = {"chain_id": "202610030", "genesis_hash": "0x" + "a" * 64, "read_url": url, "broadcast_url": url,
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
        # Confirmation is observed by the worker after restart, without retry.
        with fixture.mutex:
            fixture.mined_hashes.add(funded["transaction_hash"])
            fixture.nonces["0x7e5f4552091a69125d5dfcb7b8c2659029395bdf"] = 1
        docker("restart", "-t", "10", container)
        def funding_confirmed(identifier):
            status = json.loads(docker("exec", container, "/faucet", "status"))
            return any(item["id"] == identifier and item["status"] == "confirmed" for item in status["funding"])
        eventually(lambda: funding_confirmed("fixture-refill"))
        assert len(fixture.sent) == 2, "receipt refresh rebroadcast the refill"
        # An independent refill now reaches broadcast, without a manual confirmation step.
        command[-1] = "fixture-refill-2"
        result = subprocess.run(command, input="0" * 63 + "1", capture_output=True, text=True, timeout=100)
        assert result.returncode == 1 and "BROADCAST_UNCERTAIN" in result.stderr, result.stderr
        second = json.loads(result.stdout)
        assert second["transaction_hash"] != funded["transaction_hash"] and len(fixture.sent) == 3
        # Pending errors identify the previous transfer; failures before a job exists emit no empty JSON.
        command[-1] = "fixture-refill-blocked"
        result = subprocess.run(command, input="0" * 63 + "1", capture_output=True, text=True, timeout=100)
        assert result.returncode == 1 and "FUNDING_PENDING" in result.stderr, result.stderr
        assert json.loads(result.stdout)["id"] == "fixture-refill-2" and len(fixture.sent) == 3
        result = subprocess.run(["docker", "exec", container, "/faucet", "retry", "--request-id", "missing-refill"], capture_output=True, text=True, timeout=100)
        assert result.returncode == 1 and not result.stdout.strip() and "FUNDING_NOT_FOUND" in result.stderr
        with fixture.mutex:
            fixture.mined_hashes.add(second["transaction_hash"])
            fixture.nonces["0x7e5f4552091a69125d5dfcb7b8c2659029395bdf"] = 2
        eventually(lambda: funding_confirmed("fixture-refill-2"))
        claim_broadcast_start = len(fixture.sent)
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
        assert len(set(fixture.sent[claim_broadcast_start:])) == 1, "recovery created another claim transaction"
        fixture.mined = True
        fixture.nonces[address.lower()] = 1
        eventually(lambda: request("claims/" + job["id"])[1].get("status") == "confirmed")
        code, duplicate = request("claims", application)
        assert code == 202 and duplicate["transaction_hash"] == job["transaction_hash"]
        # Browser-supplied forwarding headers cannot bypass the trusted ingress's per-IP quota.
        code, rejected = request("claims", {"request_id": "another-claim-012345", "address": "0x" + "22" * 20},
                                 {"X-Forwarded-For": "203.0.113.77", "X-USDB-Faucet-Proxy": "forged"})
        assert code == 429 and rejected["error"]["code"] == "IP_DAILY_LIMIT", rejected
        report = json.loads(docker("exec", container, "/faucet", "status", "--limit", "1"))
        assert report["chain"]["status"] == "ok" and report["chain"]["balance_usdb"] == "1000", report
        assert report["chain"]["nonce"] == "1" and report["chain"]["block_number"] == 12
        assert report["faucet"]["balance_usdb"] == "1000"
        assert len(report["claims"]) == 1 and report["claims"][0]["transaction_hash"] == job["transaction_hash"]
        assert report["claims"][0]["status"] == "confirmed" and report["claims"][0]["amount_usdb"] == "1"
        assert report["claims"][0]["created_at_utc"].endswith("Z")
        assert len(report["funding"]) == 1 and report["funding"][0]["id"] == "fixture-refill-2"
        assert report["history"] == {"limit": 1, "funding_has_more": True, "claims_has_more": False, "transfers_has_more": False}
        public = request("status")[1]
        assert not any(key in public for key in ("chain", "claims", "funding", "transfers", "history"))
        result = subprocess.run(["docker", "exec", container, "/faucet", "status", "--limit", "101"], capture_output=True, text=True, timeout=100)
        assert result.returncode == 1 and "INVALID_LIMIT" in result.stderr and not result.stdout.strip()
        # RPC failures keep the local records visible without inventing a live balance.
        with fixture.mutex:
            fixture.fail_method = "eth_getBalance"
        unavailable = json.loads(docker("exec", container, "/faucet", "status", "--limit", "1"))
        assert unavailable["chain"]["status"] == "unavailable" and unavailable["chain"]["error"] == "RPC_REJECTED"
        assert "balance_usdb" not in unavailable["chain"] and len(unavailable["claims"]) == 1
        with fixture.mutex:
            fixture.fail_method = ""
            fixture.mined_hashes.add(job["transaction_hash"])
            fixture.mined = False
        # Directed miner transfers reuse the one-shot signing and nonce gate.
        miner_command = ["docker", "exec", "-i", container, "/faucet", "transfer", "--from", "miner", "--to", "0x" + "33" * 20,
                         "--amount", "5", "--request-id", "fixture-miner-transfer"]
        result = subprocess.run(miner_command, input="0" * 63 + "1", capture_output=True, text=True, timeout=100)
        assert result.returncode == 1 and "BROADCAST_UNCERTAIN" in result.stderr, result.stderr
        directed = json.loads(result.stdout)
        assert directed["source"] == "miner" and directed["operation"] == "transfer" and directed["address"] == "0x" + "33" * 20
        command[-1] = "fund-blocked-by-directed"
        result = subprocess.run(command, input="0" * 63 + "1", capture_output=True, text=True, timeout=100)
        assert result.returncode == 1 and "TRANSFER_PENDING" in result.stderr and json.loads(result.stdout)["id"] == directed["id"]
        def transfer_status(identifier):
            status = json.loads(docker("exec", container, "/faucet", "status"))
            return next((item for item in status["transfers"] if item["id"] == identifier), {})
        with fixture.mutex:
            fixture.mined_hashes.add(directed["transaction_hash"])
            fixture.nonces["0x7e5f4552091a69125d5dfcb7b8c2659029395bdf"] = 3
        eventually(lambda: transfer_status(directed["id"]).get("status") == "confirmed")
        # The operator queue needs no private-key input and uses the worker after a public claim.
        wallet_command = ("exec", container, "/faucet", "transfer", "--from", "faucet", "--to", "0x" + "44" * 20,
                          "--amount", "7", "--request-id", "fixture-wallet-transfer")
        wallet_broadcast_start = len(fixture.sent)
        manual = json.loads(docker(*wallet_command))
        assert manual["status"] == "queued" and manual["source"] == "faucet" and manual["operation"] == "transfer"
        assert json.loads(docker(*wallet_command))["id"] == manual["id"]
        manual = eventually(lambda: (value if (value := transfer_status(manual["id"])).get("transaction_hash") else None))
        eventually(lambda: len(fixture.sent) > wallet_broadcast_start)
        before = len(fixture.sent)
        wallet_raw = fixture.sent[wallet_broadcast_start]
        docker("restart", "-t", "10", container)
        eventually(lambda: len(fixture.sent) > before)
        assert all(raw == wallet_raw for raw in fixture.sent[before:]), "manual recovery sent different transaction bytes"
        with fixture.mutex:
            fixture.mined_hashes.add(manual["transaction_hash"])
            fixture.nonces[address.lower()] = 2
        eventually(lambda: transfer_status(manual["id"]).get("status") == "confirmed")
        final = json.loads(docker("exec", container, "/faucet", "status", "--limit", "1"))
        assert final["transfers"][0]["id"] == manual["id"] and final["transfers"][0]["amount_usdb"] == "7"
        assert final["history"]["transfers_has_more"] is True
        assert final["faucet"]["reserved_atoms"] == report["faucet"]["reserved_atoms"], "operator transfer consumed public budget"
        print("Faucet container checks passed: funding recovery, automatic confirmation, miner/wallet directed transfers, shared worker restart, public limits, live balances and bounded history.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    exercise(parser.parse_args().image)

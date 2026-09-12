"""Bounded RPC and Docker-process fixtures for standalone deployment acceptance."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading


class PublicRpcFixture:
    def __init__(self, identity):
        self.identity = identity
        self.height = 300
        self.transaction = "0x" + "bb" * 32
        self.calls = []
        self.failures = {}
        self.fork = False

    def block(self, number):
        digest = self.identity["genesis_block_hash"] if number == 0 else "0x" + hashlib.sha256(str(number).encode()).hexdigest()
        if self.fork and number:
            digest = "0x" + "ff" * 32
        return {"number": hex(number), "hash": digest, "miner": "0x" + "11" * 20,
                "transactions": [self.transaction] if number == 10 else []}

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method in self.failures:
            result = self.failures[method]
            if isinstance(result, Exception):
                raise result
            return deepcopy(result)
        if method == "eth_getBlockByNumber":
            number = self.height if params[0] == "latest" else int(params[0], 16)
            return self.block(number) if number <= self.height else None
        values = {"eth_chainId": hex(self.identity["chain_id"]), "net_version": str(self.identity["network_id"]),
                  "eth_syncing": False, "eth_getBalance": "0x0", "eth_getCode": "0x", "eth_call": "0x",
                  "eth_getTransactionReceipt": {"transactionHash": self.transaction, "blockNumber": "0xa",
                      "blockHash": self.block(10)["hash"], "status": "0x1", "gasUsed": "0x5208", "effectiveGasPrice": "0x2"},
                  "debug_traceTransaction": {"type": "CALL"}, "debug_traceBlockByNumber": [{"result": {"type": "CALL"}}]}
        if method not in values:
            raise AssertionError(f"Unexpected or privileged RPC method: {method}")
        return deepcopy(values[method])

    def api(self, url):
        if "/transactions/" in url:
            return {"hash": self.transaction, "block_number": 10, "gas_used": "21000", "result": "success", "fee": {"value": "42000"}}
        return {"hash": self.block(self.height)["hash"], "height": self.height}


@contextmanager
def rpc_server(fixture, *, request_observer=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if request_observer is not None:
                request_observer(self.path, self.headers)
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            response = {"jsonrpc": "2.0", "id": request["id"]}
            try:
                response["result"] = fixture(request["method"], request["params"])
            except ValueError:
                response["error"] = {"code": -32000, "message": "fixture failure"}
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def fake_docker(path, log):
    """Record lifecycle commands without starting a container or touching a node."""
    path.write_text('''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
with open(LOG_PATH, 'a') as output:
    output.write(json.dumps(args) + '\\n')
if args[0] == 'version': print('1.55')
elif args[0] == 'info': print(json.dumps({'MemTotal': 128 * 1024**3}))
'''.replace("LOG_PATH", repr(str(log))))
    path.chmod(0o755)

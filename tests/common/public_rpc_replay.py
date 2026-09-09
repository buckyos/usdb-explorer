#!/usr/bin/env python3
"""Replay public block/receipt samples for container tests; no state, tracing or broadcast support."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys


def serve(path):
    sample = json.loads(Path(path).read_text())
    by_number = {int(block["number"], 16): block for block in sample["blocks"]}
    by_hash = {block["hash"]: block for block in sample["blocks"]}
    transactions = {tx["hash"]: tx for block in sample["blocks"] for tx in block["transactions"]}

    def handle(item):
        method, params = item.get("method"), item.get("params", [])
        value = None
        if method == "eth_getBlockByNumber":
            height = max(by_number) if params[0] in {"latest", "pending"} else int(params[0], 16)
            value = by_number.get(height)
        elif method == "eth_getBlockByHash":
            value = by_hash.get(params[0])
        elif method == "eth_blockNumber":
            value = hex(max(by_number))
        elif method == "eth_chainId":
            value = sample["chain_id"]
        elif method == "net_version":
            value = str(int(sample["chain_id"], 16))
        elif method == "eth_syncing":
            value = False
        elif method == "web3_clientVersion":
            value = "USDB/block-receipt-replay-fixture"
        elif method == "eth_getTransactionReceipt":
            value = sample["receipts"].get(params[0])
        elif method == "eth_getTransactionByHash":
            value = transactions.get(params[0])
        elif method == "eth_getLogs":
            start = int(params[0].get("fromBlock", "0x0"), 16)
            end = int(params[0].get("toBlock", hex(max(by_number))), 16)
            value = [log for r in sample["receipts"].values() for log in r["logs"] if start <= int(log["blockNumber"], 16) <= end]
        else:
            return {"jsonrpc": "2.0", "id": item.get("id"), "error": {"code": -32601, "message": "Replay fixture has no state, tracing or broadcast API"}}
        if method in {"eth_getBlockByNumber", "eth_getBlockByHash"} and value and not params[1]:
            value = {**value, "transactions": [tx["hash"] for tx in value["transactions"]]}
        return {"jsonrpc": "2.0", "id": item.get("id"), "result": value}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            response = [handle(item) for item in data] if isinstance(data, list) else handle(data)
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    ThreadingHTTPServer(("0.0.0.0", 8545), Handler).serve_forever()


if __name__ == "__main__":
    serve(sys.argv[1])

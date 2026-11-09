#!/usr/bin/env python3
"""Validate generated trace settings against the pinned, network-isolated Blockscout runtime."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "explorer"))
import public_config as CONFIG
import usdb_public as PUBLIC


class BackendTracingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = CONFIG.image_lock(ROOT / "explorer")["images"]["backend"]["reference"]
        if subprocess.run(["docker", "image", "inspect", cls.image], capture_output=True).returncode:
            subprocess.run(["docker", "pull", cls.image], check=True, timeout=300)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="explorer-trace-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = self.root / "config.json"
        source.write_text(json.dumps(CONFIG.read_json(ROOT / "explorer/config.example.json")))
        state = self.root / "deployment"
        with redirect_stdout(io.StringIO()):
            PUBLIC.prepare(source, state)
        self.environment = CONFIG.read_json(state / "compose.json")["services"]["backend"]["environment"]

    def probe(self, fixture):
        name = "explorer-trace-test-" + uuid.uuid4().hex[:12]
        command = ["docker", "run", "--rm", "-i", "--name", name, "--network", "none",
                   "--memory", "1g", "--cpus", "2", "-e", "ERL_FLAGS=+S 2:2"]
        for key in ("ETHEREUM_JSONRPC_GETH_TRACE_BY_BLOCK", "INDEXER_INTERNAL_TRANSACTIONS_TRACER_TYPE",
                    "ETHEREUM_JSONRPC_DEBUG_TRACE_TRANSACTION_TIMEOUT"):
            if key in self.environment:
                command += ["-e", key + "=" + self.environment[key]]
        command += ["--entrypoint", "bin/blockscout", self.image, "eval",
                    (ROOT / "tests/common/blockscout_trace_probe.exs").read_text()]
        try:
            result = subprocess.run(command, input=json.dumps(fixture), text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-3000:])
            line = next(line for line in result.stdout.splitlines() if line.startswith("USDB_TRACE_RESULT="))
            return json.loads(line.split("=", 1)[1])
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)

    def test_native_traces_keep_hashes_order_nested_calls_and_reverts(self):
        first, second = "0x" + "ab" * 32, "0x" + "cd" * 32
        sender, contract, child = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20
        root = {"type": "CALL", "from": sender, "to": contract, "value": "0x0",
                "gas": "0x10000", "gasUsed": "0x100", "input": "0x", "output": "0x"}
        nested = {**root, "from": contract, "to": child, "type": "STATICCALL", "gasUsed": "0x10"}
        result = self.probe({"transactions": [{"block_number": 69, "hash": first, "index": 0},
                                               {"block_number": 69, "hash": second, "index": 1}],
                             "traces": {first: {**root, "calls": [nested]},
                                        second: {**root, "error": "execution reverted"}}})
        self.assertCountEqual(result["requests"], [first, second])
        traces = result["decoded"]
        successful = sorted((t for t in traces if t["transaction_hash"] == first), key=lambda t: t["index"])
        self.assertEqual(len(successful), 2)
        self.assertEqual([t["trace_address"] for t in successful], [[], [0]])
        self.assertEqual([t["call_type"] for t in successful], ["call", "staticcall"])
        self.assertEqual([t["to_address_hash"] for t in successful], [contract, child])
        for trace in traces:
            self.assertEqual(trace["block_number"], 69)
            self.assertEqual(trace["transaction_index"], 0 if trace["transaction_hash"] == first else 1)
        reverted = [t for t in traces if t["transaction_hash"] == second]
        self.assertEqual(len(reverted), 1)
        self.assertEqual(reverted[0]["error"], "execution reverted")

    def test_empty_transaction_batch_does_not_invent_traces(self):
        result = self.probe({"transactions": [], "traces": {}})
        self.assertEqual(result, {"decoded": [], "requests": []})


if __name__ == "__main__":
    unittest.main()

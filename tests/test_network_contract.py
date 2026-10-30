#!/usr/bin/env python3
"""Verify the explorer consumes pinned interface data without a sibling node checkout."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "explorer"))
from network_contract import check_network


class NetworkContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.networks = self.repo / "explorer/networks"
        shutil.copytree(ROOT / "explorer/networks", self.networks)

    def test_contract_validates_with_only_vendored_data(self):
        contract = check_network(self.repo)
        self.assertEqual(contract["source"]["repository"], "buckyos/usdb")
        self.assertEqual(contract["rpc"]["profile_id"], "usdb-explorer-rpc:v1")
        self.assertEqual(contract["source"]["path"], "docker/networks/testnet-v1")
        identity = json.loads((self.networks / contract["catalog"]["file"]).read_text())
        self.assertEqual(identity["chain_id"], 202610030)
        self.assertEqual(identity["genesis_block_hash"], "0xb0b6ebc9a6c2e051855c2d61dff9a51635ea1db09c898a3c0126e27b9e779314")
        self.assertEqual(identity["btc_activation_registry_id"], "53b4bfed53b55a4accbd947d04d113a684f1af896d39d2d9bd984e07ad543f32")
        self.assertEqual(identity["btc_rules_scope"], "usdb-testnet-v1")
        self.assertFalse((self.networks / "usdb-testnet-v0.json").exists())
        self.assertFalse((self.repo / "docker").exists())

    def test_network_identity_edit_requires_an_explicit_contract_update(self):
        path = self.networks / "usdb-testnet-v1.json"
        value = json.loads(path.read_text())
        value["chain_id"] += 1
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "checksum"):
            check_network(self.repo)

    def test_mutable_source_and_unsupported_rpc_semantics_are_rejected(self):
        path = self.networks / "usdb-testnet-v1.contract.json"
        original = path.read_text()
        for kind in ("revision", "trace", "semantics"):
            value = json.loads(original)
            if kind == "revision":
                value["source"]["revision"] = "master"
            elif kind == "trace":
                value["rpc"]["trace_methods"] = []
            else:
                value["rpc"]["semantics"]["rewards_supply"] = "ethereum"
            path.write_text(json.dumps(value))
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                check_network(self.repo)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Exercise optional faucet deployment, preserved storage and secret-free operator commands."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "explorer"))
import faucet_cli as CLI
import public_config as CONFIG
import usdb_public as PUBLIC


class FaucetDeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "config.json"
        self.state = self.root / "deployment"
        self.config = CONFIG.read_json(PUBLIC.KIT / "config.example.json")
        self.save()

    def save(self):
        self.source.write_text(json.dumps(self.config))

    def enable(self):
        self.config["faucet"] = {"enabled": True, "claim_amount": "1", "daily_budget": "100"}
        self.save()
        return PUBLIC.prepare(self.source, self.state)

    def args(self, *options):
        return PUBLIC.parser().parse_args(["faucet", *options, "--state-dir", str(self.state)])

    def test_default_disabled_has_no_signer_or_wallet_volume(self):
        doc = PUBLIC.prepare(self.source, self.state)
        self.assertNotIn("faucet", doc["services"])
        self.assertNotIn("faucet-data", doc["volumes"])
        self.assertIn('"enabled":false', (self.state / "nginx.conf").read_text())
        with self.assertRaisesRegex(ValueError, "disabled"):
            PUBLIC.execute(self.args("status"), self.state)

    def test_enabled_signer_is_isolated_and_source_build_contains_both_programs(self):
        doc = self.enable()
        faucet = doc["services"]["faucet"]
        self.assertEqual(faucet["entrypoint"], ["/faucet"])
        self.assertEqual(faucet["volumes"], ["faucet-data:/data"])
        self.assertTrue(faucet["read_only"])
        self.assertNotIn("ports", faucet)
        self.assertEqual(set(faucet["networks"]), {"faucet", "rpc"})
        self.assertNotIn("faucet", doc["services"]["frontend"]["networks"])
        self.assertNotIn("faucet", doc["services"]["gateway"]["networks"])
        self.assertTrue(doc["networks"]["faucet"]["internal"])
        policy = json.loads(faucet["environment"]["FAUCET_CONFIG"])
        self.assertEqual(policy["read_url"], "http://rpc-relay:8080/read")
        self.assertEqual(policy["broadcast_url"], "http://rpc-relay:8080/broadcast")
        self.assertEqual(policy["cooldown_seconds"], 86400)
        nginx = (self.state / "nginx.conf").read_text()
        self.assertIn("X-USDB-Faucet-Proxy " + policy["proxy_token"], nginx)
        self.assertIn("X-Forwarded-For $remote_addr", nginx)
        self.assertNotIn("$http_x_forwarded_for", nginx)
        self.assertTrue((self.state / "build/gateway/cmd/faucet/main.go").is_file())
        self.assertTrue((self.state / "build/gateway/go.sum").is_file())
        PUBLIC.verify(self.state)

    def test_reprepare_preserves_volume_identity_and_proxy_token(self):
        before = self.enable()
        self.config["faucet"]["daily_budget"] = "200"
        self.save()
        with mock.patch.object(PUBLIC, "require_stopped"):
            after = PUBLIC.prepare(self.source, self.state, replace=True)
        self.assertEqual(before["volumes"]["faucet-data"], after["volumes"]["faucet-data"])
        self.assertEqual(before["services"]["faucet"]["volumes"], after["services"]["faucet"]["volumes"])
        old = json.loads(before["services"]["faucet"]["environment"]["FAUCET_CONFIG"])
        new = json.loads(after["services"]["faucet"]["environment"]["FAUCET_CONFIG"])
        self.assertEqual(old["proxy_token"], new["proxy_token"])
        self.assertEqual(new["daily_budget"], "200")

    def test_external_ingress_uses_only_loopback_and_overwrites_client_headers(self):
        self.config["rpc"] = {"mode": "external", "read_url": "http://rpc.internal:8545"}
        self.config["ingress"]["mode"] = "external"
        doc = self.enable()
        self.assertEqual(doc["services"]["faucet"]["ports"], ["127.0.0.1:28083:8080"])
        self.assertIn("faucet-outbound", doc["services"]["faucet"]["networks"])
        snippet = (self.state / "nginx.locations.conf").read_text()
        self.assertIn("http://127.0.0.1:28083", snippet)
        self.assertIn("X-Forwarded-For $remote_addr", snippet)
        self.config["ingress"]["faucet_port"] = self.config["ingress"]["http_port"]
        self.save()
        with self.assertRaisesRegex(ValueError, "distinct"):
            CONFIG.load_config(self.source, PUBLIC.KIT)

    def test_external_ingress_with_local_rpc_can_publish_its_loopback_port(self):
        self.config["ingress"]["mode"] = "external"
        doc = self.enable()
        self.assertIn("faucet-outbound", doc["services"]["faucet"]["networks"])
        self.assertIn("rpc", doc["services"]["faucet"]["networks"])
        self.assertFalse(doc["networks"]["faucet-outbound"].get("internal", False))

    def test_configuration_preserves_other_settings_and_rejects_invalid_amounts_atomically(self):
        previous = self.source.read_bytes()
        args = self.args("configure", "--config", str(self.source), "--enable", "--claim-amount", "1e18", "--daily-budget", "100")
        with self.assertRaises(ValueError):
            PUBLIC.execute(args, self.state)
        self.assertEqual(self.source.read_bytes(), previous)
        args.claim_amount = "1"
        PUBLIC.execute(args, self.state)
        result = CONFIG.read_json(self.source)
        self.assertEqual(result["deployment_id"], self.config["deployment_id"])
        self.assertEqual(result["rpc"]["read_url"], self.config["rpc"]["read_url"])
        self.assertTrue(result["faucet"]["enabled"])
        self.assertEqual(list(self.root.glob("config.json.backup-*"))[0].read_bytes(), previous)
        self.assertFalse(self.state.exists())

    def test_refill_key_uses_stdin_only_and_is_removed_from_child_environment(self):
        self.enable()
        key = "ab" * 32
        args = self.args("fund", "--amount", "10", "--request-id", "refill-1", "--miner-key-env", "FAUCET_TEST_KEY")
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            self.assertNotIn("FAUCET_TEST_KEY", os.environ)
            return subprocess.CompletedProcess(command, 0, '{"id":"f_refill-1","status":"pending"}', "")
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"FAUCET_TEST_KEY": key}), mock.patch.object(CLI.subprocess, "run", side_effect=run), contextlib.redirect_stdout(output):
            self.assertEqual(PUBLIC.execute(args, self.state), 0)
        self.assertEqual(calls[0][1]["input"], key)
        self.assertNotIn(key, str(calls[0][0]))
        self.assertNotIn(key, output.getvalue())
        self.assertIn("Funding recovery ID: refill-1", output.getvalue())
        self.assertIn("confirmation is tracked automatically", output.getvalue())
        for path in (self.source, self.state / "config.json", self.state / "compose.json"):
            self.assertNotIn(key, path.read_text())

    def test_retry_needs_no_key_and_command_errors_are_sanitized(self):
        self.enable()
        args = self.args("fund", "--retry", "--request-id", "refill-1")
        with mock.patch.object(CLI.getpass, "getpass", side_effect=AssertionError("unexpected prompt")), mock.patch.object(CLI.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, '{"id":"f_refill-1","transaction_hash":"0xabc"}', "raw upstream secret\nFaucet operation failed: BROADCAST_UNCERTAIN\n")
            with self.assertRaisesRegex(ValueError, "BROADCAST_UNCERTAIN") as raised:
                PUBLIC.execute(args, self.state)
            self.assertNotIn("secret", str(raised.exception))
            self.assertEqual(run.call_args.kwargs["input"], "")
            self.assertIn("retry", run.call_args.args[0])


    def test_pending_funding_identifies_previous_transfer_and_waits_for_auto_confirmation(self):
        self.enable()
        args = self.args("fund", "--amount", "10", "--request-id", "refill-2", "--miner-key-env", "FAUCET_TEST_KEY")
        previous = {"id": "refill-1", "status": "confirming", "transaction_hash": "0xabc"}
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"FAUCET_TEST_KEY": "ab" * 32}), mock.patch.object(CLI.subprocess, "run") as run, contextlib.redirect_stdout(output):
            run.return_value = subprocess.CompletedProcess([], 1, json.dumps(previous), "Faucet operation failed: FUNDING_PENDING\n")
            with self.assertRaisesRegex(ValueError, "this new request was not submitted") as raised:
                PUBLIC.execute(args, self.state)
        self.assertIn("automatic confirmation", str(raised.exception))
        self.assertIn("Previous funding", output.getvalue())
        self.assertIn('"id": "refill-1"', output.getvalue())
        self.assertIn("0xabc", output.getvalue())
        self.assertNotIn("Funding submitted", output.getvalue())

    def test_funding_failures_do_not_print_empty_receipts(self):
        self.enable()
        args = self.args("fund", "--retry", "--request-id", "missing-refill")
        for response in ('', '{"id":"","address":"","amount_atoms":"","created_at":0,"status":""}'):
            with self.subTest(response=response):
                output = io.StringIO()
                with mock.patch.object(CLI.subprocess, "run") as run, contextlib.redirect_stdout(output):
                    run.return_value = subprocess.CompletedProcess([], 1, response, "Faucet operation failed: FUNDING_NOT_FOUND\n")
                    with self.assertRaisesRegex(ValueError, "FUNDING_NOT_FOUND"):
                        PUBLIC.execute(args, self.state)
                self.assertNotIn('"id": ""', output.getvalue())
                self.assertNotIn("Funding submitted", output.getvalue())



    def test_status_history_limit_is_bounded_and_forwarded(self):
        self.enable()
        for limit in (1, 20, 50, 100):
            with self.subTest(limit=limit), mock.patch.object(CLI.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, '{"claims":[],"funding":[]}', "")
                with contextlib.redirect_stdout(io.StringIO()):
                    PUBLIC.execute(self.args("status", "--limit", str(limit)), self.state)
                command = run.call_args.args[0]
                if limit == 20:
                    self.assertEqual(command[-1], "status")
                else:
                    self.assertEqual(command[-3:], ["status", "--limit", str(limit)])
                self.assertEqual(run.call_args.kwargs["input"], "")
        self.assertEqual(self.args("status").limit, 20)
        for value in ("0", "-1", "101", "1.5", "all"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), mock.patch.object(CLI.subprocess, "run") as run:
                with self.assertRaises(SystemExit):
                    self.args("status", "--limit", value)
                run.assert_not_called()



    def test_directed_miner_transfer_keeps_key_off_arguments_and_config(self):
        self.enable()
        key = "ab" * 32
        address = "0x" + "11" * 20
        args = self.args("transfer", "--from", "miner", "--to", address, "--amount", "5", "--request-id", "manual-miner", "--miner-key-env", "FAUCET_TEST_KEY")
        def run(command, **kwargs):
            self.assertNotIn("FAUCET_TEST_KEY", os.environ)
            self.assertEqual(kwargs["input"], key)
            self.assertNotIn(key, str(command))
            self.assertEqual(command[-9:], ["transfer", "--from", "miner", "--to", address, "--amount", "5", "--request-id", "manual-miner"])
            return subprocess.CompletedProcess(command, 0, '{"id":"manual-miner","status":"pending"}', "")
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"FAUCET_TEST_KEY": key}), mock.patch.object(CLI.subprocess, "run", side_effect=run), contextlib.redirect_stdout(output):
            self.assertEqual(PUBLIC.execute(args, self.state), 0)
        self.assertNotIn(key, output.getvalue())
        self.assertIn("Transfer recovery ID: manual-miner", output.getvalue())
        self.assertIn("confirmation is tracked automatically", output.getvalue())

    def test_faucet_transfer_and_recovery_do_not_prompt_for_keys(self):
        self.enable()
        address = "0x" + "11" * 20
        for arguments in (("--from", "faucet", "--to", address, "--amount", "5"), ("--retry",)):
            args = self.args("transfer", *arguments, "--request-id", "manual-wallet")
            with mock.patch.object(CLI.getpass, "getpass", side_effect=AssertionError("unexpected key prompt")), mock.patch.object(CLI.subprocess, "run") as run, contextlib.redirect_stdout(io.StringIO()) as output:
                run.return_value = subprocess.CompletedProcess([], 0, '{"id":"manual-wallet","status":"queued"}', "")
                self.assertEqual(PUBLIC.execute(args, self.state), 0)
                self.assertEqual(run.call_args.kwargs["input"], "")
                command = run.call_args.args[0]
                if arguments == ("--retry",):
                    self.assertEqual(command[-4:], ["transfer", "--retry", "--request-id", "manual-wallet"])
                self.assertIn("Transfer queued", output.getvalue())

    def test_directed_transfer_rejects_ambiguous_or_invalid_arguments_before_docker(self):
        self.enable()
        address = "0x" + "11" * 20
        cases = [
            ("--to", address, "--amount", "1"),
            ("--from", "faucet", "--amount", "1"),
            ("--from", "faucet", "--to", "0x" + "00" * 20, "--amount", "1"),
            ("--from", "faucet", "--to", address, "--amount", "0"),
            ("--from", "faucet", "--to", address, "--amount", "1", "--miner-key-env", "UNUSED_KEY"),
            ("--retry", "--request-id", "manual", "--from", "faucet"),
        ]
        for options in cases:
            with self.subTest(options=options), mock.patch.object(CLI.subprocess, "run") as run, mock.patch.object(CLI.getpass, "getpass", side_effect=AssertionError("unexpected key prompt")):
                with self.assertRaises(ValueError):
                    PUBLIC.execute(self.args("transfer", *options), self.state)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

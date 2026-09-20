#!/usr/bin/env python3
"""Exercise the operator wizard without accessing Docker, real keys or service data."""
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "explorer"))
import public_config as CONFIG
import public_setup as SETUP
import usdb_public as PUBLIC


class Answers:
    """Accept defaults except for named prompts; bound retries to catch stuck input loops."""

    def __init__(self, **answers):
        self.answers = {key: list(value) for key, value in answers.items()}
        self.seen = []

    def __call__(self, prompt):
        self.seen.append(prompt)
        if len(self.seen) > 100:
            raise AssertionError("wizard did not finish")
        for key, values in self.answers.items():
            if prompt.startswith(key) and values:
                return values.pop(0)
        return ""


class PublicSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "operator config.json"
        self.state = self.root / "prepared deployment"
        self.args = PUBLIC.parser().parse_args(["setup", "--config", str(self.source), "--state-dir", str(self.state)])
        self.output = io.StringIO()

    def run_setup(self, answers=None):
        with mock.patch.object(PUBLIC, "docker", side_effect=AssertionError("setup must not operate Docker")):
            return SETUP.setup(self.args, self.state, kit=PUBLIC.KIT, input_fn=answers or Answers(), output=self.output)

    def config(self):
        return CONFIG.read_json(self.source)

    def initial(self, **updates):
        config = CONFIG.read_json(PUBLIC.KIT / "config.example.json")
        config.update(updates)
        PUBLIC.write_json(self.source, config)
        return config

    def test_first_setup_and_rerun_preserve_defaults_and_private_backups(self):
        self.assertEqual(self.run_setup(), 0)
        config = self.config()
        self.assertEqual(config["rpc"]["mode"], "local-node")
        self.assertEqual(config["ingress"]["bind_address"], "127.0.0.1")
        self.assertFalse(config["faucet"]["enabled"])
        self.assertFalse(self.state.exists())
        self.assertEqual(self.source.stat().st_mode & 0o777, 0o600)
        self.assertIn("prepare --config '", self.output.getvalue())
        self.assertNotIn("prepare --replace", self.output.getvalue())
        before = self.source.read_bytes()
        self.run_setup()
        self.assertEqual(self.source.read_bytes(), before)
        backup, = self.root.glob("operator config.json.backup-*")
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_ipv6_setup_preserves_existing_listener_and_clears_it_for_external_ingress(self):
        self.run_setup(Answers(**{"Visitor URL": ["http://explorer.example.test:28080"], "Also publish Nginx over IPv6": ["y"]}))
        self.assertEqual(self.config()["ingress"]["bind_address_ipv6"], "::")
        self.assertIn("[::]:28080", self.output.getvalue())
        self.run_setup()
        self.assertEqual(self.config()["ingress"]["bind_address_ipv6"], "::")
        self.run_setup(Answers(**{"Also publish Nginx over IPv6": ["n"]}))
        self.assertNotIn("bind_address_ipv6", self.config()["ingress"])
        self.run_setup(Answers(**{"Also publish Nginx over IPv6": ["y"], "Visitor URL": ["http://localhost:28080"]}))
        self.assertEqual(self.config()["ingress"]["bind_address_ipv6"], "::1")
        self.run_setup(Answers(**{"Ingress (": ["external"]}))
        self.assertNotIn("bind_address_ipv6", self.config()["ingress"])

    def test_public_nat_url_is_independent_of_listening_port(self):
        answers = Answers(**{"Visitor URL": ["http://192.0.2.10:38080/"], "Nginx local HTTP port": ["0", "65536", "28080"]})
        self.run_setup(answers)
        ingress = self.config()["ingress"]
        self.assertEqual(ingress["explorer_url"], "http://192.0.2.10:38080")
        self.assertEqual((ingress["bind_address"], ingress["http_port"]), ("0.0.0.0", 28080))
        self.assertEqual(ingress["exposure"], "public")
        self.assertIn("Enter an integer", self.output.getvalue())

    def test_bundled_https_requires_existing_certs_and_keeps_http_redirect_port(self):
        certs = self.root / "certificates"
        certs.mkdir()
        for name in ("fullchain.pem", "privkey.pem"):
            (certs / name).write_text("test fixture, no private material")
        self.run_setup(Answers(**{"Visitor URL": ["https://explorer.example.test"],
                                 "Certificate directory": [str(self.root / "missing"), str(certs)]}))
        ingress = self.config()["ingress"]
        self.assertEqual(ingress["tls"]["certificate_dir"], str(certs))
        self.assertEqual((ingress["http_port"], ingress["https_port"]), (28080, 28443))
        self.assertIn("no external symlinks", self.output.getvalue())
        self.run_setup(Answers(**{"Visitor URL": ["http://explorer.example.test:28080"]}))
        self.assertNotIn("tls", self.config()["ingress"])

    def test_external_https_faucet_and_private_rpc_without_local_certificates(self):
        self.run_setup(Answers(**{
            "Node connection": ["external"], "Read RPC URL": ["http://rpc.internal:8545"],
            "Tracing RPC URL": ["http://trace.internal:8545"], "Ingress (": ["external"],
            "Visitor URL": ["https://explorer.example.test"], "Enable testnet faucet": ["y"],
            "Customize cooldown": ["y"], "Address cooldown": ["3600"], "Claims per IP": ["20"],
        }))
        config = self.config()
        self.assertEqual(config["rpc"]["broadcast_url"], "http://rpc.internal:8545")
        self.assertEqual(config["rpc"]["trace_url"], "http://trace.internal:8545")
        self.assertNotIn("indexer_url", config["rpc"])
        self.assertEqual(config["ingress"]["bind_address"], "127.0.0.1")
        self.assertEqual(config["ingress"]["faucet_port"], 28083)
        self.assertNotIn("tls", config["ingress"])
        self.assertEqual(config["faucet"]["cooldown_seconds"], 3600)
        self.assertEqual(config["faucet"]["ip_daily_claims"], 20)
        self.assertIn("nginx.locations.conf", self.output.getvalue())
        self.assertIn("faucet fund --amount <USDB>", self.output.getvalue())
        self.assertNotIn("rpc.internal", self.output.getvalue())

    def test_existing_identity_samples_resources_and_prepared_credentials_are_preserved(self):
        config = self.initial(deployment_id="existing-explorer", resources={"memory_budget_gib": 8, "other_services_memory_gib": 30})
        config["rpc"].update(trace_url="http://127.0.0.1:9854", historical_block=12, transaction="0x" + "ab" * 32,
                             reference_url="https://private.internal/rpc")
        config["faucet"] = {"enabled": True, "claim_amount": "2", "daily_budget": "200", "ip_daily_claims": 15}
        PUBLIC.write_json(self.source, config)
        PUBLIC.prepare(self.source, self.state)
        before = PUBLIC.file_hashes(self.state)
        answers = Answers(**{"Visitor URL": ["http://192.0.2.2:28080"]})
        self.run_setup(answers)
        self.assertEqual(PUBLIC.file_hashes(self.state), before)
        current = self.config()
        for key in ("deployment_id", "network", "resources"):
            self.assertEqual(current[key], config[key])
        for key, value in config["rpc"].items():
            self.assertEqual(current["rpc"][key], value)
        self.assertEqual(current["faucet"]["ip_daily_claims"], 15)
        self.assertIn("prepare --replace", self.output.getvalue())
        self.assertNotIn("private.internal", str(answers.seen) + self.output.getvalue())

    def test_missing_source_recovers_settings_from_verified_deployment(self):
        self.initial(deployment_id="recovered-explorer")
        PUBLIC.prepare(self.source, self.state)
        self.source.unlink()
        self.run_setup()
        self.assertEqual(self.config()["deployment_id"], "recovered-explorer")

    def test_invalid_budget_can_be_corrected_without_early_writes(self):
        self.run_setup(Answers(**{"Enable testnet faucet": ["y"], "Daily USDB budget": ["1", "100"],
                                 "USDB per claim": ["1e18", "1"]}))
        self.assertIn("daily_budget must cover", self.output.getvalue())
        self.assertIn("positive decimal strings", self.output.getvalue())
        self.assertEqual(self.config()["faucet"]["daily_budget"], "100")
        self.assertEqual(list(self.root.glob("*.backup-*")), [])

    def test_port_collision_does_not_save_invalid_configuration(self):
        self.initial()
        before = self.source.read_bytes()
        self.run_setup(Answers(**{"Ingress (": ["external"], "Local gateway port": ["28080"], "Review settings again": ["n"]}))
        self.assertEqual(self.source.read_bytes(), before)
        self.assertIn("ports must be distinct", self.output.getvalue())
        self.assertEqual(list(self.root.glob("*.backup-*")), [])

    def test_decline_eof_and_interrupt_leave_source_unchanged(self):
        self.initial()
        before = self.source.read_bytes()
        for error in (EOFError(), KeyboardInterrupt()):
            def cancel(prompt):
                raise error
            self.assertEqual(self.run_setup(cancel), 130)
        self.assertEqual(self.run_setup(Answers(**{"Save this Explorer": ["n"]})), 0)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.backup-*")), [])
        self.assertFalse(self.state.exists())

    def test_disable_faucet_keeps_saved_policy_and_deployment_files(self):
        self.initial(faucet={"enabled": True, "claim_amount": "2", "daily_budget": "100"})
        PUBLIC.prepare(self.source, self.state)
        before = PUBLIC.file_hashes(self.state)
        self.run_setup(Answers(**{"Enable testnet faucet": ["n"]}))
        self.assertEqual(self.config()["faucet"], {"enabled": False, "claim_amount": "2", "daily_budget": "100"})
        self.assertEqual(PUBLIC.file_hashes(self.state), before)

    def test_source_cannot_overwrite_prepared_files_or_change_existing_identity(self):
        self.initial()
        PUBLIC.prepare(self.source, self.state)
        before = PUBLIC.file_hashes(self.state)
        self.args.config = self.state / "config.json"
        with self.assertRaisesRegex(ValueError, "outside the prepared"):
            self.run_setup()
        self.args.config = self.source
        self.initial(deployment_id="another-explorer")
        with self.assertRaisesRegex(ValueError, "preserve the existing"):
            self.run_setup()
        self.assertEqual(PUBLIC.file_hashes(self.state), before)

    def test_external_source_edit_during_review_is_not_overwritten(self):
        self.initial()
        edited = {**self.config(), "deployment_id": "edited-explorer"}
        def answer(prompt):
            if prompt.startswith("Save this Explorer"):
                PUBLIC.write_json(self.source, edited)
            return ""
        with self.assertRaisesRegex(ValueError, "changed during setup"):
            self.run_setup(answer)
        self.assertEqual(self.config(), edited)

    def test_same_mode_retains_explicit_resource_reservation(self):
        self.initial(resources={"memory_budget_gib": 8, "other_services_memory_gib": 0})
        self.run_setup(Answers(**{"Save this Explorer": ["yes"]}))
        self.assertEqual(self.config()["resources"], {"memory_budget_gib": 8, "other_services_memory_gib": 0})

    def test_local_custom_rpc_validation_and_switch_from_external(self):
        self.initial(rpc={"mode": "external", "read_url": "http://private.internal:8545", "historical_block": 2})
        answers = Answers(**{"Node connection": ["local-node"], "Change private RPC": ["y"],
                             "Read RPC URL": ["http://private.internal:9999", "http://127.0.0.1:9999"]})
        self.run_setup(answers)
        rpc = self.config()["rpc"]
        self.assertEqual(rpc["historical_block"], 2)
        self.assertEqual(rpc["trace_url"], "http://127.0.0.1:9999")
        self.assertEqual(rpc["indexer_url"], "http://127.0.0.1:28020")
        self.assertNotIn("private.internal", str(answers.seen) + self.output.getvalue())

    def test_cli_requires_terminal_and_holds_source_and_deployment_locks(self):
        args = ["setup", "--config", str(self.source), "--state-dir", str(self.state)]
        with mock.patch.object(sys.stdin, "isatty", return_value=False), mock.patch("sys.stderr", new=io.StringIO()) as error:
            self.assertEqual(PUBLIC.main(args), 1)
            self.assertIn("interactive terminal", error.getvalue())
        self.assertFalse(self.source.exists())
        with mock.patch.object(sys.stdin, "isatty", return_value=True), mock.patch.object(sys.stdout, "isatty", return_value=True), mock.patch.object(PUBLIC, "execute", return_value=0) as execute:
            with PUBLIC.operation_lock(self.state), mock.patch("sys.stderr", new=io.StringIO()) as error:
                self.assertEqual(PUBLIC.main(args), 1)
                self.assertIn("another operation", error.getvalue())
                execute.assert_not_called()
            self.assertEqual(PUBLIC.main(args), 0)
            execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()

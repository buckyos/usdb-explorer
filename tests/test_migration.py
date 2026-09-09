#!/usr/bin/env python3
"""Upgrade the actual legacy installer in an isolated home without changing deployment data."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "explorer"))
import package_release as PACKAGE
from common.public_installer import fake_release_curl
from common.public_services import fake_docker

FIXTURE = ROOT / "tests/fixtures/legacy-v0.1.0"


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assets = self.root / "assets"
        shutil.copytree(FIXTURE, self.assets)
        self.stub_bin = self.root / "stub-bin"
        self.stub_bin.mkdir()
        fake_release_curl(self.stub_bin / "curl", self.assets)
        fake_docker(self.stub_bin / "docker", self.root / "docker.log")
        self.env = {**os.environ, "PATH": str(self.stub_bin) + os.pathsep + os.environ["PATH"],
                    "PYTHONDONTWRITEBYTECODE": "1"}
        self.operator_root = self.root / "operator"
        self.operator_root.mkdir()
        self.commands = self.operator_root / ".local/bin"
        self.storage = self.operator_root / ".local/share/usdb-public"
        self.config = self.operator_root / ".config/usdb-public/config.json"
        self.state = self.operator_root / ".config/usdb-public/default"
        self.install_options = ["--install-root", str(self.storage), "--bin-dir", str(self.commands),
                                "--config-file", str(self.config)]

    def run_command(self, *arguments):
        result = subprocess.run(arguments, env=self.env, cwd=self.root, text=True,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_legacy_asset_fixture_matches_recorded_release_hashes(self):
        for name, digest in {
            "install-usdb-public-v0.1.0.sh": "af145ff3de47356c9643660af6f0359c39c65c6c5c785b6c8897a260d3c6d97e",
            "usdb-public-v0.1.0.tar.gz": "3ed799cd5e0592f7e55f66fd23f65d81c19f97fd35107e8bada679d9c4975c49",
        }.items():
            self.assertEqual(hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest(), digest)

    def test_real_legacy_install_upgrades_with_config_credentials_volumes_and_alias_preserved(self):
        self.run_command("bash", str(self.assets / "install-usdb-public-v0.1.0.sh"), *self.install_options)
        self.run_command(str(self.commands / "usdb-public"), "prepare", "--config", str(self.config), "--state-dir", str(self.state))
        before_config = self.config.read_bytes()
        credentials = (self.state / "credentials.json").read_bytes()
        before_compose = json.loads((self.state / "compose.json").read_bytes())
        marker = self.root / "existing-database-marker"
        marker.write_bytes(b"existing operator data")
        PACKAGE.package(ROOT, self.assets, "0.2.0", "ghcr.io/buckyos/usdb-explorer-gateway@sha256:" + "ab" * 32)
        self.run_command("bash", str(self.assets / "install-usdb-explorer-v0.2.0.sh"), *self.install_options)
        self.assertEqual(os.readlink(self.storage / "current"), "releases/usdb-explorer-v0.2.0")
        self.assertTrue((self.storage / "releases/usdb-public-v0.1.0").is_dir())
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual((self.state / "credentials.json").read_bytes(), credentials)
        self.run_command(str(self.commands / "usdb-public"), "--help")
        self.run_command(str(self.commands / "usdb-explorer"), "prepare", "--replace", "--config", str(self.config), "--state-dir", str(self.state))
        after_compose = json.loads((self.state / "compose.json").read_bytes())
        self.assertEqual(after_compose["name"], before_compose["name"])
        self.assertEqual(after_compose["volumes"], before_compose["volumes"])
        self.assertEqual((self.state / "credentials.json").read_bytes(), credentials)
        self.assertEqual(marker.read_bytes(), b"existing operator data")
        calls = [json.loads(line) for line in (self.root / "docker.log").read_text().splitlines()]
        self.assertTrue(all(call[0] in {"ps", "inspect"} for call in calls))


if __name__ == "__main__":
    unittest.main()

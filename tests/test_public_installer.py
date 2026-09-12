#!/usr/bin/env python3
"""Verify release-bound one-line install, upgrades, preservation and fail-closed extraction."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "explorer"))
import package_release as RELEASE
import install_release as INSTALL
import release_installer as RENDER
from common.public_installer import fake_release_curl


class PublicInstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assets = self.root / "assets"
        self.storage = self.root / "user files" / "install"
        self.commands = self.root / "user files" / "bin"
        self.config = self.root / "private" / "config.json"
        self.stub_bin = self.root / "stub-bin"
        self.stub_bin.mkdir()
        fake_release_curl(self.stub_bin / "curl", self.assets)
        self.env = {**os.environ, "PATH": str(self.stub_bin) + os.pathsep + os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"}
        self.archive = self.make_release("0.1.0")

    def make_release(self, version):
        image = "ghcr.io/buckyos/usdb-explorer-gateway@sha256:" + "ab" * 32
        return RELEASE.package(ROOT, self.assets, version, image)

    def run_installer(self, version="0.1.0", *, extra=(), process_substitution=False):
        script = self.assets / ("install-usdb-explorer-v" + version + ".sh")
        options = ["--install-root", str(self.storage), "--bin-dir", str(self.commands), "--config-file", str(self.config), *extra]
        if process_substitution:
            url = "https://github.com/buckyos/usdb-explorer/releases/download/v" + version + "/" + script.name
            command = ["bash", "-c", 'bash <(curl -fsSL "$1") "${@:2}"', "installer-test", url, *options]
        else:
            command = ["bash", str(script), *options]
        return subprocess.run(command, cwd=self.root, env=self.env, text=True, capture_output=True, timeout=30)

    def test_one_line_install_creates_a_working_command_and_private_config(self):
        result = self.run_installer(process_substitution=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.readlink(self.storage / "current"), "releases/usdb-explorer-v0.1.0")
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.config.read_text())["network"], "usdb-testnet-v0")
        command = subprocess.run([str(self.commands / "usdb-public"), "prepare", "--help"], env=self.env,
                                 text=True, capture_output=True, timeout=10)
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertIn("~/.config/usdb-public/config.json", "".join(command.stdout.split()))
        self.assertFalse((self.root / "node.env").exists())

    def test_installed_command_can_configure_and_prepare_same_host_http_ingress(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        initial = json.loads(self.config.read_text())
        self.assertEqual(initial["rpc"]["mode"], "local-node")
        self.assertEqual(initial["rpc"]["read_url"], "http://127.0.0.1:8545")
        self.assertEqual(initial["ingress"]["mode"], "bundled")
        state = self.root / "state"
        commands = [["configure", "--local-node", "--config", str(self.config),
                     "--explorer-url", "http://192.0.2.10:38080", "--http-port", "28080"],
                    ["prepare", "--config", str(self.config), "--state-dir", str(state)]]
        for arguments in commands:
            command = subprocess.run([str(self.commands / "usdb-explorer"), *arguments], env=self.env,
                                     text=True, capture_output=True, timeout=10)
            self.assertEqual(command.returncode, 0, command.stderr)
        document = json.loads((state / "compose.json").read_text())
        self.assertEqual(document["services"]["proxy"]["ports"], ["0.0.0.0:28080:8080"])
        self.assertNotIn("build", document["services"]["gateway"])
        self.assertTrue((state / "rpc-host.conf").is_file())

    def test_reinstall_and_upgrade_preserve_operator_config_and_deployment_data(self):
        self.assertEqual(self.run_installer().returncode, 0)
        self.config.write_text('{"operator_settings":"keep exactly"}\n')
        state = self.root / "private/default/credentials.json"
        state.parent.mkdir()
        state.write_text("saved deployment credential fixture")
        self.assertEqual(self.run_installer().returncode, 0)
        self.make_release("0.2.0")
        result = self.run_installer("0.2.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.readlink(self.storage / "current"), "releases/usdb-explorer-v0.2.0")
        self.assertEqual(self.config.read_text(), '{"operator_settings":"keep exactly"}\n')
        self.assertEqual(state.read_text(), "saved deployment credential fixture")
        self.assertTrue((self.storage / "releases/usdb-explorer-v0.1.0").is_dir())

    def test_corrupt_download_cannot_switch_existing_release(self):
        self.assertEqual(self.run_installer().returncode, 0)
        next_archive = self.make_release("0.2.0")
        next_archive.write_bytes(b"corrupt download")
        before = self.config.read_bytes()
        result = self.run_installer("0.2.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(os.readlink(self.storage / "current"), "releases/usdb-explorer-v0.1.0")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.storage / "releases/usdb-explorer-v0.2.0").exists())

    def test_installed_release_tampering_is_not_silently_overwritten(self):
        self.assertEqual(self.run_installer().returncode, 0)
        target = self.storage / "releases/usdb-explorer-v0.1.0/usdb_public.py"
        target.write_text("operator change")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("file changed", result.stderr)
        self.assertEqual(target.read_text(), "operator change")

    def test_installer_pins_cannot_be_overridden_by_user_arguments(self):
        result = self.run_installer(extra=("--release-id", "usdb-explorer-v9.9.9", "--expected-sha256", "0" * 64,
                                           "--archive", str(self.root / "missing")))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.readlink(self.storage / "current"), "releases/usdb-explorer-v0.1.0")

    def test_archive_traversal_and_links_are_rejected_without_external_writes(self):
        for index, (name, kind) in enumerate((("usdb-explorer-v0.1.0/../../outside", tarfile.REGTYPE),
                                             ("usdb-explorer-v0.1.0/linked", tarfile.SYMTYPE),
                                             ("usdb-explorer-v0.1.0/hardlinked", tarfile.LNKTYPE),
                                             ("other-release/file", tarfile.REGTYPE))):
            archive = self.root / f"unsafe-{index}.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                entry = tarfile.TarInfo(name)
                entry.type = kind
                if kind == tarfile.REGTYPE:
                    entry.size = 4
                    tar.addfile(entry, io.BytesIO(b"test"))
                else:
                    entry.linkname = "/tmp/outside"
                    tar.addfile(entry)
            with self.assertRaisesRegex(ValueError, "unsafe"):
                INSTALL.install(archive, "usdb-explorer-v0.1.0", INSTALL.sha256(archive), self.storage, self.commands, self.config)
        self.assertFalse(self.config.exists())
        self.assertFalse((self.root / "outside").exists())
        self.assertFalse((self.storage / "current").exists())

    def test_wrong_manifest_version_cannot_be_installed_even_with_matching_archive_hash(self):
        archive = self.root / "wrong-version.tar.gz"
        body = json.dumps({"schema_version": "usdb-public-release:v1", "version": "9.9.9", "files": {}}).encode()
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("usdb-explorer-v0.1.0/release.json")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
        with self.assertRaisesRegex(ValueError, "manifest identity"):
            INSTALL.install(archive, "usdb-explorer-v0.1.0", INSTALL.sha256(archive), self.storage, self.commands, self.config)

    def test_existing_unmanaged_command_is_preserved(self):
        self.commands.mkdir(parents=True)
        command = self.commands / "usdb-public"
        command.write_text("another managed installation")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(command.read_text(), "another managed installation")
        self.assertFalse(self.config.exists())

    def test_existing_unmanaged_explorer_alias_is_preserved(self):
        self.commands.mkdir(parents=True)
        alias = self.commands / "usdb-explorer"
        alias.write_text("unmanaged explorer command")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(alias.read_text(), "unmanaged explorer command")
        self.assertFalse(self.config.exists())

    def test_alias_activation_failure_restores_the_previous_release(self):
        self.assertEqual(self.run_installer().returncode, 0)
        archive = self.make_release("0.2.0")
        replace = os.replace

        def fail_alias(source, destination):
            if destination == self.commands / "usdb-explorer":
                raise OSError("fixture alias activation failure")
            return replace(source, destination)

        with patch.object(INSTALL.os, "replace", side_effect=fail_alias):
            with self.assertRaisesRegex(OSError, "alias activation"):
                INSTALL.install(archive, "usdb-explorer-v0.2.0", INSTALL.sha256(archive),
                                self.storage, self.commands, self.config)
        self.assertEqual(os.readlink(self.storage / "current"), "releases/usdb-explorer-v0.1.0")
        for command in ("usdb-public", "usdb-explorer"):
            self.assertTrue((self.commands / command).resolve().is_file())

    def test_generated_script_help_and_asset_checksums(self):
        script = self.assets / "install-usdb-explorer-v0.1.0.sh"
        subprocess.run(["bash", "-n", str(script)], check=True)
        # Help must work without downloading an archive.
        self.archive.unlink()
        result = subprocess.run(["bash", str(script), "--help"], env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--install-root", result.stdout)
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        self.assertTrue(script.with_name(script.name + ".sha256").read_text().startswith(digest + "  "))
        self.assertIn("--proto-redir '=https'", script.read_text())


if __name__ == "__main__":
    unittest.main()

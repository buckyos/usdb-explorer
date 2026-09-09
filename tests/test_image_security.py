#!/usr/bin/env python3
"""Verify advisory findings never weaken immutable identity or evidence checks."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import image_security as SECURITY
from common.image_security import trivy_report, trivy_sarif

REVISION = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
GATEWAY = "ghcr.io/buckyos/usdb-explorer-gateway@sha256:" + "ab" * 32


class ImageSecurityTests(unittest.TestCase):
    def identity(self, name="gateway", mode="report-only"):
        reference = GATEWAY if name == "gateway" else SECURITY.image_lock(ROOT / "explorer")["images"][name]["reference"]
        return SECURITY.scan_input(ROOT, name, reference, REVISION, mode)

    def test_complete_plan_uses_pinned_lock_and_testnet_default(self):
        plan = SECURITY.scan_plan(ROOT, GATEWAY)
        self.assertEqual({item["name"] for item in plan["include"]},
                         {"gateway", "backend", "frontend", "postgres", "redis", "nginx", "go"})
        for item in plan["include"]:
            self.assertEqual(item["enforcement"], "report-only")
            self.identity(item["name"])
        strict = SECURITY.scan_plan(ROOT, GATEWAY, "strict")
        self.assertTrue(all(item["enforcement"] == "strict" for item in strict["include"]))

    def test_mutable_or_foreign_gateway_and_wrong_source_are_rejected(self):
        for reference in (GATEWAY.replace("@sha256:", ":"), GATEWAY.replace("buckyos", "other")):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                SECURITY.scan_plan(ROOT, reference)
        with self.assertRaisesRegex(ValueError, "source revision"):
            SECURITY.scan_input(ROOT, "gateway", GATEWAY, "f" * 40, "report-only")
        with self.assertRaisesRegex(ValueError, "selected source lock"):
            SECURITY.scan_input(ROOT, "redis", GATEWAY, REVISION, "report-only")

    def test_mainnet_cannot_inherit_testnet_report_only(self):
        self.assertEqual(SECURITY.security_enforcement("usdb-mainnet-v1"), "strict")
        for network, requested in (("usdb-mainnet-v1", "report-only"), ("unknown", None), ("usdb-testnet-v0", "disabled")):
            with self.subTest(network=network), self.assertRaises(ValueError):
                SECURITY.security_enforcement(network, requested)

    def test_uncommitted_lock_cannot_be_attributed_to_a_published_source_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "source"
            subprocess.run(["git", "clone", "--quiet", "--local", "--no-hardlinks", str(ROOT), str(repo)], check=True)
            lock_path = repo / "explorer/assets/images.lock.json"
            value = json.loads(lock_path.read_text())
            value["images"]["redis"]["reference"] = value["images"]["redis"]["reference"].split("@")[0] + "@sha256:" + "e" * 64
            lock_path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "inputs differ from the selected source commit"):
                SECURITY.scan_input(repo, "redis", value["images"]["redis"]["reference"], REVISION, "report-only")

    def test_same_findings_are_recorded_in_both_modes_without_acceptance(self):
        for mode in ("report-only", "strict"):
            with self.subTest(mode=mode):
                decision = SECURITY.evaluate(trivy_report(GATEWAY, REVISION), self.identity(mode=mode))
                self.assertEqual(decision["accepted_count"], 0)
                self.assertEqual(decision["unresolved_count"], 1)
                self.assertEqual(decision["raw_counts"]["HIGH"], 1)
                self.assertEqual(decision["review_result"], "findings")
                self.assertEqual(decision["result"], "pass" if mode == "report-only" else "fail")

    def test_wrong_digest_platform_revision_or_missing_coverage_block_report_only(self):
        original = trivy_report(GATEWAY, REVISION)
        changed = []
        value = deepcopy(original)
        value["ArtifactName"] = GATEWAY.replace("ab", "cd")
        changed.append(value)
        value = deepcopy(original)
        value["Metadata"]["RepoDigests"] = []
        changed.append(value)
        value = deepcopy(original)
        value["Metadata"]["ImageConfig"]["architecture"] = "arm64"
        changed.append(value)
        value = deepcopy(original)
        value["Metadata"]["ImageConfig"]["config"]["Labels"] = {}
        changed.append(value)
        value = deepcopy(original)
        value["Results"][0]["Target"] = "unrelated-binary"
        changed.append(value)
        value = deepcopy(original)
        value["Results"] = []
        changed.append(value)
        for report in changed:
            with self.subTest(report=report), self.assertRaises(ValueError):
                SECURITY.evaluate(report, self.identity())

    def test_third_party_digest_does_not_claim_explorer_build_provenance(self):
        identity = self.identity("redis")
        report = trivy_report(identity["image_reference"], "upstream-commit", "redis")
        report["ArtifactName"] = report["ArtifactName"].removeprefix("docker.io/library/")
        report["Metadata"]["RepoDigests"] = [report["ArtifactName"]]
        self.assertIsNone(identity["image_source_revision"])
        self.assertEqual(SECURITY.evaluate(report, identity)["result"], "pass")

    def test_malformed_reports_and_unknown_severities_are_errors_in_both_modes(self):
        for mode in ("report-only", "strict"):
            for key, value in (("Severity", "hidden"), ("PkgName", ""), ("InstalledVersion", None)):
                report = trivy_report(GATEWAY, REVISION)
                report["Results"][0]["Vulnerabilities"][0][key] = value
                with self.subTest(mode=mode, key=key), self.assertRaises(ValueError):
                    SECURITY.evaluate(report, self.identity(mode=mode))

    def test_evidence_precedes_strict_failure_and_detects_later_tampering(self):
        for mode in ("strict", "report-only"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                identity = self.identity(mode=mode)
                for name, value in (("scan-input.json", identity), ("trivy-image.json", trivy_report(GATEWAY, REVISION)),
                                    ("trivy-image.sarif", trivy_sarif())):
                    (root / name).write_text(json.dumps(value))
                metadata = SECURITY.write_evidence(root, identity, "Version: 0.74.0")
                self.assertEqual(metadata["image_reference"], GATEWAY)
                self.assertTrue((root / "SHA256SUMS").exists())
                command = [sys.executable, "-B", str(ROOT / "scripts/image_security.py"), "enforce", "--directory", str(root)]
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, int(mode == "strict"), result.stderr)
                (root / "trivy-image.json").write_text("{}")
                self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)

    def test_corrupt_sarif_never_produces_success_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "trivy-image.json").write_text(json.dumps(trivy_report(GATEWAY, REVISION)))
            (root / "trivy-image.sarif").write_text("{}")
            with self.assertRaisesRegex(ValueError, "SARIF"):
                SECURITY.write_evidence(root, self.identity(), "Version: 0.74.0")
            self.assertFalse((root / "metadata.json").exists())
            sarif = trivy_sarif()
            sarif["runs"][0]["results"] = []
            (root / "trivy-image.sarif").write_text(json.dumps(sarif))
            with self.assertRaisesRegex(ValueError, "SARIF High/Critical results"):
                SECURITY.write_evidence(root, self.identity(), "Version: 0.74.0")

    def test_cli_invalid_input_never_emits_a_scan_matrix(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/image_security.py"), "plan",
                                     "--gateway-image", "ghcr.io/buckyos/usdb-explorer-gateway:latest"],
                                    env={**os.environ, "GITHUB_OUTPUT": str(output)}, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

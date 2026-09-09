#!/usr/bin/env python3
"""Verify explorer tag preparation stays inside its own release boundary."""
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import prepare_release as PREPARE


class PrepareReleaseTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.dirty = False
        self.failed_push = False

    def git(self, repo, *args):
        self.assertEqual(repo, ROOT)
        self.calls.append(args)
        if args[:3] == ("remote", "get-url", "origin"):
            return "https://github.com/buckyos/usdb-explorer.git"
        if args[0] == "status":
            return " M changed.py" if self.dirty else ""
        if args[0] == "branch":
            return "main"
        if args[0] == "rev-parse":
            return "a" * 40
        if args[0] == "push" and self.failed_push:
            raise subprocess.CalledProcessError(1, ["git", *args])
        return ""

    def run_prepare(self, **options):
        with patch.object(PREPARE, "git", side_effect=self.git):
            PREPARE.prepare(ROOT, "0.2.0", **options)

    def test_default_preflight_creates_no_tag_or_commit(self):
        self.run_prepare()
        self.assertFalse(any(args[0] in {"push", "commit"} or args[:2] == ("tag", "-a") for args in self.calls))

    def test_annotated_explorer_tag_and_exact_push_are_explicit(self):
        self.run_prepare(create=True, push=True)
        self.assertIn(("push", "origin", "refs/tags/v0.2.0"), self.calls)
        self.assertTrue(any(args[:3] == ("tag", "-a", "v0.2.0") for args in self.calls))

    def test_dirty_source_cannot_create_a_tag(self):
        self.dirty = True
        with self.assertRaisesRegex(ValueError, "clean explorer main"):
            self.run_prepare(create=True)
        self.assertFalse(any(args[:2] == ("tag", "-a") for args in self.calls))

    def test_failed_push_preserves_created_tag_with_resume_instruction(self):
        self.failed_push = True
        with self.assertRaisesRegex(ValueError, "preserve it and retry git push origin refs/tags/v0.2.0"):
            self.run_prepare(create=True, push=True)
        self.assertFalse(any(args[:2] == ("tag", "-d") for args in self.calls))

    def test_node_and_old_public_release_ids_are_rejected(self):
        for version in ("usdb-testnet-v0-r20", "usdb-public-v0.1.0", "v0.2.0", "0.2.0;id"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                PREPARE.prepare(ROOT, version)


if __name__ == "__main__":
    unittest.main()

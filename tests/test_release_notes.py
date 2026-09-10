#!/usr/bin/env python3
"""Exercise release boundaries, committed fragments, coverage and upgrade evidence."""
from copy import deepcopy
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "explorer")]
import release_notes as NOTES
import package_release as PACKAGE
import publish_release as PUBLISH
from common.public_release import PublicReleaseAPI, make_public_release

GATEWAY = "ghcr.io/buckyos/usdb-explorer-gateway@sha256:" + "ab" * 32


class ReleaseNotesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo, self.assets = make_public_release(self.root, ROOT, PACKAGE, structured_notes=True)
        self.previous = NOTES.tag_identity(self.repo, "v0.1.0")

    def git(self, *args):
        return NOTES.git(self.repo, *args).strip()

    def fragment(self, change_id="explorer-fixture-change"):
        value = {"schema_version": NOTES.FRAGMENT_SCHEMA_VERSION, "change_id": change_id, "type": "fixed",
                 "scopes": ["gateway"], "summary": "Fix public RPC behavior", "details": ["Preserve upstream responses."],
                 "operator_actions": [], "compatibility": {key: False for key in NOTES.COMPATIBILITY_KEYS}, "references": []}
        path = self.repo / NOTES.FRAGMENT_PATH_PREFIX / (change_id + ".json")
        path.write_bytes(NOTES.canonical_json(value))
        return path, value

    def commit(self, message="Update fixture\n\nRelease-Note: none"):
        self.git("add", ".")
        self.git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", message)

    def release(self, tag="v0.2.0", *, previous=True):
        self.git("-c", "tag.gpgsign=false", "tag", "-a", tag, "-m", "Freeze fixture")
        return NOTES.build_changes(self.repo, tag, GATEWAY, previous=self.previous if previous else None)

    def test_fragment_schema_matches_usdb_fields_and_rejects_invalid_values(self):
        _, original = self.fragment()
        for key, value in (("unknown", True), ("type", "feature"), ("scopes", ["consensus"]),
                           ("summary", ""), ("details", []), ("references", ["http://example.invalid"]),
                           ("change_id", "v0.2.0")):
            invalid = deepcopy(original)
            invalid[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                NOTES.validate_fragment(invalid, "fixture")
        invalid = deepcopy(original)
        invalid["compatibility"]["restart_required"] = "false"
        with self.assertRaisesRegex(ValueError, "boolean"):
            NOTES.validate_fragment(invalid, "fixture")
        invalid["compatibility"] = {**original["compatibility"], "network_reset": True, "data_rebuild": True}
        with self.assertRaisesRegex(ValueError, "redundant"):
            NOTES.validate_fragment(invalid, "fixture")
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            NOTES.load_json_bytes(b'{"summary":"a","summary":"b"}', "fixture")

    def test_initial_release_has_full_inventory_and_no_invented_previous_evidence(self):
        changes = NOTES.build_changes(self.repo, "v0.1.0", GATEWAY)
        self.assertIsNone(changes["previous_release"])
        self.assertIsNone(changes["previous_source_inputs"])
        self.assertEqual(changes["coverage"]["unclassified"], 1)
        self.assertFalse(changes["coverage_enforced"])
        self.assertIn("尚无已发布版本", NOTES.render_markdown(changes))

    def test_multiple_trailers_and_unclassified_inventory_are_preserved(self):
        self.fragment("explorer-first-change")
        self.fragment("explorer-second-change")
        self.commit("Two effects\n\nRelease-Note: explorer-first-change\nRelease-Note: explorer-second-change")
        self.commit("Maintenance\n\nRelease-Note: none")
        self.commit("Unknown fragment\n\nRelease-Note: explorer-missing-change")
        self.commit("No trailer")
        changes = self.release()
        self.assertEqual(changes["coverage"], {"classified": 1, "exempt": 1, "unclassified": 2})
        self.assertEqual(changes["commits"][0]["release_notes"], ["explorer-first-change", "explorer-second-change"])
        self.assertEqual(len(changes["changes"]), 2)
        self.assertIn(self.previous["source_revision"], changes["compare_url"])

    def test_duplicate_and_mixed_none_trailers_are_rejected(self):
        for trailers in (["explorer-change", "explorer-change"], ["none", "explorer-change"]):
            with self.subTest(trailers=trailers), self.assertRaises(ValueError):
                NOTES._classify_release_notes(trailers, {"explorer-change"}, "fixture")
        self.assertEqual(NOTES._classify_release_notes(["explorer-change", "unknown"], {"explorer-change"}, "fixture"), "unclassified")

    def test_uncommitted_fragment_changes_are_not_release_inputs(self):
        self.commit()
        changes = self.release()
        self.fragment()
        self.assertEqual(NOTES.build_changes(self.repo, "v0.2.0", GATEWAY, previous=self.previous), changes)

    def test_published_fragment_edit_delete_and_rename_are_rejected(self):
        original = next((self.repo / NOTES.FRAGMENT_PATH_PREFIX).glob("*.json"))
        for operation in ("edit", "delete", "rename"):
            with self.subTest(operation=operation):
                self.git("reset", "--hard", self.previous["source_revision"])
                if operation == "edit":
                    original.write_bytes(original.read_bytes() + b"\n")
                elif operation == "delete":
                    original.unlink()
                else:
                    original.rename(original.with_name("renamed.json"))
                self.commit()
                with self.assertRaisesRegex(ValueError, "append-only|file name"):
                    NOTES.range_changes(self.repo, self.git("rev-parse", "HEAD"), self.previous["source_revision"])

    def test_symlink_and_mismatched_fragment_name_are_rejected(self):
        path, _ = self.fragment()
        path.rename(path.with_name("wrong-name.json"))
        with self.assertRaisesRegex(ValueError, "file name"):
            NOTES.validate_fragment_directory(self.repo)
        path.with_name("wrong-name.json").unlink()
        path.symlink_to(self.repo / "explorer/config.example.json")
        self.commit()
        with self.assertRaisesRegex(ValueError, "regular"):
            NOTES.fragments_at(self.repo, self.git("rev-parse", "HEAD"))

    def test_previous_release_selects_highest_lower_published_tag_and_skips_drafts(self):
        self.git("tag", "-a", "v0.2.0-rc.2", "-m", "RC")
        self.git("tag", "-a", "v0.2.0-rc.10", "-m", "RC")
        expected = NOTES.tag_identity(self.repo, "v0.2.0-rc.10")
        releases = [{"tag_name": tag, "draft": draft, "published_at": "2026-09-09T00:00:00Z"}
                    for tag, draft in (("v0.1.0", False), ("v0.2.0-rc.2", False), ("v0.2.0-rc.10", False),
                                       ("v0.2.0", True), ("v0.3.0", False), ("usdb-public-v0.1.0", False))]
        api = Mock()
        api.json.side_effect = [[releases], {"object": {"type": "tag", "sha": expected["tag_object"]}}]
        self.assertEqual(NOTES.previous_published(api, self.repo, "v0.2.1"), expected)
        api.json.side_effect = [[[releases[3]]]]
        self.assertIsNone(NOTES.previous_published(api, self.repo, "v0.2.1"))

    def test_releases_published_after_build_start_do_not_change_the_boundary(self):
        self.git("tag", "-a", "v0.2.0", "-m", "Later published release")
        api = Mock()
        releases = [{"tag_name": "v0.1.0", "draft": False, "published_at": "2026-09-08T00:00:00Z"},
                    {"tag_name": "v0.2.0", "draft": False, "published_at": "2026-09-10T00:00:00Z"}]
        api.json.side_effect = [[releases], {"object": {"type": "tag", "sha": self.previous["tag_object"]}}]
        self.assertEqual(NOTES.previous_published(api, self.repo, "v0.3.0", published_before="2026-09-09T00:00:00Z"), self.previous)

    def test_rehashed_record_cannot_choose_a_different_published_boundary(self):
        self.commit()
        changes = self.release()
        output = self.root / "new-assets"
        body = self.root / "new-notes.md"
        NOTES.write_release_files(changes, output, body)
        with self.assertRaisesRegex(ValueError, "wrong published boundary"):
            NOTES.validate_release_files(self.repo, "v0.2.0", GATEWAY, output, body.read_text(), expected_previous=None)

    def test_moved_previous_tag_and_nonancestor_range_fail(self):
        self.commit()
        self.git("tag", "-fa", "v0.1.0", "-m", "Moved")
        with self.assertRaisesRegex(ValueError, "previous release tag changed"):
            self.release()
        self.git("checkout", "--orphan", "unrelated")
        self.commit()
        with self.assertRaises(ValueError):
            NOTES.range_changes(self.repo, self.git("rev-parse", "HEAD"), self.previous["source_revision"])

    def test_automatic_network_and_runtime_evidence_overrides_missing_flags(self):
        catalog = self.repo / "explorer/networks/usdb-testnet-v0.json"
        value = NOTES.load_json(catalog)
        value["chain_id"] += 1
        catalog.write_bytes(NOTES.canonical_json(value))
        lock = self.repo / "explorer/assets/images.lock.json"
        value = NOTES.load_json(lock)
        value["images"]["postgres"]["reference"] = "docker.io/library/postgres@sha256:" + "cd" * 32
        lock.write_bytes(NOTES.canonical_json(value))
        self.commit()
        changes = self.release()
        self.assertEqual(changes["compatibility"]["classification"], "network_reset")
        self.assertTrue(changes["compatibility"]["flags"]["restart_required"])
        self.assertEqual({entry["path"] for entry in changes["compatibility"]["source_changes"]}, {"network", "images.postgres"})

    def test_go_image_change_requires_restart_for_rebuilt_gateway_and_ca_certificates(self):
        lock = self.repo / "explorer/assets/images.lock.json"
        value = NOTES.load_json(lock)
        value["images"]["go"]["reference"] = "docker.io/library/golang@sha256:" + "cd" * 32
        lock.write_bytes(NOTES.canonical_json(value))
        self.commit()
        changes = self.release()
        self.assertEqual(changes["compatibility"]["classification"], "restart_required")
        self.assertEqual(changes["compatibility"]["source_changes"][0]["impact"], "restart_required")

    def test_gateway_source_and_config_schema_changes_have_separate_flags(self):
        (self.repo / "gateway").mkdir()
        (self.repo / "gateway/main.go").write_text("package main\n")
        config = self.repo / "explorer/public_config.py"
        config.write_text(config.read_text().replace("usdb-public-config:v1", "usdb-public-config:v2"))
        self.commit()
        changes = self.release()
        self.assertTrue(changes["compatibility"]["flags"]["config_change"])
        self.assertTrue(changes["compatibility"]["flags"]["restart_required"])

    def test_existing_outputs_cannot_be_overwritten_and_render_is_deterministic(self):
        changes = NOTES.build_changes(self.repo, "v0.1.0", GATEWAY)
        body = (self.root / "notes.md").read_text()
        self.assertEqual(NOTES.validate_release_files(self.repo, "v0.1.0", GATEWAY, self.assets, body), changes)
        with self.assertRaisesRegex(ValueError, "already exists"):
            NOTES.write_release_files(changes, self.assets, self.root / "notes.md")

    def test_build_cli_uses_the_tag_build_and_validates_its_generated_files(self):
        api = PublicReleaseAPI(self.repo, self.assets)
        output, body = self.root / "cli-assets", self.root / "cli-notes.md"
        arguments = ["--repository-root", str(self.repo), "--release-id", "v0.1.0", "--gateway-image", GATEWAY,
                     "--output-dir", str(output), "--notes", str(body)]
        with patch.object(PUBLISH, "GitHub", return_value=api), patch.dict(os.environ, {"GITHUB_RUN_ID": "101"}):
            with patch.object(sys, "argv", ["release_notes.py", "generate", *arguments]):
                self.assertEqual(NOTES.main(), 0)
            with patch.object(sys, "argv", ["release_notes.py", "validate-release", *arguments]):
                self.assertEqual(NOTES.main(), 0)
        self.assertEqual((output / "release-changes.json").read_bytes(), (self.assets / "release-changes.json").read_bytes())
        self.assertEqual(api.writes, [])


if __name__ == "__main__":
    unittest.main()

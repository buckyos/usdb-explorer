#!/usr/bin/env python3
"""Exercise public draft promotion, immutable input checks and download verification."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "explorer"))
import package_release as PACKAGE
import publish_release as PUBLISH
import release_installer as RENDER
from common.public_release import PublicReleaseAPI, make_public_release, rewrite_public_archive


class PublicReleasePublishTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo, self.assets = make_public_release(self.root, ROOT, PACKAGE)
        self.api = PublicReleaseAPI(self.repo, self.assets)

    def inspect(self):
        return PUBLISH.inspect_release(self.api, self.repo, self.api.tag)

    def promote(self, result, expected=None, downloader=None):
        PUBLISH.promote(self.api, result, expected or result["fingerprint"],
                        downloader=downloader or self.api.public_download)

    def test_existing_legacy_draft_is_verified_without_new_build_metadata(self):
        result = self.inspect()
        self.assertEqual(result["snapshot"]["source_revision"], self.api.revision)
        self.assertTrue(result["draft"])
        self.assertEqual(len(result["snapshot"]["assets"]), 4)
        self.assertEqual(self.api.writes, [])
        self.assertFalse((self.repo / ".github/workflows/release-publish.yml").exists())

    def test_publish_preserves_asset_ids_hashes_notes_and_reruns_without_writes(self):
        before = deepcopy(self.api.release)
        result = self.inspect()
        self.promote(result)
        self.assertEqual(self.api.writes, [{"draft": False, "prerelease": True, "make_latest": "false"}])
        for key in ("assets", "body", "name", "tag_name", "id"):
            self.assertEqual(self.api.release[key], before[key])
        published = self.inspect()
        self.assertEqual(result["fingerprint"], published["fingerprint"])
        self.promote(published)
        self.assertEqual(len(self.api.writes), 1)

    def test_node_tags_and_branch_names_are_rejected_before_api_access(self):
        for tag in ("main", "0.2.0", "usdb-testnet-v0-r20", "usdb-explorer-v1.0", "usdb-explorer-v1.0.0\n"):
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, "release tag"):
                PUBLISH.inspect_release(self.api, self.repo, tag)
        self.assertEqual(self.api.writes, [])

    def test_tag_dispatch_verifies_assets_from_the_workflow_revision(self):
        result = PUBLISH.inspect_release(self.api, self.repo, self.api.tag,
                                         expected_source_revision=self.api.revision)
        self.assertEqual(result["snapshot"]["source_revision"], self.api.revision)
        self.assertEqual(len(result["snapshot"]["assets"]), 4)
        self.assertEqual(self.api.writes, [])

    def test_wrong_or_empty_workflow_revision_fails_before_api_access(self):
        with patch.object(self.api, "json") as api_read:
            for revision in ("ff" * 20, ""):
                with self.subTest(revision=revision), self.assertRaisesRegex(ValueError, "workflow source revision"):
                    PUBLISH.inspect_release(self.api, self.repo, self.api.tag,
                                            expected_source_revision=revision)
            api_read.assert_not_called()

    def test_newer_checkout_is_rejected_for_dispatch_but_can_inspect_old_releases_locally(self):
        subprocess.run(["git", "-C", str(self.repo), "commit", "--allow-empty", "-qm", "Advance main"], check=True)
        with patch.object(self.api, "json") as api_read:
            with self.assertRaisesRegex(ValueError, "publisher checkout"):
                PUBLISH.inspect_release(self.api, self.repo, self.api.tag,
                                        expected_source_revision=self.api.revision)
            api_read.assert_not_called()
        self.assertEqual(self.inspect()["snapshot"]["source_revision"], self.api.revision)

    def test_tag_moved_after_dispatch_is_rejected_even_if_local_and_remote_tags_agree(self):
        subprocess.run(["git", "-C", str(self.repo), "commit", "--allow-empty", "-qm", "Advance main"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "tag", "-fa", self.api.tag, "-m", "Moved tag"],
                       check=True, stdout=subprocess.DEVNULL)
        self.api.remote["object"]["sha"] = PUBLISH.git(self.repo, "rev-parse", self.api.tag).decode().strip()
        with patch.object(self.api, "json") as api_read:
            with self.assertRaisesRegex(ValueError, "workflow source revision"):
                PUBLISH.inspect_release(self.api, self.repo, self.api.tag,
                                        expected_source_revision=self.api.revision)
            api_read.assert_not_called()

    def test_lightweight_or_moved_remote_tag_is_rejected(self):
        self.api.remote["object"]["sha"] = "ff" * 20
        with self.assertRaisesRegex(ValueError, "tag changed"):
            self.inspect()
        subprocess.run(["git", "-C", str(self.repo), "tag", "v0.2.0"], check=True)
        with self.assertRaisesRegex(ValueError, "must be annotated"):
            PUBLISH.inspect_release(self.api, self.repo, "v0.2.0")

    def test_build_must_match_tag_revision_workflow_and_success(self):
        original = deepcopy(self.api.runs)
        for key, value in (("head_sha", "ff" * 20), ("head_branch", "main"),
                           ("event", "workflow_dispatch"), ("status", "in_progress"),
                           ("conclusion", "failure"), ("path", ".github/workflows/ci.yml")):
            with self.subTest(key=key):
                self.api.runs = deepcopy(original)
                self.api.runs[0][key] = value
                with self.assertRaisesRegex(ValueError, "successful public release build"):
                    self.inspect()
        self.api.runs = original + deepcopy(original)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.inspect()
        self.assertEqual(self.api.writes, [])

    def test_hidden_draft_reports_required_access_after_a_successful_build(self):
        original_json = self.api.json

        def read(endpoint, **kwargs):
            if endpoint == "releases?per_page=100":
                return [[]]
            return original_json(endpoint, **kwargs)

        with patch.object(self.api, "json", side_effect=read), patch.object(self.api, "download") as download:
            with self.assertRaisesRegex(ValueError, "successful build 101.*contents: write"):
                self.inspect()
            download.assert_not_called()
        self.assertEqual(self.api.writes, [])

    def test_duplicate_release_records_are_not_misreported_as_missing_permissions(self):
        original_json = self.api.json

        def read(endpoint, **kwargs):
            if endpoint == "releases?per_page=100":
                return [[deepcopy(self.api.release)], [deepcopy(self.api.release)]]
            return original_json(endpoint, **kwargs)

        with patch.object(self.api, "json", side_effect=read):
            with self.assertRaisesRegex(ValueError, "multiple GitHub Releases"):
                self.inspect()
        self.assertEqual(self.api.writes, [])

    def test_missing_extra_duplicate_and_incomplete_assets_are_rejected(self):
        original = deepcopy(self.api.release["assets"])
        variants = [original[:-1], original + [original[0]], original[:-1] + [original[0]]]
        for key, value in (("state", "new"), ("size", 0), ("size", PUBLISH.MAX_ASSET_BYTES + 1),
                           ("digest", ""), ("id", -1)):
            changed = deepcopy(original)
            changed[0][key] = value
            variants.append(changed)
        for assets in variants:
            with self.subTest(assets=assets):
                self.api.release["assets"] = assets
                with self.assertRaises(ValueError):
                    self.inspect()

    def test_download_digest_and_canonical_checksum_are_both_required(self):
        checksum = self.assets / ("usdb-explorer-" + self.api.tag + ".tar.gz.sha256")
        checksum.write_text("0" * 64 + "  " + "usdb-explorer-" + self.api.tag + ".tar.gz\n")
        with self.assertRaisesRegex(ValueError, "GitHub metadata"):
            self.inspect()
        self.api.refresh_assets()
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.inspect()

    def test_wrong_revision_fails_even_after_rehashing(self):
        def mutate(payload, manifest):
            manifest["source_revision"] = "ff" * 20
        rewrite_public_archive(self.root, self.repo, self.assets, mutate, RENDER)
        self.api.refresh_assets()
        with self.assertRaisesRegex(ValueError, "revision is wrong or dirty"):
            self.inspect()

    def test_dirty_source_fails_even_with_the_expected_revision(self):
        def mutate(payload, manifest):
            manifest["source_dirty"] = True
        rewrite_public_archive(self.root, self.repo, self.assets, mutate, RENDER)
        self.api.refresh_assets()
        with self.assertRaisesRegex(ValueError, "revision is wrong or dirty"):
            self.inspect()

    def test_modified_source_file_fails_even_after_rehashing_all_assets(self):
        def mutate(payload, manifest):
            (payload / "usdb_public.py").write_text("raise SystemExit('unexpected code')\n")
        rewrite_public_archive(self.root, self.repo, self.assets, mutate, RENDER)
        self.api.refresh_assets()
        with self.assertRaisesRegex(ValueError, "differs from tagged source: usdb_public.py"):
            self.inspect()

    def test_third_party_image_substitution_is_rejected(self):
        def mutate(payload, manifest):
            path = payload / "assets/images.lock.json"
            lock = json.loads(path.read_text())
            lock["images"]["backend"]["reference"] = "ghcr.io/other/backend@sha256:" + "ff" * 32
            path.write_text(json.dumps(lock))
        rewrite_public_archive(self.root, self.repo, self.assets, mutate, RENDER)
        self.api.refresh_assets()
        with self.assertRaisesRegex(ValueError, "image lock or qualification"):
            self.inspect()

    def test_rehashed_installer_with_wrong_download_url_is_rejected(self):
        installer = self.assets / ("install-usdb-explorer-" + self.api.tag + ".sh")
        installer.write_text(installer.read_text().replace("https://github.com/", "https://example.invalid/"))
        installer.with_name(installer.name + ".sha256").write_text(
            hashlib.sha256(installer.read_bytes()).hexdigest() + "  " + installer.name + "\n")
        self.api.refresh_assets()
        with self.assertRaisesRegex(ValueError, "installer differs"):
            self.inspect()

    def test_changes_during_approval_require_a_new_preflight(self):
        before = self.inspect()
        self.api.release["body"] += "\nChanged during approval."
        after = self.inspect()
        with self.assertRaisesRegex(ValueError, "changed after preflight"):
            self.promote(after, before["fingerprint"])
        self.assertEqual(self.api.writes, [])

    def test_already_published_stable_release_is_not_reclassified(self):
        self.api.release["draft"] = False
        with self.assertRaisesRegex(ValueError, "must already be a pre-release"):
            self.inspect()
        self.assertEqual(self.api.writes, [])

    def test_anonymous_404_fails_and_retry_only_checks_existing_publication(self):
        def not_found(url, path):
            raise subprocess.CalledProcessError(22, ["curl", url])
        with self.assertRaisesRegex(ValueError, "release is published but anonymous download verification failed"):
            self.promote(self.inspect(), downloader=not_found)
        self.assertFalse(self.api.release["draft"])
        self.promote(self.inspect())
        self.assertEqual(len(self.api.writes), 1)

    def test_anonymous_corrupt_download_fails(self):
        def corrupt(url, path):
            path.write_bytes(b"wrong download body")
        with self.assertRaisesRegex(ValueError, "anonymous release download differs"):
            self.promote(self.inspect(), downloader=corrupt)

    def test_public_download_does_not_use_authentication_and_disables_curl_config(self):
        with patch.object(PUBLISH.subprocess, "run") as run:
            PUBLISH.download_public("https://github.com/buckyos/usdb-explorer/example", Path("download"))
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["curl", "--disable"])
        self.assertNotIn("-H", command)
        self.assertNotIn("--netrc", command)
        self.assertIn("--retry-all-errors", command)

    def test_github_pagination_supports_adjacent_json_pages_from_older_gh(self):
        response = subprocess.CompletedProcess([], 0, stdout='[{"id": 1}]\n[{"id": 2}]\n')
        with patch.object(PUBLISH.subprocess, "run", return_value=response) as run:
            pages = PUBLISH.GitHub().json("releases?per_page=100", paginate=True)
        self.assertEqual(pages, [[{"id": 1}], [{"id": 2}]])
        self.assertIn("--paginate", run.call_args.args[0])
        self.assertNotIn("--slurp", run.call_args.args[0])


class StructuredReleasePublishTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo, self.assets = make_public_release(self.root, ROOT, PACKAGE, structured_notes=True)
        self.api = PublicReleaseAPI(self.repo, self.assets)

    def inspect(self):
        return PUBLISH.inspect_release(self.api, self.repo, self.api.tag)

    def test_seven_assets_and_original_body_survive_promotion_and_anonymous_checks(self):
        original = deepcopy(self.api.release)
        result = self.inspect()
        with patch.object(self.api, "public_download", wraps=self.api.public_download) as download:
            PUBLISH.promote(self.api, result, result["fingerprint"], downloader=download)
            self.assertEqual(download.call_count, 7)
        self.assertEqual(self.api.release["assets"], original["assets"])
        self.assertEqual(self.api.release["body"], original["body"])
        again = self.inspect()
        PUBLISH.promote(self.api, again, again["fingerprint"], downloader=self.api.public_download)
        self.assertEqual(len(self.api.writes), 1)

    def test_removing_change_assets_cannot_downgrade_the_tag_to_legacy(self):
        original = deepcopy(self.api.release["assets"])
        for missing in ({"release-changes.md"}, PUBLISH.RELEASE_NOTES.ASSET_NAMES):
            with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, "complete asset set"):
                self.api.release["assets"] = [asset for asset in original if asset["name"] not in missing]
                self.inspect()
        self.assertEqual(self.api.writes, [])

    def test_modified_body_is_rejected_before_promotion(self):
        self.api.release["body"] += "\nUnexpected release claim."
        with self.assertRaisesRegex(ValueError, "body differs"):
            self.inspect()
        self.assertEqual(self.api.writes, [])

    def test_rehashed_changes_cannot_rewrite_compatibility_coverage_or_source(self):
        path = self.assets / "release-changes.json"
        original = json.loads(path.read_text())
        variants = []
        for key, value in (("gateway_image", "ghcr.io/buckyos/usdb-explorer-gateway@sha256:" + "cd" * 32),
                           ("source_revision", "ff" * 20), ("coverage_enforced", True),
                           ("coverage", {"classified": 1, "exempt": 0, "unclassified": 0})):
            altered = deepcopy(original)
            altered[key] = value
            variants.append(altered)
        altered = deepcopy(original)
        altered["changes"][0]["summary"] = "Unreviewed feature claim"
        variants.append(altered)
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "frozen source evidence"):
                path.write_bytes(PUBLISH.RELEASE_NOTES.canonical_json(changes))
                path.with_suffix(".json.sha256").write_text(PUBLISH.sha256(path) + "  release-changes.json\n")
                self.api.refresh_assets()
                self.inspect()
        self.assertEqual(self.api.writes, [])

    def test_rehashed_markdown_and_wrong_json_checksum_are_rejected(self):
        markdown = self.assets / "release-changes.md"
        original = markdown.read_bytes()
        markdown.write_text("Unreviewed upgrade instructions\n")
        self.api.refresh_assets()
        with self.assertRaisesRegex(ValueError, "Markdown mismatch"):
            self.inspect()
        markdown.write_bytes(original)
        (self.assets / "release-changes.json.sha256").write_text("0" * 64 + "  release-changes.json\n")
        self.api.refresh_assets()
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.inspect()


if __name__ == "__main__":
    unittest.main()

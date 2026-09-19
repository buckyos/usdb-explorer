#!/usr/bin/env python3
"""Verify USDB page deployment boundaries and immutable frontend releases."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "explorer"), str(ROOT / "scripts")]
import public_config as CONFIG
import package_release as PACKAGE
import publish_release as PUBLISH
import release_notes as NOTES
from common.public_release import make_public_release, PublicReleaseAPI, rewrite_public_archive


class USDBPageDeploymentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def config(self, rpc):
        value = json.loads((ROOT / "explorer/config.example.json").read_text())
        value["rpc"] = rpc
        path = self.root / "config.json"
        path.write_text(json.dumps(value))
        return CONFIG.load_config(path, kit=ROOT / "explorer")

    def test_local_indexer_uses_private_relay_and_can_be_disabled(self):
        config, _ = self.config({"mode":"local-node"})
        self.assertEqual(config["rpc"]["indexer_url"], "http://127.0.0.1:28020")
        self.assertEqual(CONFIG.container_rpc(config)["indexer_url"], "http://rpc-relay:8080/indexer")
        host = CONFIG.local_rpc_config(config, host=True)
        self.assertIn("28020", host)
        self.assertIn("/indexer", host)
        config, _ = self.config({"mode":"local-node", "indexer_url":None})
        self.assertNotIn("/indexer", CONFIG.local_rpc_config(config, host=True))
        for url in ("http://10.0.0.1:28020", "https://127.0.0.1:28020"):
            with self.assertRaisesRegex(ValueError, "loopback"):
                self.config({"mode":"local-node", "indexer_url":url})

    def test_external_indexer_is_explicit_and_not_a_public_rpc_method(self):
        config, _ = self.config({"read_url":"http://node.internal:8545"})
        self.assertNotIn("indexer_url", config["rpc"])
        config, _ = self.config({"read_url":"http://node.internal:8545", "indexer_url":"http://indexer.internal:28020"})
        self.assertEqual(CONFIG.container_rpc(config)["indexer_url"], "http://indexer.internal:28020")
        public = CONFIG.nginx_config(config)
        self.assertNotIn("28020", public)
        self.assertNotIn("location /indexer", public)

    def test_custom_frontend_cannot_be_packaged_without_immutable_digest(self):
        gateway = "ghcr.io/buckyos/usdb-explorer-gateway@sha256:" + "ab" * 32
        for value in (None, "ghcr.io/buckyos/usdb-explorer-frontend:latest", gateway):
            with self.subTest(image=value), self.assertRaisesRegex(ValueError, "frontend-image"):
                PACKAGE.package(ROOT, self.root / "output", "0.3.0", gateway, value)

    def test_publish_binds_frontend_digest_and_overlay_to_tagged_source(self):
        repo, assets = make_public_release(self.root, ROOT, PACKAGE, structured_notes=True)
        api = PublicReleaseAPI(repo, assets)
        PUBLISH.inspect_release(api, repo, "v0.1.0")
        def mutate(root, manifest):
            path = root / "assets/images.lock.json"
            value = json.loads(path.read_text())
            value["images"]["frontend"]["upstream"]["source_revision"] = "f" * 40
            path.write_text(json.dumps(value))
        rewrite_public_archive(self.root, repo, assets, mutate, PUBLISH)
        api.refresh_assets()
        with self.assertRaisesRegex(ValueError, "frontend"):
            PUBLISH.inspect_release(api, repo, "v0.1.0")

    def test_overlay_changes_require_restart_and_wrong_digest_invalidates_notes(self):
        repo, assets = make_public_release(self.root, ROOT, PACKAGE, structured_notes=True)
        changes = NOTES.load_json(assets / "release-changes.json")
        before = changes["source_inputs"]
        after = deepcopy(before)
        after["frontend_source_sha256"] = "f" * 64
        self.assertEqual(NOTES.source_changes(before, after)[0]["impact"], "restart_required")
        with self.assertRaisesRegex(ValueError, "frozen source"):
            NOTES.validate_release_files(repo, "v0.1.0", changes["gateway_image"], assets,
                                         NOTES.render_release_notes(changes),
                                         frontend_image="ghcr.io/buckyos/usdb-explorer-frontend@sha256:" + "ee" * 32)

    def test_archive_checksum_is_verified_before_unpacking(self):
        spec = importlib.util.spec_from_file_location("frontend_prepare", ROOT / "frontend/prepare.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        archive = self.root / "bad.tar.gz"
        archive.write_bytes(b"wrong source")
        with self.assertRaisesRegex(ValueError, "checksum"):
            module.prepare(archive, self.root / "source")
        self.assertFalse((self.root / "source").exists())


if __name__ == "__main__":
    unittest.main()

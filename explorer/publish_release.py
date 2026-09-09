#!/usr/bin/env python3
"""Verify and promote existing public-service assets without rebuilding a release."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile

from install_release import extract_release, sha256, verify_release
from package_release import FILES
from release_installer import render_installer

REPOSITORY = "buckyos/usdb-explorer"
BUILD_WORKFLOW = ".github/workflows/release-build.yml"
MAX_ASSET_BYTES = 16 * 1024**2


class GitHub:
    """Use the workflow token for API reads and one explicit release promotion."""

    def json(self, endpoint, *, fields=None, paginate=False):
        command = ["gh", "api", f"repos/{REPOSITORY}/{endpoint}"]
        if fields is not None:
            command += ["--method", "PATCH", "--input", "-"]
        if paginate:
            command += ["--paginate"]
        result = subprocess.run(command, input=json.dumps(fields) if fields is not None else None,
                                stdout=subprocess.PIPE, text=True, check=True, timeout=120)
        if paginate:
            # Older operator gh versions emit adjacent JSON pages without --slurp support.
            pages, remaining = [], result.stdout.strip()
            while remaining:
                page, end = json.JSONDecoder().raw_decode(remaining)
                pages.append(page)
                remaining = remaining[end:].lstrip()
            return pages
        return json.loads(result.stdout)

    def download(self, asset_id, destination):
        with destination.open("xb") as output:
            subprocess.run(["gh", "api", f"repos/{REPOSITORY}/releases/assets/{asset_id}",
                            "-H", "Accept: application/octet-stream"], stdout=output,
                           check=True, timeout=120)


def git(repo, *arguments):
    """Read immutable tagged source without checking it out or executing its code."""
    return subprocess.check_output(["git", "-C", str(repo), *arguments], timeout=30)


def release_assets(release, release_id):
    """Require the complete, bounded four-file publication produced by the builder."""
    archive_id = "usdb-explorer-" + release_id
    archive, installer = archive_id + ".tar.gz", "install-" + archive_id + ".sh"
    names = {archive, archive + ".sha256", installer, installer + ".sha256"}
    assets = release.get("assets", [])
    if (release.get("tag_name") != release_id or type(release.get("id")) is not int
            or release["id"] <= 0 or type(release.get("draft")) is not bool
            or len(assets) != len(names) or {a.get("name") for a in assets} != names):
        raise ValueError("release identity or complete asset set does not match the public tag")
    if not release["draft"] and release.get("prerelease") is not True:
        raise ValueError("existing public release must already be a pre-release; refusing to edit it")
    for asset in assets:
        if (type(asset.get("id")) is not int or asset["id"] <= 0
                or asset.get("state") != "uploaded" or type(asset.get("size")) is not int
                or not 0 < asset["size"] <= MAX_ASSET_BYTES
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or "")):
            raise ValueError("release asset is incomplete or has an invalid size/digest")
    if len({a["id"] for a in assets}) != len(assets):
        raise ValueError("release assets contain duplicate IDs")
    return sorted(assets, key=lambda asset: asset["name"])


def resolve_build(pages, release_id, revision):
    """Bind publication to one successful tag-push build, including its reusable CI."""
    runs = [run for page in pages for run in page["workflow_runs"]
            if run.get("head_branch") == release_id and run.get("head_sha") == revision
            and run.get("event") == "push"
            and run.get("path", "").split("@", 1)[0] == BUILD_WORKFLOW]
    if len(runs) != 1 or runs[0].get("status") != "completed" or runs[0].get("conclusion") != "success":
        raise ValueError("expected exactly one successful public release build for this tag and revision")
    return runs[0]


def verify_payload(directory, release_id, revision, read_source):
    """Check checksums, clean source identity and installer binding without running assets."""
    archive = directory / (release_id + ".tar.gz")
    installer = directory / ("install-" + release_id + ".sh")
    for path in (archive, installer):
        expected = sha256(path) + "  " + path.name + "\n"
        if path.with_name(path.name + ".sha256").read_text() != expected:
            raise ValueError(f"release checksum mismatch: {path.name}")
    with tempfile.TemporaryDirectory(prefix="usdb-public-verify-") as temporary:
        staging = Path(temporary)
        root = extract_release(archive, staging, release_id)
        manifest = json.loads(verify_release(root, release_id))
        if manifest.get("source_repository") != REPOSITORY or manifest.get("source_revision") != revision or manifest.get("source_dirty") is not False:
            raise ValueError("release archive source revision is wrong or dirty")
        if set(manifest["files"]) != set(FILES):
            raise ValueError("release archive differs from the supported public file allowlist")
        lock = json.loads((root / "assets/images.lock.json").read_bytes())
        source_lock = json.loads(read_source("assets/images.lock.json"))
        gateway = lock.get("images", {}).get("gateway", {})
        if (gateway.get("tag") != release_id.removeprefix("usdb-explorer-") or not re.fullmatch(
                r"ghcr\.io/buckyos/usdb-explorer-gateway@sha256:[0-9a-f]{64}", gateway.get("reference", ""))):
            raise ValueError("release gateway image is not pinned to a public gateway digest")
        source_lock["images"]["gateway"] = gateway
        if lock != source_lock or manifest.get("qualified_for_public_exposure") != lock.get("qualified_for_public_exposure"):
            raise ValueError("release image lock or qualification differs from tagged source")
        for name in FILES:
            if name != "assets/images.lock.json" and (root / name).read_bytes() != read_source(name):
                raise ValueError(f"release file differs from tagged source: {name}")
        # Render from the tagged embedded installer source; never execute a downloaded script.
        source = staging / "installer-source"
        source.mkdir()
        (source / "install_release.py").write_bytes(read_source("install_release.py"))
        if installer.read_text() != render_installer(source, archive, release_id):
            raise ValueError("release installer differs from its pinned source, archive hash or download URL")
    return gateway["reference"]


def inspect_release(api, repo, release_id):
    """Resolve and verify the original draft, or an already published immutable release."""
    if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?", release_id) is None:
        raise ValueError("expected an independent vX.Y.Z release tag")
    ref = "refs/tags/" + release_id
    if git(repo, "cat-file", "-t", ref).strip() != b"tag":
        raise ValueError("public release tag must be annotated")
    tag_object = git(repo, "rev-parse", ref).decode().strip()
    revision = git(repo, "rev-parse", ref + "^{commit}").decode().strip()
    git(repo, "merge-base", "--is-ancestor", revision, "refs/remotes/origin/main")
    remote = api.json("git/ref/tags/" + release_id)["object"]
    if remote.get("type") != "tag" or remote.get("sha") != tag_object:
        raise ValueError("remote annotated tag changed from the checked-out tag")
    run = resolve_build(api.json(
        f"actions/workflows/release-build.yml/runs?event=push&head_sha={revision}&per_page=100",
        paginate=True), release_id, revision)
    # Listing releases also finds authenticated drafts, whose public tag URLs still return 404.
    matches = [r for page in api.json("releases?per_page=100", paginate=True) for r in page
               if r.get("tag_name") == release_id]
    if len(matches) != 1:
        raise ValueError("expected one existing public release; run the tag build and wait for its draft")
    release = matches[0]
    assets = release_assets(release, release_id)
    with tempfile.TemporaryDirectory(prefix="usdb-public-assets-") as temporary:
        directory = Path(temporary)
        for asset in assets:
            path = directory / asset["name"]
            api.download(asset["id"], path)
            if path.stat().st_size != asset["size"] or "sha256:" + sha256(path) != asset["digest"]:
                raise ValueError(f"downloaded release asset differs from GitHub metadata: {asset['name']}")
        gateway = verify_payload(directory, "usdb-explorer-" + release_id, revision,
                                 lambda name: git(repo, "show", f"{revision}:explorer/{name}"))
    snapshot = {"release_id": release_id, "release_database_id": release["id"],
                "tag_object": tag_object, "source_revision": revision,
                "build_run_id": run["id"], "build_run_attempt": run["run_attempt"],
                "title": release.get("name"), "body": release.get("body"),
                "assets": [{key: asset[key] for key in ("id", "name", "size", "digest")} for asset in assets]}
    fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return {"fingerprint": fingerprint, "snapshot": snapshot, "draft": release["draft"],
            "gateway_image": gateway,
            "release_url": f"https://github.com/{REPOSITORY}/releases/tag/{release_id}"}


def download_public(url, destination):
    """Probe the actual installer URLs anonymously, with bounded HTTPS downloads and retries."""
    subprocess.run(["curl", "--disable", "--fail", "--silent", "--show-error", "--location",
                    "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "10",
                    "--max-time", "60", "--retry", "5", "--retry-all-errors", "--retry-delay", "3",
                    "--max-filesize", str(MAX_ASSET_BYTES), "--output", str(destination), url],
                   check=True, timeout=420)


def promote(api, result, expected_fingerprint, *, downloader=download_public):
    """Publish the verified draft once; reruns only verify existing publication and downloads."""
    if not expected_fingerprint or result["fingerprint"] != expected_fingerprint:
        raise ValueError("release changed after preflight; refusing to publish")
    snapshot = result["snapshot"]
    endpoint = f"releases/{snapshot['release_database_id']}"
    if result["draft"]:
        api.json(endpoint, fields={"draft": False, "prerelease": True, "make_latest": "false"})
    published = api.json(endpoint)
    assets = release_assets(published, snapshot["release_id"])
    if (published["draft"] or published.get("prerelease") is not True
            or published["id"] != snapshot["release_database_id"]
            or published.get("name") != snapshot["title"] or published.get("body") != snapshot["body"]
            or [{key: asset[key] for key in ("id", "name", "size", "digest")} for asset in assets] != snapshot["assets"]):
        raise ValueError("published release differs from the approved assets or notes")
    result["draft"] = False
    try:
        with tempfile.TemporaryDirectory(prefix="usdb-public-download-check-") as temporary:
            for asset in assets:
                path = Path(temporary) / asset["name"]
                url = f"https://github.com/{REPOSITORY}/releases/download/{snapshot['release_id']}/{asset['name']}"
                downloader(url, path)
                if path.stat().st_size != asset["size"] or "sha256:" + sha256(path) != asset["digest"]:
                    raise ValueError(f"anonymous release download differs from approved asset: {asset['name']}")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise ValueError(f"release is published but anonymous download verification failed: {error}; "
                         "preserve the assets and rerun Publish after resolving the download failure") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "publish"))
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--expected-fingerprint")
    args = parser.parse_args()
    try:
        api = GitHub()
        result = inspect_release(api, args.repository_root, args.release_id)
        if args.command == "publish":
            promote(api, result, args.expected_fingerprint)
        print(json.dumps(result, indent=2))
        if output := os.environ.get("GITHUB_OUTPUT"):
            with open(output, "a") as destination:
                destination.write(f"fingerprint={result['fingerprint']}\nrelease_url={result['release_url']}\n")
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            snapshot = result["snapshot"]
            with open(summary, "a") as destination:
                destination.write(f"### Public release {args.command}: {args.release_id}\n\n"
                                  f"Source: `{snapshot['source_revision']}`\n\n"
                                  f"[Successful build](https://github.com/{REPOSITORY}/actions/runs/{snapshot['build_run_id']})\n\n"
                                  f"Verified asset fingerprint: `{result['fingerprint']}`\n\n")
                if args.command == "publish":
                    destination.write(f"[Published pre-release]({result['release_url']}); all four anonymous downloads verified.\n")
                else:
                    destination.write("Existing assets verified; public download URLs become available after publication.\n")
        return 0
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError,
            subprocess.SubprocessError) as error:
        print(f"Public release {args.command} failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

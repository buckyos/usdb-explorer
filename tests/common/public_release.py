"""Isolated tagged repositories and GitHub assets for public publication tests."""
from copy import deepcopy
import hashlib
import json
import shutil
import subprocess
import tarfile
from unittest.mock import patch

from install_release import extract_release


def make_public_release(root, source, packager, *, structured_notes=False):
    """Produce real installer assets from a clean temporary annotated tag."""
    repo = root / "repo"
    (repo / "explorer").mkdir(parents=True)
    for name in (*packager.FILES, "install_release.py"):
        target = repo / "explorer" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / "explorer" / name, target)
    if structured_notes:
        shutil.copytree(source / ".release-notes", repo / ".release-notes")
    commands = (["init", "--initial-branch=main"], ["config", "user.name", "Release fixture"],
                ["config", "user.email", "fixture@example.invalid"], ["add", "."],
                ["-c", "commit.gpgsign=false", "commit", "-m", "Create public release fixture"],
                ["-c", "tag.gpgsign=false", "tag", "-a", "v0.1.0", "-m", "Public fixture"],
                ["update-ref", "refs/remotes/origin/main", "HEAD"])
    for arguments in commands:
        subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True)
    assets = root / "assets"
    with patch.object(packager, "check_network"):
        packager.package(repo, assets, "0.1.0", "ghcr.io/buckyos/usdb-explorer-gateway@sha256:" + "ab" * 32)
    if structured_notes:
        import release_notes
        changes = release_notes.build_changes(repo, "v0.1.0", "ghcr.io/buckyos/usdb-explorer-gateway@sha256:" + "ab" * 32)
        release_notes.write_release_files(changes, assets, root / "notes.md")
    return repo, assets


class PublicReleaseAPI:
    """Serve paginated reads and record the only permitted release-state mutation."""

    def __init__(self, repo, assets):
        self.assets = assets
        self.tag = "v0.1.0"
        self.revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        tag_object = subprocess.check_output(["git", "-C", str(repo), "rev-parse", self.tag], text=True).strip()
        self.remote = {"object": {"type": "tag", "sha": tag_object}}
        self.runs = [{"id": 101, "run_attempt": 1, "path": ".github/workflows/release-build.yml",
                      "event": "push", "head_sha": self.revision, "head_branch": self.tag,
                      "created_at": "2026-09-09T00:00:00Z",
                      "status": "completed", "conclusion": "success"}]
        self.release = {"id": 202, "tag_name": self.tag, "draft": True, "prerelease": False,
                        "name": self.tag, "body": "Existing preview notes and install command."}
        if (assets / "release-changes.json").exists():
            import release_notes
            self.release["body"] = release_notes.render_release_notes(release_notes.load_json(assets / "release-changes.json"))
        self.writes = []
        self.refresh_assets()

    def refresh_assets(self):
        self.release["assets"] = [{"id": 300 + index, "name": path.name, "state": "uploaded",
                                   "size": path.stat().st_size,
                                   "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}
                                  for index, path in enumerate(sorted(self.assets.iterdir()))]

    def json(self, endpoint, *, fields=None, paginate=False):
        if fields is not None:
            assert endpoint == "releases/202", endpoint
            self.writes.append(deepcopy(fields))
            self.release.update({key: value for key, value in fields.items() if key != "make_latest"})
        if endpoint == "releases/202":
            return deepcopy(self.release)
        if endpoint.startswith("git/ref/tags/"):
            return deepcopy(self.remote)
        if endpoint.startswith("actions/workflows/release-build.yml/runs?"):
            assert paginate
            return [{"workflow_runs": []}, {"workflow_runs": deepcopy(self.runs)}]
        if endpoint == "actions/runs/101":
            return deepcopy(self.runs[0])
        if endpoint == "releases?per_page=100":
            assert paginate
            return [[], [deepcopy(self.release)]]
        raise AssertionError(f"Unexpected GitHub API call: {endpoint}")

    def download(self, asset_id, destination):
        asset = next(a for a in self.release["assets"] if a["id"] == asset_id)
        shutil.copyfile(self.assets / asset["name"], destination)

    def public_download(self, url, destination):
        assert self.release["draft"] is False
        prefix = f"https://github.com/buckyos/usdb-explorer/releases/download/{self.tag}/"
        assert url.startswith(prefix), url
        name = url.removeprefix(prefix)
        assert name in {a["name"] for a in self.release["assets"]}, name
        shutil.copyfile(self.assets / name, destination)


def rewrite_public_archive(root, repo, assets, mutate, renderer):
    """Rehash modified payloads to test source binding beyond transport integrity."""
    release_id = "usdb-explorer-v0.1.0"
    archive = assets / (release_id + ".tar.gz")
    staging = root / "repacked"
    staging.mkdir()
    payload = extract_release(archive, staging, release_id)
    manifest = json.loads((payload / "release.json").read_text())
    mutate(payload, manifest)
    manifest["files"] = {name: hashlib.sha256((payload / name).read_bytes()).hexdigest()
                         for name in manifest["files"]}
    (payload / "release.json").write_text(json.dumps(manifest))
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload, arcname=release_id)
    installer = assets / ("install-" + release_id + ".sh")
    installer.write_text(renderer.render_installer(repo / "explorer", archive, release_id))
    for path in (archive, installer):
        path.with_name(path.name + ".sha256").write_text(
            hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n")

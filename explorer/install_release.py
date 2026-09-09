"""Install a verified public-service kit without touching deployment state or running services."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import secrets
import shlex
import shutil
import sys
import tarfile
import tempfile

RELEASE_ID = re.compile(r"usdb-(?:public|explorer)-v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?")


def sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def extract_release(archive, staging, release_id):
    """Extract only bounded regular files beneath the expected release directory."""
    seen, total = set(), 0
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            parts = PurePosixPath(member.name).parts
            if (not parts or parts[0] != release_id or any(part in {".", ".."} for part in member.name.split("/"))
                    or member.name.startswith("/") or "\\" in member.name or member.name in seen
                    or not (member.isfile() or member.isdir())):
                raise ValueError("release archive contains unsafe or duplicate entries")
            seen.add(member.name)
            total += member.size
            if len(seen) > 128 or member.size < 0 or member.size > 4 * 1024**2 or total > 16 * 1024**2:
                raise ValueError("release archive exceeds installer size limits")
            target = staging / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o755 if member.name in {release_id + "/usdb-public", release_id + "/usdb-explorer"} else 0o644)
    return staging / release_id


def verify_release(root, release_id, expected_manifest=None):
    """Verify the installed files before reusing a version or activating it."""
    path = root / "release.json"
    if root.is_symlink() or path.is_symlink():
        raise ValueError("installed release cannot be a symlink")
    manifest_bytes = path.read_bytes()
    if expected_manifest is not None and manifest_bytes != expected_manifest:
        raise ValueError("existing release differs from this installer; refusing to replace it")
    manifest = json.loads(manifest_bytes)
    if (manifest.get("schema_version") != "usdb-public-release:v1"
            or manifest.get("version") != re.sub(r"^usdb-(?:public|explorer)-v", "", release_id)
            or not isinstance(manifest.get("files"), dict)
            or not {"usdb-public", "usdb_public.py", "config.example.json", "assets/images.lock.json"}.issubset(manifest["files"])):
        raise ValueError("release manifest identity or required files are invalid")
    files = manifest["files"]
    if release_id.startswith("usdb-explorer-") and (
            "usdb-explorer" not in files or not os.access(root / "usdb-explorer", os.X_OK)):
        raise ValueError("explorer release is missing its executable command entrypoint")
    if any(p.is_symlink() for p in root.rglob("*")) or not os.access(root / "usdb-public", os.X_OK):
        raise ValueError("installed release contains symlinks or a non-executable entrypoint")
    actual = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
    if actual != set(files) | {"release.json"}:
        raise ValueError("release file set differs from its manifest")
    for name, expected in files.items():
        target = root / name
        if (not target.resolve().is_relative_to(root) or target.is_symlink()
                or not re.fullmatch(r"[0-9a-f]{64}", str(expected)) or sha256(target) != expected):
            raise ValueError(f"installed release file changed: {name}")
    return manifest_bytes


def temporary_link(target, path):
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(8))
    os.symlink(target, temporary)
    return temporary


def install(archive, release_id, expected_sha256, install_root, bin_dir, config_file):
    """Install immutable version directories and atomically switch only the CLI pointer."""
    if RELEASE_ID.fullmatch(release_id) is None or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("invalid pinned installer identity")
    if sha256(archive) != expected_sha256:
        raise ValueError("release archive SHA-256 mismatch")
    install_root, bin_dir = install_root.expanduser().resolve(), bin_dir.expanduser().resolve()
    config_file = config_file.expanduser().absolute()
    if any(c in str(path) for path in (install_root, bin_dir, config_file) for c in "\n\r"):
        raise ValueError("installation paths cannot contain newlines")
    if config_file.resolve().is_relative_to(install_root) or config_file in {bin_dir / "usdb-public", bin_dir / "usdb-explorer"}:
        raise ValueError("operator configuration must be outside release storage and the command entrypoint")
    if bin_dir.is_relative_to(install_root / "releases"):
        raise ValueError("command directory must be outside immutable release storage")
    current, launcher = install_root / "current", bin_dir / "usdb-public"
    expected_launcher = str(current / "usdb-public")
    explorer_launcher = bin_dir / "usdb-explorer"
    expected_explorer = str(current / "usdb-explorer")
    add_explorer = release_id.startswith("usdb-explorer-")
    if add_explorer and os.path.lexists(explorer_launcher) and (
            not explorer_launcher.is_symlink() or os.readlink(explorer_launcher) != expected_explorer):
        raise ValueError("usdb-explorer command already exists outside this installation; select another --bin-dir")
    had_launcher = os.path.lexists(launcher)
    if os.path.lexists(launcher) and (not launcher.is_symlink() or os.readlink(launcher) != expected_launcher):
        raise ValueError("usdb-public command already exists outside this installation; select another --bin-dir")
    if os.path.lexists(current) and (not current.is_symlink()
            or not re.fullmatch(r"releases/usdb-(?:public|explorer)-v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?", os.readlink(current))):
        raise ValueError("current path is not a managed public-service release link")
    if os.path.lexists(config_file) and not config_file.is_file():
        raise ValueError("configuration path exists but is not a regular file")
    install_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(install_root / ".install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another public-service installation is running") from None
        releases = install_root / "releases"
        if releases.is_symlink():
            raise ValueError("release storage cannot be a symlink")
        releases.mkdir(exist_ok=True, mode=0o700)
        destination = releases / release_id
        with tempfile.TemporaryDirectory(prefix=".install-", dir=install_root) as temporary:
            incoming = extract_release(archive, Path(temporary), release_id)
            expected_manifest = verify_release(incoming, release_id)
            if os.path.lexists(destination):
                verify_release(destination, release_id, expected_manifest)
            else:
                incoming.rename(destination)
        bin_dir.mkdir(parents=True, exist_ok=True)
        config_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(config_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass  # Existing operator settings survive installs and upgrades byte-for-byte.
        else:
            with os.fdopen(fd, "wb") as output:
                output.write((destination / "config.example.json").read_bytes())
        previous = os.readlink(current) if current.is_symlink() else None
        current_temporary = temporary_link("releases/" + release_id, current)
        launcher_temporary = None
        explorer_temporary = None
        try:
            launcher_temporary = temporary_link(expected_launcher, launcher)
            if add_explorer:
                explorer_temporary = temporary_link(expected_explorer, explorer_launcher)
            os.replace(current_temporary, current)
            try:
                os.replace(launcher_temporary, launcher)
                if explorer_temporary:
                    os.replace(explorer_temporary, explorer_launcher)
            except OSError:
                if not had_launcher:
                    launcher.unlink(missing_ok=True)
                if previous is None:
                    current.unlink()
                else:
                    rollback = temporary_link(previous, current)
                    os.replace(rollback, current)
                raise
        finally:
            current_temporary.unlink(missing_ok=True)
            if launcher_temporary:
                launcher_temporary.unlink(missing_ok=True)
            if explorer_temporary:
                explorer_temporary.unlink(missing_ok=True)
    finally:
        os.close(descriptor)
    print(f"Installed {release_id}: {destination}")
    primary_launcher = explorer_launcher if add_explorer else launcher
    print(f"Command: {primary_launcher}")
    if add_explorer:
        print(f"Compatibility command: {launcher}")
    print(f"Configuration (existing settings preserved): {config_file}")
    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        print("Add the command to this shell: export PATH=" + shlex.quote(str(bin_dir)) + ':"$PATH"')
    print("Edit the upstream RPC and ingress settings, then run:")
    print("  " + shlex.quote(str(primary_launcher)) + " prepare --config " + shlex.quote(str(config_file)))
    print("For an existing prepared deployment, use down, prepare --replace, then up.")
    print("Installation does not start or restart containers, or change node configuration.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--install-root", type=Path, default=Path.home() / ".local/share/usdb-public")
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    parser.add_argument("--config-file", type=Path, default=Path.home() / ".config/usdb-public/config.json")
    args = parser.parse_args()
    try:
        if sys.version_info < (3, 11) or platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
            raise ValueError("installer requires Linux amd64 and Python 3.11+")
        install(args.archive, args.release_id, args.expected_sha256, args.install_root, args.bin_dir, args.config_file)
        return 0
    except (OSError, ValueError, tarfile.TarError, KeyError, TypeError) as error:
        print(f"USDB public installation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

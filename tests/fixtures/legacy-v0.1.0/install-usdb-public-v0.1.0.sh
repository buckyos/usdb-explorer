#!/usr/bin/env bash
set -euo pipefail
umask 077

release_id=usdb-public-v0.1.0
archive_sha256=3ed799cd5e0592f7e55f66fd23f65d81c19f97fd35107e8bada679d9c4975c49
archive_url=https://github.com/buckyos/usdb/releases/download/usdb-public-v0.1.0/usdb-public-v0.1.0.tar.gz

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'USDB_PUBLIC_INSTALLER_HELP'
Install this pinned USDB public-service release for the current user.
  --install-root DIR  Version storage (default ~/.local/share/usdb-public)
  --bin-dir DIR       Command directory (default ~/.local/bin)
  --config-file FILE  Preserve or create configuration (default ~/.config/usdb-public/config.json)
Existing configuration, deployment data and running services are preserved.
USDB_PUBLIC_INSTALLER_HELP
  exit 0
fi

for command in python3 curl sha256sum mktemp; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "Required command is not installed: $command" >&2
    exit 1
  }
done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11+ is required")'
temporary="$(mktemp -d "${TMPDIR:-/tmp}/.usdb-public-installer.XXXXXX")"
trap 'rm -rf -- "$temporary"' EXIT
archive="$temporary/$release_id.tar.gz"
echo "Downloading $release_id"
curl --disable --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
  --tlsv1.2 --retry 3 --connect-timeout 30 --max-time 300 --max-filesize 16777216 \
  "$archive_url" --output "$archive"
printf '%s  %s\n' "$archive_sha256" "$archive" | sha256sum -c - >/dev/null

# Frozen arguments follow user options, so they cannot be replaced by runtime flags.
python3 - "$@" --archive "$archive" --release-id "$release_id" --expected-sha256 "$archive_sha256" <<'USDB_PUBLIC_INSTALLER_PY'
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

RELEASE_ID = re.compile(r"usdb-public-v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?")


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
            target.chmod(0o755 if member.name == release_id + "/usdb-public" else 0o644)
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
            or manifest.get("version") != release_id.removeprefix("usdb-public-v")
            or not isinstance(manifest.get("files"), dict)
            or not {"usdb-public", "usdb_public.py", "config.example.json", "assets/images.lock.json"}.issubset(manifest["files"])):
        raise ValueError("release manifest identity or required files are invalid")
    files = manifest["files"]
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
    if config_file.resolve().is_relative_to(install_root) or config_file == bin_dir / "usdb-public":
        raise ValueError("operator configuration must be outside release storage and the command entrypoint")
    if bin_dir.is_relative_to(install_root / "releases"):
        raise ValueError("command directory must be outside immutable release storage")
    current, launcher = install_root / "current", bin_dir / "usdb-public"
    expected_launcher = str(current / "usdb-public")
    if os.path.lexists(launcher) and (not launcher.is_symlink() or os.readlink(launcher) != expected_launcher):
        raise ValueError("usdb-public command already exists outside this installation; select another --bin-dir")
    if os.path.lexists(current) and (not current.is_symlink()
            or not re.fullmatch(r"releases/usdb-public-v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?", os.readlink(current))):
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
        try:
            launcher_temporary = temporary_link(expected_launcher, launcher)
            os.replace(current_temporary, current)
            try:
                os.replace(launcher_temporary, launcher)
            except OSError:
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
    finally:
        os.close(descriptor)
    print(f"Installed {release_id}: {destination}")
    print(f"Command: {launcher}")
    print(f"Configuration (existing settings preserved): {config_file}")
    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        print("Add the command to this shell: export PATH=" + shlex.quote(str(bin_dir)) + ':"$PATH"')
    print("Edit the upstream RPC and ingress settings, then run:")
    print("  " + shlex.quote(str(launcher)) + " prepare --config " + shlex.quote(str(config_file)))
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

USDB_PUBLIC_INSTALLER_PY

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
import stat
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


def guidance_metadata(path):
    """Read bounded local JSON for advice without exposing private values or parser errors."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("unsupported metadata file")
        data = source.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("metadata exceeds guidance limit")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    return value


def installation_guidance(launcher, config_file, state_dir, destination, *, created_config):
    """Choose post-install commands from local metadata only; never inspect or operate Docker."""
    def command(action, *, replace=False):
        args = [str(launcher), action]
        if replace:
            args.append("--replace")
        if action in {"setup", "prepare"}:
            args += ["--config", str(config_file)]
        return "  " + shlex.join([*args, "--state-dir", str(state_dir)])

    lines = [f"Deployment directory: {state_dir}",
             "Running services: not checked; installing the tool does not apply a deployment."]
    try:
        try:
            state_dir.lstat()
        except FileNotFoundError:
            label = "new configuration" if created_config else "existing configuration; not prepared here"
            lines += [f"Deployment: {label}", "Next steps (not executed):"]
            if (destination / "public_setup.py").is_file():
                lines += [command("setup")]
            else:
                lines += ["Review the upstream RPC and ingress settings in the configuration first."]
            lines += [command(action) for action in ("prepare", "preflight", "up", "check")]
            lines += ["If your deployment is elsewhere, rerun this installer with --state-dir pointing to it."]
            return lines
        if created_config:
            raise ValueError("source configuration was missing for an existing deployment")
        saved = guidance_metadata(state_dir / "deployment.json")
        required = {"compose.json", "config.json", "identity.json", "credentials.json", "network.json", "images.lock.json"}
        files = saved.get("files")
        if (saved.get("schema_version") != "usdb-public-deployment:v1" or saved.get("state_dir") != str(state_dir)
                or not isinstance(files, dict) or not required.issubset(files)
                or any(not (state_dir / name).is_file() for name in required)):
            raise ValueError("unrecognized prepared deployment")
        prepared = guidance_metadata(state_dir / "config.json")
        source = guidance_metadata(config_file)
        target = guidance_metadata(destination / "release.json")
        if (source.get("schema_version") != "usdb-public-config:v1"
                or prepared.get("schema_version") != "usdb-public-config:v1"
                or any(not isinstance(source.get(key), str) or not source[key]
                       or source[key] != prepared.get(key) for key in ("deployment_id", "network"))
                or "networks/" + source["network"] + ".json" not in target["files"]):
            raise ValueError("configuration and deployment identity require review")
        version = saved.get("release_version")
        if not isinstance(version, str) or not re.fullmatch(r"development|[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?", version):
            raise ValueError("unknown prepared release")
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        # Installation succeeded; missing/ambiguous advice must not turn it into an install failure.
        lines += ["Deployment: existing state requires review before setup or replacement.",
                  "Check the selected --state-dir, the original source configuration and deployment metadata; values were not printed.",
                  "Inspect the selected deployment (not executed):", command("status")]
        if created_config:
            lines += ["The source configuration was missing; restore its original settings before applying this release."]
        return lines
    lines += [f"Prepared release: {version}"]
    if version == target["version"]:
        lines += ["Deployment: prepared release already matches the installed tool; service state is not inferred.",
                  "Next steps (not executed):", command("status"), command("check"),
                  "Changed settings still require down, prepare --replace, preflight, up and check with these same paths."]
    else:
        lines += ["Deployment: existing prepared deployment; apply the installed release without rerunning setup.",
                  "Next steps (not executed):", command("down"), command("prepare", replace=True),
                  *[command(action) for action in ("preflight", "up", "check")],
                  "These commands retain deployment credentials and data volumes; down stops only Explorer."]
    return lines


def install(archive, release_id, expected_sha256, install_root, bin_dir, config_file, *, state_dir=None):
    """Install immutable version directories and atomically switch only the CLI pointer."""
    if RELEASE_ID.fullmatch(release_id) is None or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("invalid pinned installer identity")
    if sha256(archive) != expected_sha256:
        raise ValueError("release archive SHA-256 mismatch")
    install_root, bin_dir = install_root.expanduser().resolve(), bin_dir.expanduser().resolve()
    config_file = config_file.expanduser().absolute()
    state_dir = (state_dir if state_dir is not None else Path.home() / ".config/usdb-public/default").expanduser().resolve()
    if any(c in str(path) for path in (install_root, bin_dir, config_file, state_dir) for c in "\n\r"):
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
        created_config = False
        try:
            fd = os.open(config_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass  # Existing operator settings survive installs and upgrades byte-for-byte.
        else:
            created_config = True
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
    mode = "first installation" if previous is None else ("reinstallation" if previous == "releases/" + release_id else "version change")
    print(f"Tool installation: {mode}")
    print(f"Configuration ({'created defaults' if created_config else 'existing settings preserved'}): {config_file}")
    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        print("Add the command to this shell: export PATH=" + shlex.quote(str(bin_dir)) + ':"$PATH"')
    print("\n".join(installation_guidance(primary_launcher, config_file, state_dir, destination,
                                           created_config=created_config)))
    print("Installation does not start or restart containers, or change node configuration.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--install-root", type=Path, default=Path.home() / ".local/share/usdb-public")
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    parser.add_argument("--config-file", type=Path, default=Path.home() / ".config/usdb-public/config.json")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".config/usdb-public/default",
                        help="Deployment directory to inspect for post-install guidance; no deployment is changed")
    args = parser.parse_args()
    try:
        if sys.version_info < (3, 11) or platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
            raise ValueError("installer requires Linux amd64 and Python 3.11+")
        install(args.archive, args.release_id, args.expected_sha256, args.install_root, args.bin_dir, args.config_file,
                state_dir=args.state_dir)
        return 0
    except (OSError, ValueError, tarfile.TarError, KeyError, TypeError) as error:
        print(f"USDB public installation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

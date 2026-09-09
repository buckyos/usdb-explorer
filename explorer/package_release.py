#!/usr/bin/env python3
"""Build an independent public-service release; do not consume the USDB node kit."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

from public_config import DIGEST_IMAGE, image_lock
from network_contract import check_network
from release_installer import render_installer

FILES = ("usdb-explorer", "usdb-public", "usdb_public.py", "public_config.py", "public_checks.py", "config.example.json",
         "assets/images.lock.json", "networks/usdb-testnet-v0.json", "networks/usdb-testnet-v0.contract.json", "README.md")


def package(repo, output, version, gateway_image):
    """Package only an allowlist of public files and a digest-bound gateway image."""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?", version):
        raise ValueError("version must be an independent semantic version")
    if not DIGEST_IMAGE.fullmatch(gateway_image):
        raise ValueError("gateway-image must be pinned by digest")
    check_network(repo)
    source = repo / "explorer"
    lock = image_lock(source)
    lock["images"]["gateway"] = {"tag": "v" + version, "reference": gateway_image}
    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=repo, capture_output=True, text=True)
    revision = head.stdout.strip() if head.returncode == 0 else None
    dirty = revision is None or bool(subprocess.run(["git", "status", "--porcelain"],
                                cwd=repo, check=True, capture_output=True, text=True).stdout.strip())
    output.mkdir(parents=True, exist_ok=True)
    name = "usdb-explorer-v" + version
    archive = output / (name + ".tar.gz")
    checksum = output / (archive.name + ".sha256")
    installer = output / ("install-" + name + ".sh")
    installer_checksum = output / (installer.name + ".sha256")
    if any(path.exists() for path in (archive, checksum, installer, installer_checksum)):
        raise ValueError("release output already exists; immutable artifacts cannot be overwritten")
    with tempfile.TemporaryDirectory(prefix="usdb-public-package-") as temporary:
        root = Path(temporary) / name
        for relative in FILES:
            path = source / relative
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"missing release file: {relative}")
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(0o755 if relative in {"usdb-public", "usdb-explorer"} else 0o644)
        (root / "assets/images.lock.json").write_text(json.dumps(lock, indent=2) + "\n")
        hashes = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}
        (root / "release.json").write_text(json.dumps({"schema_version": "usdb-public-release:v1", "version": version,
            "source_repository": "buckyos/usdb-explorer", "source_revision": revision, "source_dirty": dirty, "config_schema": "usdb-public-config:v1",
            "upstream_contract": "USDB JSON-RPC with historical state and callTracer; identity checked at deployment",
            "qualified_for_public_exposure": lock["qualified_for_public_exposure"], "files": hashes}, indent=2) + "\n")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(root, arcname=name)
    checksum.write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n")
    installer.write_text(render_installer(source, archive, name))
    installer.chmod(0o755)
    installer_checksum.write_text(hashlib.sha256(installer.read_bytes()).hexdigest() + "  " + installer.name + "\n")
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version")
    parser.add_argument("--gateway-image")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--check-network", action="store_true")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parent.parent
    try:
        if args.check_network:
            check_network(repo)
            print("Explorer network catalog matches its pinned USDB contract")
        else:
            if not all((args.version, args.gateway_image, args.output_dir)):
                parser.error("--version, --gateway-image and --output-dir are required")
            print(package(repo, args.output_dir, args.version, args.gateway_image))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Public release packaging failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Prepare an annotated explorer tag in this repository, independently of node releases."""
import argparse
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "explorer"))
from network_contract import check_network
from release_notes import audit_before_tag


def git(repo, *args):
    """Run a checked Git operation in the selected explorer checkout."""
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def prepare(repo, version, *, create=False, push=False, fetch=True):
    """Validate a clean published main; tag creation and pushing require explicit flags."""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?", version):
        raise ValueError("expected an independent semantic version such as 0.2.0")
    if push and not create:
        raise ValueError("--push requires --create")
    if git(repo, "remote", "get-url", "origin") not in {
            "https://github.com/buckyos/usdb-explorer", "https://github.com/buckyos/usdb-explorer.git",
            "git@github.com:buckyos/usdb-explorer.git"}:
        raise ValueError("origin must be buckyos/usdb-explorer")
    if git(repo, "status", "--porcelain") or git(repo, "branch", "--show-current") != "main":
        raise ValueError("release preparation requires a clean explorer main")
    if fetch:
        git(repo, "fetch", "--prune", "origin")
    revision = git(repo, "rev-parse", "HEAD")
    if revision != git(repo, "rev-parse", "refs/remotes/origin/main"):
        raise ValueError("explorer main must match origin/main before creating a release tag")
    tag = "v" + version
    if git(repo, "tag", "--list", tag) or git(repo, "ls-remote", "--tags", "origin", "refs/tags/" + tag):
        raise ValueError("release tag already exists; never move or reuse it")
    check_network(repo)
    audit_before_tag(repo, tag, revision)
    print(f"repository=buckyos/usdb-explorer\nrelease_tag={tag}\nsource_revision={revision}")
    if not create:
        print("Preflight passed; use --create to create this annotated tag, and --push to publish the tag.")
        return
    git(repo, "tag", "-a", tag, revision, "-m", f"Freeze USDB Explorer {tag}\n\nSource: {revision}")
    if push:
        try:
            git(repo, "push", "origin", "refs/tags/" + tag)
        except subprocess.SubprocessError as error:
            raise ValueError(f"tag {tag} was created; preserve it and retry git push origin refs/tags/{tag}") from error
    print("Created " + tag + "; tag push starts release-build.yml, followed by manual release-publish.yml.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args()
    try:
        prepare(ROOT, args.version, create=args.create, push=args.push, fetch=not args.no_fetch)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Explorer release preparation failed: {error}\n")


if __name__ == "__main__":
    main()

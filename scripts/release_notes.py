#!/usr/bin/env python3
"""Generate immutable Explorer release changes from annotated tags and Git objects.

Fragment validation and trailer classification are adapted from USDB's
release_notes.py at 3344a4c316a3c69fe833b782718ff31440aa3d64.
No sibling repository is read at runtime.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

REPOSITORY = "buckyos/usdb-explorer"
FRAGMENT_SCHEMA_VERSION = "usdb-change-fragment:v1"
RELEASE_CHANGES_SCHEMA_VERSION = "usdb-explorer-release-changes:v1"
POLICY_PATH = ".release-notes/config.json"
POLICY = {"schema_version": "usdb-explorer-release-notes-policy:v1"}
ASSET_NAMES = {"release-changes.json", "release-changes.json.sha256", "release-changes.md"}
FRAGMENT_PATH_PREFIX = ".release-notes/fragments/"
CHANGE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RELEASE_ID_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?")
REVISION_RE = re.compile(r"[0-9a-f]{40}")
CHANGE_TYPE_TITLES = {"security": "Security", "removed": "Removed", "deprecated": "Deprecated",
                      "added": "Added", "changed": "Changed", "fixed": "Fixed", "internal": "Internal"}
CHANGE_TYPES = set(CHANGE_TYPE_TITLES)
CHANGE_TYPE_ORDER = tuple(CHANGE_TYPE_TITLES)
SCOPES = {"deployment", "documentation", "release", "security", "testing", "gateway", "explorer", "network"}
COMPATIBILITY_KEYS = {"config_change", "data_rebuild", "network_reset", "restart_required"}
ROOT = Path(__file__).resolve().parents[1]

def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    return load_json_bytes(path.read_bytes(), str(path))


def load_json_bytes(data: bytes, source: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_object_without_duplicates
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON document {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON document must be an object: {source}")
    return value


def canonical_json(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def require_exact_keys(value: dict[str, Any], expected: set[str], context: str) -> None:
    actual = set(value)
    require(
        actual == expected,
        f"{context} keys mismatch: missing={sorted(expected - actual)}, "
        f"unknown={sorted(actual - expected)}",
    )


def _require_text(value: Any, context: str, *, max_length: int = 1000) -> str:
    require(isinstance(value, str), f"{context} must be a string")
    normalized = value.strip()
    require(normalized == value and normalized != "", f"{context} must be trimmed and non-empty")
    require("\n" not in value and "\r" not in value, f"{context} must be one line")
    require(len(value) <= max_length, f"{context} exceeds {max_length} characters")
    return value


def _require_text_array(
    value: Any,
    context: str,
    *,
    allow_empty: bool,
    max_items: int = 12,
    max_length: int = 1000,
) -> list[str]:
    require(isinstance(value, list), f"{context} must be an array")
    if not allow_empty:
        require(len(value) > 0, f"{context} must not be empty")
    require(len(value) <= max_items, f"{context} exceeds {max_items} items")
    result = [
        _require_text(item, f"{context}[{index}]", max_length=max_length)
        for index, item in enumerate(value)
    ]
    require(len(result) == len(set(result)), f"{context} must not contain duplicates")
    return result


def validate_fragment(value: dict[str, Any], source: str) -> dict[str, Any]:
    require_exact_keys(
        value,
        {
            "change_id",
            "compatibility",
            "details",
            "operator_actions",
            "references",
            "schema_version",
            "scopes",
            "summary",
            "type",
        },
        source,
    )
    require(
        value["schema_version"] == FRAGMENT_SCHEMA_VERSION,
        f"{source}.schema_version must be {FRAGMENT_SCHEMA_VERSION}",
    )
    change_id = _require_text(value["change_id"], f"{source}.change_id", max_length=96)
    require(
        CHANGE_ID_RE.fullmatch(change_id) is not None,
        f"{source}.change_id must be lowercase kebab-case",
    )
    change_type = _require_text(value["type"], f"{source}.type", max_length=24)
    require(change_type in CHANGE_TYPES, f"{source}.type is unsupported")
    scopes = _require_text_array(
        value["scopes"], f"{source}.scopes", allow_empty=False, max_items=8, max_length=40
    )
    require(set(scopes) <= SCOPES, f"{source}.scopes contains an unsupported scope")
    summary = _require_text(value["summary"], f"{source}.summary", max_length=160)
    details = _require_text_array(
        value["details"], f"{source}.details", allow_empty=False, max_items=12
    )
    operator_actions = _require_text_array(
        value["operator_actions"],
        f"{source}.operator_actions",
        allow_empty=True,
        max_items=8,
    )
    references = _require_text_array(
        value["references"],
        f"{source}.references",
        allow_empty=True,
        max_items=12,
        max_length=500,
    )
    for index, reference in enumerate(references):
        require(
            reference.startswith("https://"),
            f"{source}.references[{index}] must be an HTTPS URL",
        )
    compatibility = value["compatibility"]
    require(isinstance(compatibility, dict), f"{source}.compatibility must be an object")
    require_exact_keys(compatibility, COMPATIBILITY_KEYS, f"{source}.compatibility")
    for key, enabled in compatibility.items():
        require(type(enabled) is bool, f"{source}.compatibility.{key} must be boolean")
    require(
        not (compatibility["network_reset"] and compatibility["data_rebuild"]),
        f"{source}.compatibility.data_rebuild is redundant when network_reset is true",
    )
    return {
        "schema_version": FRAGMENT_SCHEMA_VERSION,
        "change_id": change_id,
        "type": change_type,
        "scopes": sorted(scopes),
        "summary": summary,
        "details": details,
        "operator_actions": operator_actions,
        "compatibility": {key: compatibility[key] for key in sorted(COMPATIBILITY_KEYS)},
        "references": sorted(references),
    }


def validate_fragment_directory(repository_root: Path) -> list[dict[str, Any]]:
    fragment_dir = repository_root / FRAGMENT_PATH_PREFIX
    if not fragment_dir.exists():
        return []
    require(fragment_dir.is_dir(), f"fragment path is not a directory: {fragment_dir}")
    fragments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(fragment_dir.iterdir()):
        require(path.is_file() and not path.is_symlink(), f"fragment must be a regular file: {path}")
        require(path.suffix == ".json", f"fragment must use .json: {path}")
        value = validate_fragment(load_json(path), str(path))
        require(path.stem == value["change_id"], f"fragment file name must equal change_id: {path}")
        require(value["change_id"] not in seen, f"duplicate change_id: {value['change_id']}")
        seen.add(value["change_id"])
        fragments.append(value)
    return fragments


def _classify_release_notes(
    release_notes: list[str], known_change_ids: set[str], context: str
) -> str:
    require(
        len(release_notes) == len(set(release_notes)),
        f"{context} has duplicate Release-Note trailers",
    )
    require(
        "none" not in release_notes or release_notes == ["none"],
        f"{context} mixes Release-Note: none with change IDs",
    )
    if release_notes == ["none"]:
        return "exempt"
    if release_notes and all(note in known_change_ids for note in release_notes):
        return "classified"
    return "unclassified"


def git(repo, *arguments):
    """Read bounded Git evidence without checking out or executing release code."""
    result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True,
                            text=True, timeout=30)
    if result.returncode:
        raise ValueError(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout


def version_key(tag):
    """Order supported release tags, including numeric prerelease identifiers."""
    require(isinstance(tag, str) and RELEASE_ID_RE.fullmatch(tag) is not None, "expected a vX.Y.Z release tag")
    version, separator, suffix = tag[1:].partition("-")
    identifiers = tuple((0, int(part)) if part.isdigit() else (1, part) for part in suffix.split("."))
    return (*map(int, version.split(".")), not separator, identifiers if separator else ())


def tag_identity(repo, tag):
    """Resolve an annotated release tag to its immutable tag object and commit."""
    version_key(tag)
    ref = "refs/tags/" + tag
    require(git(repo, "cat-file", "-t", ref).strip() == "tag", "release tag must be annotated")
    return {"release_id": tag, "tag_object": git(repo, "rev-parse", ref).strip(),
            "source_revision": git(repo, "rev-parse", ref + "^{commit}").strip()}


def requires_notes(repo, revision):
    """Use tagged policy to distinguish legacy four-asset releases from new releases."""
    require(REVISION_RE.fullmatch(revision) is not None, "invalid source revision")
    entry = git(repo, "ls-tree", revision, "--", POLICY_PATH).strip()
    if not entry:
        return False
    require(entry.startswith("100644 blob "), "release notes policy must be a regular file")
    require(load_json_bytes(git(repo, "show", f"{revision}:{POLICY_PATH}").encode(), POLICY_PATH) == POLICY,
            "unsupported release notes policy")
    return True


def previous_published(api, repo, release_id, *, published_before=None):
    """Select the highest lower published version; drafts never establish a boundary."""
    candidates = [release for page in api.json("releases?per_page=100", paginate=True) for release in page
                  if release.get("draft") is False and release.get("published_at")
                  and (published_before is None or release["published_at"] <= published_before)
                  and RELEASE_ID_RE.fullmatch(release.get("tag_name", ""))
                  and version_key(release["tag_name"]) < version_key(release_id)]
    if not candidates:
        return None
    previous = max(candidates, key=lambda release: version_key(release["tag_name"]))
    identity = tag_identity(repo, previous["tag_name"])
    remote = api.json("git/ref/tags/" + previous["tag_name"])["object"]
    require(remote.get("type") == "tag" and remote.get("sha") == identity["tag_object"],
            "previous published tag differs from local annotated tag")
    return identity


def fragments_at(repo, revision):
    """Validate every fragment from committed blobs, including file modes and names."""
    fragments = {}
    for entry in git(repo, "ls-tree", "-rz", revision, "--", FRAGMENT_PATH_PREFIX).split("\0"):
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        require(metadata.startswith("100644 blob ") and Path(path).parent.as_posix() == FRAGMENT_PATH_PREFIX.rstrip("/")
                and path.endswith(".json"), f"fragment must be a regular top-level JSON file: {path}")
        raw = git(repo, "show", f"{revision}:{path}").encode()
        fragment = validate_fragment(load_json_bytes(raw, path), path)
        require(Path(path).stem == fragment["change_id"], f"fragment file name must equal change_id: {path}")
        fragments[path] = (raw, fragment)
    return fragments


def commit_records(repo, revision, previous_revision, known_ids):
    """Keep the complete commit inventory; missing trailers remain report-only."""
    revision_range = f"{previous_revision}..{revision}" if previous_revision else revision
    output = git(repo, "log", "--reverse", "--format=%H%x1f%s%x1f%b%x1e", revision_range)
    records = []
    for raw in output.split("\x1e"):
        if not raw.strip("\n"):
            continue
        sha, subject, body = raw.strip("\n").split("\x1f", 2)
        notes = re.findall(r"^Release-Note:\s*([^\s]+)\s*$", body, re.MULTILINE)
        records.append({"revision": sha, "subject": subject, "release_notes": notes,
                        "classification": _classify_release_notes(notes, known_ids, f"commit {sha}")})
    return records


def source_inputs(repo, revision):
    """Read network, config, image and gateway identities without importing source code."""
    def read(path):
        return git(repo, "show", f"{revision}:{path}")

    def constant(path, name):
        tree = ast.parse(read(path))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                value = ast.literal_eval(node.value)
                require(isinstance(value, str), f"{path}:{name} must be a string")
                return value
        raise ValueError(f"missing compatibility constant: {path}:{name}")

    catalog = load_json_bytes(read("explorer/networks/usdb-testnet-v0.json").encode(), "network catalog")
    identity_keys = ("bundle_id", "chain_id", "network_id", "genesis_block_hash", "btc_network_id",
                     "btc_index_origin_height", "btc_activation_registry_id")
    contract = load_json_bytes(read("explorer/networks/usdb-testnet-v0.contract.json").encode(), "RPC contract")
    lock = load_json_bytes(read("explorer/assets/images.lock.json").encode(), "image lock")
    gateway = git(repo, "ls-tree", "-r", revision, "--", "gateway", "explorer/assets/Dockerfile.gateway")
    return {"network": {key: catalog[key] for key in identity_keys},
            "rpc_contract": contract["rpc"],
            "config_schema": constant("explorer/public_config.py", "CONFIG_SCHEMA"),
            "deployment_schema": constant("explorer/usdb_public.py", "DEPLOYMENT_SCHEMA"),
            "images": {key: value["reference"] for key, value in sorted(lock["images"].items())},
            "gateway_source_sha256": sha256_bytes(gateway.encode())}


def source_changes(previous, current):
    """Derive conservative upgrade flags independently of fragment declarations."""
    if previous is None:
        return []
    tracked = [("network", "network_reset"), ("rpc_contract", "config_change"),
               ("config_schema", "config_change"), ("deployment_schema", "config_change"),
               ("gateway_source_sha256", "restart_required")]
    changes = [{"path": key, "previous": previous[key], "current": current[key], "impact": impact}
               for key, impact in tracked if previous[key] != current[key]]
    for name in sorted(set(previous["images"]) | set(current["images"])):
        before, after = previous["images"].get(name), current["images"].get(name)
        if before != after:
            # The Go stage supplies both the gateway binary and runtime CA certificates.
            changes.append({"path": "images." + name, "previous": before, "current": after,
                            "impact": "restart_required"})
    return changes


def range_changes(repo, revision, previous_revision=None):
    """Reject published fragment mutations and classify the selected commit range."""
    require(REVISION_RE.fullmatch(revision) is not None, "invalid current revision")
    if previous_revision:
        require(REVISION_RE.fullmatch(previous_revision) is not None, "invalid previous revision")
        git(repo, "merge-base", "--is-ancestor", previous_revision, revision)
    before = fragments_at(repo, previous_revision) if previous_revision else {}
    after = fragments_at(repo, revision)
    for path, (raw, _) in before.items():
        require(path in after and after[path][0] == raw, f"published fragments are append-only: {path}")
    changes = [{**fragment, "source_path": path} for path, (_, fragment) in after.items() if path not in before]
    changes.sort(key=lambda item: (CHANGE_TYPE_ORDER.index(item["type"]), item["change_id"]))
    commits = commit_records(repo, revision, previous_revision, {change["change_id"] for change in changes})
    return changes, commits


def audit_before_tag(repo, release_id, revision):
    """Review the published boundary before creating a tag; coverage stays report-only."""
    sys.path.insert(0, str(ROOT / "explorer"))
    from publish_release import GitHub
    validate_fragment_directory(repo)
    require(requires_notes(repo, revision), "release notes policy must be committed before tagging")
    previous = previous_published(GitHub(), repo, release_id)
    changes, commits = range_changes(repo, revision, previous["source_revision"] if previous else None)
    unclassified = [commit for commit in commits if commit["classification"] == "unclassified"]
    print(f"Release notes: {len(changes)} fragments, {len(unclassified)} unclassified commits (report-only).")
    for commit in unclassified:
        print(f"  unclassified {commit['revision'][:12]} {commit['subject']}")
    return previous


def build_changes(repo, release_id, gateway_image, *, previous=None):
    """Freeze one source range, fragments, coverage and independently compared inputs."""
    current = tag_identity(repo, release_id)
    revision = current["source_revision"]
    require(requires_notes(repo, revision), "tag does not enable structured release notes")
    require(re.fullmatch(r"ghcr\.io/buckyos/usdb-explorer-gateway@sha256:[0-9a-f]{64}", gateway_image) is not None,
            "gateway image must be pinned to an Explorer digest")
    previous_revision = None
    if previous is not None:
        require(isinstance(previous, dict), "previous release must be an identity object")
        require_exact_keys(previous, {"release_id", "tag_object", "source_revision"}, "previous release")
        require(version_key(previous["release_id"]) < version_key(release_id), "previous release must have a lower version")
        require(tag_identity(repo, previous["release_id"]) == previous, "previous release tag changed")
        previous_revision = previous["source_revision"]
    changes, commits = range_changes(repo, revision, previous_revision)
    inputs = source_inputs(repo, revision)
    prior_inputs = source_inputs(repo, previous_revision) if previous_revision else None
    evidence = source_changes(prior_inputs, inputs)
    flags = {key: any(change["compatibility"][key] for change in changes)
             or any(change["impact"] == key for change in evidence) for key in sorted(COMPATIBILITY_KEYS)}
    if flags["network_reset"]:
        flags["data_rebuild"] = False
    classification = next((key for key in ("network_reset", "data_rebuild", "config_change", "restart_required") if flags[key]), "in_place")
    compare_url = f"https://github.com/{REPOSITORY}/compare/{previous_revision}...{revision}" if previous_revision else f"https://github.com/{REPOSITORY}/tree/{revision}"
    return {"schema_version": RELEASE_CHANGES_SCHEMA_VERSION, "repository": REPOSITORY,
            "release_id": release_id, "source_revision": revision, "previous_release": previous,
            "gateway_image": gateway_image, "source_inputs": inputs, "previous_source_inputs": prior_inputs,
            "changes": changes, "coverage_enforced": False,
            "compatibility": {"classification": classification, "flags": flags, "source_changes": evidence,
                              "operator_actions": sorted({action for change in changes for action in change["operator_actions"]})},
            "compare_url": compare_url, "commits": commits,
            "coverage": {status: sum(commit["classification"] == status for commit in commits)
                         for status in ("classified", "exempt", "unclassified")}}


def render_markdown(changes):
    """Render operator effects first, with source evidence and the full inventory below."""
    previous = changes["previous_release"]
    label = "相对 " + previous["release_id"] if previous else "首次发布，无前版"
    compatibility = changes["compatibility"]
    lines = [f"## 本次变更（{label}）", "", f"升级分类：`{compatibility['classification']}`。", "",
             "兼容性标记：" + "，".join(f"`{key}={str(value).lower()}`" for key, value in compatibility["flags"].items()) + "。", "",
             "### 升级操作", ""]
    lines += [f"- {action}" for action in compatibility["operator_actions"]] or ["未声明本版专属的手工操作；仍需核对下方自动检测的兼容性变化。"]
    lines.append("")
    if not changes["changes"]:
        lines += ["此范围未新增结构化变更条目；完整提交清单见下方。", ""]
    for kind in CHANGE_TYPE_ORDER:
        selected = [change for change in changes["changes"] if change["type"] == kind]
        if not selected:
            continue
        lines += ["### " + CHANGE_TYPE_TITLES[kind], ""]
        for change in selected:
            url = f"https://github.com/{REPOSITORY}/blob/{changes['source_revision']}/{change['source_path']}"
            lines += [f"#### {change['summary']}", ""]
            lines += [f"- {detail}" for detail in change["details"]]
            lines += [f"- 范围：`{', '.join(change['scopes'])}`；[变更条目]({url})"]
            lines += [f"- 参考：{url}" for url in change["references"]]
            lines.append("")
    lines += ["### 兼容性证据", ""]
    for change in compatibility["source_changes"]:
        before, after = (json.dumps(change[key], ensure_ascii=False, sort_keys=True) for key in ("previous", "current"))
        lines.append(f"- `{change['path']}`（`{change['impact']}`）：`{before}` → `{after}`")
    if not compatibility["source_changes"]:
        lines.append("未检测到已跟踪输入的变化。" if previous else "尚无已发布版本，无法进行前后兼容性比较。")
    coverage = changes["coverage"]
    lines += ["", f"[源码范围]({changes['compare_url']})：{len(changes['commits'])} 个提交，"
              f"{coverage['classified']} 个已分类，{coverage['exempt']} 个免记，{coverage['unclassified']} 个未分类。", "",
              "> 提交覆盖率为 report-only；发布 review 须逐项检查未分类提交。", "",
              "<details>", "<summary>完整提交清单</summary>", ""]
    for commit in changes["commits"]:
        url = f"https://github.com/{REPOSITORY}/commit/{commit['revision']}"
        notes = ", ".join(commit["release_notes"]) or "missing"
        lines.append(f"- [{commit['revision'][:12]}]({url}) {commit['subject']}（{commit['classification']}；Release-Note: {notes}）")
    return "\n".join(lines + ["", "</details>", ""])


def render_release_notes(changes):
    """Build the exact Release body from the frozen change record, without mutable text."""
    tag, revision = changes["release_id"], changes["source_revision"]
    docs = f"https://github.com/{REPOSITORY}/blob/{revision}"
    command = f"bash <(curl -fsSL https://github.com/{REPOSITORY}/releases/download/{tag}/install-usdb-explorer-{tag}.sh)"
    return (f"# USDB Explorer {tag}\n\n" + render_markdown(changes)
            + f"\n## 安装本版本\n\n草稿附件在完成 Publish 后才可下载。\n\n```bash\n{command}\n```\n\n"
            + f"[部署与升级说明]({docs}/explorer/README.md)。\n\n"
            + f"## 构建与安全\n\n源码：`{revision}`。\n\nGateway：`{changes['gateway_image']}`。\n\n"
            + f"测试网镜像漏洞检查为 report-only，扫描与证据错误仍阻断发布；[策略与验收边界]({docs}/docs/image-security.md)。\n")


def write_release_files(changes, output_dir, notes_path):
    """Write the three immutable change assets and the matching GitHub Release body."""
    output_dir.mkdir(parents=True, exist_ok=True)
    data = canonical_json(changes)
    paths = {output_dir / "release-changes.json": data,
             output_dir / "release-changes.json.sha256": (sha256_bytes(data) + "  release-changes.json\n").encode(),
             output_dir / "release-changes.md": render_markdown(changes).encode(),
             notes_path: render_release_notes(changes).encode()}
    require(not any(path.exists() for path in paths), "release notes output already exists; preserve immutable outputs")
    for path, payload in paths.items():
        path.write_bytes(payload)


def validate_release_files(repo, release_id, gateway_image, directory, body, *, expected_previous=...):
    """Regenerate all evidence from its frozen baseline; rehashing edited notes cannot pass."""
    path = directory / "release-changes.json"
    changes = load_json(path)
    require(changes.get("schema_version") == RELEASE_CHANGES_SCHEMA_VERSION, "unsupported release changes schema")
    if expected_previous is not ...:
        require(changes["previous_release"] == expected_previous, "release changes have the wrong published boundary for this build")
    expected = build_changes(repo, release_id, gateway_image, previous=changes["previous_release"])
    require(path.read_bytes() == canonical_json(expected), "release changes differ from frozen source evidence")
    require((directory / "release-changes.json.sha256").read_bytes() == (sha256_file(path) + "  release-changes.json\n").encode(),
            "release changes checksum mismatch")
    require((directory / "release-changes.md").read_bytes() == render_markdown(expected).encode(), "release changes Markdown mismatch")
    require(body == render_release_notes(expected), "release notes body differs from frozen source evidence")
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-fragments")
    validate.add_argument("--repository-root", type=Path, default=ROOT)
    for name in ("generate", "validate-release"):
        command = commands.add_parser(name)
        command.add_argument("--repository-root", type=Path, default=ROOT)
        command.add_argument("--release-id", required=True)
        command.add_argument("--gateway-image", required=True)
        command.add_argument("--output-dir", type=Path, required=True)
        command.add_argument("--notes", type=Path, required=True)
        if name == "generate":
            command.add_argument("--previous-release", default="auto", help="auto (published GitHub boundary), none, or an explicit tag for offline review")
    args = parser.parse_args()
    try:
        if args.command == "validate-fragments":
            fragments = validate_fragment_directory(args.repository_root)
            print(f"Validated {len(fragments)} Explorer release fragments")
        elif args.command == "generate":
            if args.previous_release == "auto":
                sys.path.insert(0, str(ROOT / "explorer"))
                from publish_release import GitHub
                api = GitHub()
                # Use the original build start on retries, not a later publication date.
                cutoff = None
                if run_id := os.environ.get("GITHUB_RUN_ID"):
                    require(run_id.isdigit(), "invalid GitHub build run ID")
                    run = api.json("actions/runs/" + run_id)
                    require(run.get("head_sha") == tag_identity(args.repository_root, args.release_id)["source_revision"]
                            and run.get("event") == "push" and run.get("head_branch") == args.release_id,
                            "release notes source differs from the tag build")
                    cutoff = run["created_at"]
                previous = previous_published(api, args.repository_root, args.release_id, published_before=cutoff)
            else:
                previous = None if args.previous_release == "none" else tag_identity(args.repository_root, args.previous_release)
            changes = build_changes(args.repository_root, args.release_id, args.gateway_image, previous=previous)
            write_release_files(changes, args.output_dir, args.notes)
            print(json.dumps({"release_id": args.release_id, "previous_release": previous, "coverage": changes["coverage"]}))
        else:
            validate_release_files(args.repository_root, args.release_id, args.gateway_image, args.output_dir, args.notes.read_text())
            print("Release changes, checksums, Markdown and body match frozen source")
    except (OSError, ValueError, KeyError, TypeError, SyntaxError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Explorer release notes failed: {error}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

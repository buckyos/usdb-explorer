#!/usr/bin/env python3
"""Plan independent digest scans and retain evidence before applying optional enforcement."""
from collections import Counter
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "explorer"))
from network_contract import check_network
from public_config import DIGEST_IMAGE, image_lock, read_json, security_enforcement

SEVERITIES = {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
GATEWAY = re.compile(r"ghcr\.io/buckyos/usdb-explorer-gateway@sha256:[0-9a-f]{64}")


def require(condition, message):
    """Fail on incomplete evidence even when vulnerability findings are advisory."""
    if not condition:
        raise ValueError(message)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def scan_plan(repo, gateway_image, enforcement=None):
    """Scan the gateway, all locked runtime images and the frozen Go build image."""
    require(GATEWAY.fullmatch(gateway_image), "expected an immutable usdb-explorer-gateway image")
    check_network(repo)
    network = read_json(repo / "explorer/networks/usdb-testnet-v0.json")["bundle_id"]
    mode = security_enforcement(network, enforcement)
    images = image_lock(repo / "explorer")["images"]
    images["gateway"] = {"reference": gateway_image}
    return {"include": [{"name": name, "image_reference": item["reference"], "enforcement": mode}
                        for name, item in sorted(images.items())]}


def scan_input(repo, name, reference, revision, enforcement):
    """Bind third-party images to the lock commit and first-party images to their build commit."""
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "expected an exact Explorer source commit")
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    require(head == revision, "scan checkout differs from requested source revision")
    for path in ("explorer/assets/images.lock.json", "explorer/networks/usdb-testnet-v0.json",
                 "explorer/networks/usdb-testnet-v0.contract.json"):
        committed = subprocess.check_output(["git", "-C", str(repo), "show", f"{revision}:{path}"])
        require((repo / path).read_bytes() == committed, "scan inputs differ from the selected source commit")
    gateway = reference if name == "gateway" else "ghcr.io/buckyos/usdb-explorer-gateway@sha256:" + "0" * 64
    plan = scan_plan(repo, gateway, enforcement)
    require(any(row["name"] == name and row["image_reference"] == reference for row in plan["include"]),
            "scan image differs from the selected source lock")
    return {"name": name, "image_reference": reference, "platform": "linux/amd64",
            "enforcement": enforcement, "network": "usdb-testnet-v0",
            "explorer_source_revision": revision,
            "image_source_revision": revision if name == "gateway" else None,
            "image_lock_sha256": sha256(repo / "explorer/assets/images.lock.json"),
            "network_contract_sha256": sha256(repo / "explorer/networks/usdb-testnet-v0.contract.json")}


def canonical_reference(reference):
    """Accept Docker Hub's standard short names without accepting a different repository/digest."""
    require(isinstance(reference, str) and DIGEST_IMAGE.fullmatch(reference), "invalid report image digest")
    name, digest = reference.split("@")
    if name.startswith("index.docker.io/"):
        name = name.removeprefix("index.")
    first = name.split("/")[0]
    if "." not in first:
        name = "docker.io/" + ("library/" if "/" not in name else "") + name
    if name.startswith("docker.io/") and name.count("/") == 1:
        name = name.replace("docker.io/", "docker.io/library/", 1)
    return name + "@" + digest


def evaluate(report, identity):
    """Keep every finding unresolved; this project has no inherited node-image exceptions."""
    mode = security_enforcement(identity["network"], identity["enforcement"])
    require(isinstance(report, dict) and report.get("SchemaVersion") == 2 and report.get("ArtifactType") == "container_image",
            "invalid Trivy container report")
    metadata = report.get("Metadata", {})
    require(isinstance(metadata, dict) and isinstance(metadata.get("RepoDigests"), list), "invalid Trivy image metadata")
    expected = canonical_reference(identity["image_reference"])
    require(canonical_reference(report.get("ArtifactName")) == expected and expected in
            [canonical_reference(value) for value in metadata.get("RepoDigests", [])], "report image digest mismatch")
    config = metadata.get("ImageConfig", {})
    require(isinstance(config, dict), "invalid image configuration")
    require(config.get("os") == "linux" and config.get("architecture") == "amd64", "report platform mismatch")
    if identity["image_source_revision"] is not None:
        labels = config.get("config", {}).get("Labels", {})
        require(labels.get("org.opencontainers.image.revision") == identity["image_source_revision"],
                "gateway image source revision mismatch")
    targets = report.get("Results")
    require(isinstance(targets, list) and targets, "report has no scan targets")
    counts, unresolved = Counter({level: 0 for level in SEVERITIES}), []
    gateway_covered = False
    for target in targets:
        require(isinstance(target, dict) and all(isinstance(target.get(key), str) and target[key]
                for key in ("Target", "Class", "Type")), "malformed scan target")
        gateway_covered |= (target["Class"] == "lang-pkgs" and target["Type"] == "gobinary"
                            and target["Target"].lstrip("/") == "gateway")
        findings = target.get("Vulnerabilities", [])
        require(isinstance(findings, list), "malformed vulnerabilities array")
        for finding in findings:
            require(isinstance(finding, dict) and all(isinstance(finding.get(key), str) and finding[key]
                    for key in ("VulnerabilityID", "PkgName", "InstalledVersion", "Severity")), "malformed vulnerability")
            level = finding["Severity"]
            require(level in SEVERITIES, "unknown vulnerability severity")
            counts[level] += 1
            if level in {"HIGH", "CRITICAL"}:
                unresolved.append({"target": target["Target"], "advisory_id": finding["VulnerabilityID"],
                                   "package": finding["PkgName"], "version": finding["InstalledVersion"],
                                   "severity": level, "fixed_version": finding.get("FixedVersion", "")})
    require(identity["name"] != "gateway" or gateway_covered, "gateway Go binary coverage is missing")
    return {"enforcement": mode, "raw_counts": dict(sorted(counts.items())), "accepted_count": 0,
            "unresolved_count": len(unresolved), "unresolved": unresolved,
            "review_result": "findings" if unresolved else "clean",
            "result": "fail" if mode == "strict" and unresolved else "pass"}


def write_evidence(directory, identity, scanner_version):
    """Bind one JSON scan and its SARIF conversion, including mode and source semantics."""
    decision = evaluate(read_json(directory / "trivy-image.json"), identity)
    sarif = read_json(directory / "trivy-image.sarif")
    require(isinstance(sarif, dict) and sarif.get("version") == "2.1.0" and isinstance(sarif.get("runs"), list) and sarif["runs"],
            "invalid SARIF evidence")
    for run in sarif["runs"]:
        require(isinstance(run, dict) and run.get("tool", {}).get("driver", {}).get("name") == "Trivy"
                and isinstance(run.get("results"), list), "invalid Trivy SARIF run")
    require(sum(len(run["results"]) for run in sarif["runs"]) == decision["unresolved_count"],
            "SARIF High/Critical results differ from the JSON scan")
    require(scanner_version.startswith("Version: 0.74.0"), "unexpected Trivy version")
    metadata = {"schema_version": "usdb-explorer-image-security:v1", **identity, "policy": decision,
                "scanner": scanner_version, "generated_at": datetime.now(timezone.utc).isoformat(),
                "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
                "workflow_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                "reports": {name: sha256(directory / name) for name in ("trivy-image.json", "trivy-image.sarif")}}
    (directory / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    names = ("scan-input.json", "trivy-image.json", "trivy-image.sarif", "metadata.json")
    (directory / "SHA256SUMS").write_text("".join(sha256(directory / name) + "  " + name + "\n" for name in names))
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--gateway-image", required=True)
    plan.add_argument("--enforcement", choices=("strict", "report-only"))
    validate = sub.add_parser("validate")
    validate.add_argument("--name", required=True)
    validate.add_argument("--image-reference", required=True)
    validate.add_argument("--source-revision", required=True)
    validate.add_argument("--enforcement", choices=("strict", "report-only"), required=True)
    validate.add_argument("--output-dir", type=Path, required=True)
    evidence = sub.add_parser("evidence")
    evidence.add_argument("--directory", type=Path, required=True)
    evidence.add_argument("--scanner-version", required=True)
    enforce = sub.add_parser("enforce")
    enforce.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            value = json.dumps(scan_plan(ROOT, args.gateway_image, args.enforcement), separators=(",", ":"))
            print(value)
            if output := os.environ.get("GITHUB_OUTPUT"):
                with open(output, "a") as destination:
                    destination.write("matrix=" + value + "\n")
        elif args.command == "validate":
            value = scan_input(ROOT, args.name, args.image_reference, args.source_revision, args.enforcement)
            args.output_dir.mkdir(parents=True, exist_ok=True)
            (args.output_dir / "scan-input.json").write_text(json.dumps(value, indent=2) + "\n")
        elif args.command == "evidence":
            value = write_evidence(args.directory, read_json(args.directory / "scan-input.json"), args.scanner_version)
            print(json.dumps({key: value["policy"][key] for key in
                              ("enforcement", "raw_counts", "accepted_count", "unresolved_count", "result")}, indent=2))
            if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
                with open(summary, "a") as destination:
                    destination.write(f"\nImage: `{value['image_reference']}`\n\nMode: `{value['enforcement']}`; "
                                      f"unresolved High/Critical: {value['policy']['unresolved_count']}; "
                                      f"result: {value['policy']['result']}. No image qualification is granted.\n")
        else:
            subprocess.run(["sha256sum", "--check", "SHA256SUMS"], cwd=args.directory, check=True)
            identity = read_json(args.directory / "scan-input.json")
            decision = evaluate(read_json(args.directory / "trivy-image.json"), identity)
            return int(decision["result"] != "pass")
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"Explorer image security failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

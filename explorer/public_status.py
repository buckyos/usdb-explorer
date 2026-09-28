"""Read-only version/status reports and their terminal presentation."""
from __future__ import annotations

import json

from public_config import read_json


def tool_identity(kit, version):
    """Expose only public identity fields from an already verified release kit."""
    manifest = read_json(kit / "release.json") if version != "development" else {}
    return {"version": version, "source_revision": manifest.get("source_revision"),
            "source_dirty": manifest.get("source_dirty"), "release_dir": str(kit)}


def status_report(tool, root, deployment=None, config=None, identity=None):
    """Keep prepared configuration distinct from Docker observations and CLI identity."""
    report = {"schema_version": "usdb-explorer-status:v1", "tool": tool,
              "state_dir": str(root), "status": "NOT_PREPARED", "deployment": None,
              "version_relation": "unknown", "containers": []}
    if deployment is None:
        return report
    version = deployment.get("release_version")
    report["deployment"] = {
        "release_version": version, "deployment_id": config["deployment_id"],
        "network": config["network"], "chain_id": identity["chain_id"],
        "genesis_block_hash": identity["genesis_block_hash"],
        "rpc_mode": config["rpc"].get("mode", "external"),
        "ingress_mode": config["ingress"]["mode"], "explorer_url": config["ingress"]["explorer_url"],
        "faucet_enabled": config.get("faucet", {}).get("enabled", False),
    }
    if version and tool["version"] != "development" and version != "development":
        report["version_relation"] = "same" if version == tool["version"] else "different"
    report["status"] = "OBSERVED"
    return report


def container_observations(output):
    """Accept Compose's JSON array and newline-delimited objects without exposing commands."""
    output = output.strip()
    if not output:
        return []
    try:
        rows = json.loads(output) if output.startswith("[") else [json.loads(line) for line in output.splitlines() if line.strip()]
    except ValueError:
        raise ValueError("Docker Compose returned invalid status JSON") from None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("Docker Compose returned invalid status rows")
    fields = {"Name": "name", "Service": "service", "Image": "image", "State": "state",
              "Status": "status", "Health": "health"}
    result = []
    for row in rows:
        if not isinstance(row.get("Service"), str) or not isinstance(row.get("State"), str):
            raise ValueError("Docker Compose status is missing service or state")
        observation = {target: str(row.get(source) or "") for source, target in fields.items()}
        observation["published_ports"] = []
        for port in row.get("Publishers") or []:
            if not isinstance(port, dict) or not isinstance(port.get("PublishedPort"), int):
                raise ValueError("Docker Compose returned invalid port mappings")
            if port["PublishedPort"] > 0:
                observation["published_ports"].append({"address": str(port.get("URL") or ""),
                    "published_port": port["PublishedPort"], "target_port": port.get("TargetPort"),
                    "protocol": str(port.get("Protocol") or "tcp")})
        result.append(observation)
    return sorted(result, key=lambda row: (row["service"], row["name"]))


def release_label(version):
    return "unknown" if not version else version if version == "development" else "v" + version


def port_label(port):
    address = port["address"]
    if ":" in address and not address.startswith("["):
        address = "[" + address + "]"
    return f"{address}:{port['published_port']}->{port['target_port']}/{port['protocol']}"


def print_tool(tool):
    """Describe the selected CLI, even when no deployment or Docker daemon exists."""
    print("USDB Explorer tool: " + release_label(tool["version"]))
    print("Source revision: " + (tool["source_revision"] or "unavailable (development or legacy release)"))
    if tool["source_dirty"] is True:
        print("WARNING: This tool was packaged from a modified source checkout.")
    print("Tool directory: " + tool["release_dir"])


def print_status(report):
    """Render a compact inventory; running containers are not a readiness certificate."""
    print_tool(report["tool"])
    print("Deployment directory: " + report["state_dir"])
    deployment = report["deployment"]
    if deployment is None:
        print("Deployment: NOT_PREPARED")
        print("Use the existing --state-dir, or run setup then prepare for a new deployment.")
        return
    print("Prepared release: " + release_label(deployment["release_version"]))
    print("Deployment: " + deployment["deployment_id"])
    print(f"Network: {deployment['network']} | Chain ID: {deployment['chain_id']}")
    print("Genesis: " + deployment["genesis_block_hash"])
    print(f"RPC: {deployment['rpc_mode']} | Ingress: {deployment['ingress_mode']} | Faucet: "
          + ("enabled" if deployment["faucet_enabled"] else "disabled"))
    print("Explorer: " + deployment["explorer_url"])
    if report["version_relation"] == "different":
        print("WARNING: Tool and prepared deployment versions differ; installing a tool does not update the deployment.")
        print("To apply the selected release, review its upgrade notes, then down -> prepare --replace -> preflight -> up -> check; retain custom --config/--state-dir paths.")
    elif report["version_relation"] == "unknown":
        print("Version comparison: unavailable for development or missing release metadata.")
    print("\nContainers:")
    if "error" in report:
        print("  UNAVAILABLE: " + report["error"]["message"])
    elif not report["containers"]:
        print("  No containers found for this deployment. Prepared files do not mean services are running.")
    else:
        rows = [(row["service"], row["state"], row["health"] or "-", row["status"] or "-",
                 ", ".join(port_label(port) for port in row["published_ports"]) or "-")
                for row in report["containers"]]
        rows.insert(0, ("SERVICE", "STATE", "HEALTH", "STATUS", "PUBLISHED PORTS"))
        widths = [max(len(row[column]) for row in rows) for column in range(4)]
        for row in rows:
            print("  " + "  ".join(value.ljust(widths[index]) if index < 4 else value for index, value in enumerate(row)))
    print("Scope: prepared configuration and container observations; use check to verify RPC and indexing. Image references are available with status --json.")

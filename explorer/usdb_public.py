"""Operate an independent public-service deployment; never import the node controller."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile

from public_config import (GIB, compose_document, container_rpc, endpoint, image_lock, load_config,
                           local_rpc_config, nginx_config, read_json, security_enforcement, wallet_network)
from public_checks import ReadRpc, check_explorer, identity_check, preflight, same_checkpoint

KIT = Path(__file__).resolve().parent
DEPLOYMENT_SCHEMA = "usdb-public-deployment:v1"


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_directory(path):
    """Avoid Compose interpolation and volume-short-syntax ambiguity in operator paths."""
    path = path.expanduser().resolve()
    if any(c in str(path) for c in "$:\n\r"):
        raise ValueError("deployment paths cannot contain $, colon or newlines")
    return path


def file_hashes(root):
    return {str(path.relative_to(root)): digest(path) for path in sorted(root.rglob("*")) if path.is_file()}


def verify_files(root, values):
    if not isinstance(values, dict) or not values:
        raise ValueError("missing deployment file hashes")
    for name, expected in values.items():
        path = root / name
        if (Path(name).is_absolute() or not path.resolve().is_relative_to(root) or path.is_symlink()
                or not path.is_file() or digest(path) != expected):
            raise ValueError(f"deployment file changed or missing: {name}")


def verify_kit(kit):
    """Packaged releases have a complete file manifest; development checkouts remain explicit."""
    manifest = kit / "release.json"
    if not manifest.exists():
        if not (kit.parent / "gateway/main.go").is_file():
            raise ValueError("release manifest missing outside a development checkout")
        return "development"
    value = read_json(manifest)
    if value.get("schema_version") != "usdb-public-release:v1":
        raise ValueError("unsupported public release manifest")
    verify_files(kit, value["files"])
    return value["version"]


def verify(root):
    """Lifecycle operations use the prepared generation, including after a tool upgrade."""
    value = read_json(root / "deployment.json")
    if value.get("schema_version") != DEPLOYMENT_SCHEMA or value.get("state_dir") != str(root):
        raise ValueError("deployment identity or location changed; prepare the new location explicitly")
    required = {"compose.json", "config.json", "identity.json", "credentials.json", "network.json", "images.lock.json"}
    if not required.issubset(value.get("files", {})):
        raise ValueError("deployment manifest is missing required files")
    verify_files(root, value["files"])
    return value


def docker(arguments, *, capture=True, timeout=30):
    """Do not echo environment or Compose documents containing database credentials."""
    return subprocess.run(["docker", *arguments], check=True, text=True,
                          capture_output=capture, timeout=timeout)


def compose(root, arguments, *, capture=False, timeout=None):
    return docker(["compose", "--project-directory", str(root), "-f", str(root / "compose.json"), *arguments],
                  capture=capture, timeout=timeout)


@contextmanager
def operation_lock(root):
    """Only this deployment is serialized; public operations never acquire a node lock."""
    root.parent.mkdir(parents=True, exist_ok=True)
    path = root.parent / ("." + root.name + ".operation.lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another operation is running for this public deployment") from None
        yield
    finally:
        os.close(fd)


def project_containers(project, *, running=False):
    ids = docker(["ps", *( [] if running else ["-a"]), "-q", "--filter", f"label=com.docker.compose.project={project}"]).stdout.split()
    return json.loads(docker(["inspect", *ids]).stdout) if ids else []


def require_stopped(project):
    if project_containers(project, running=True):
        raise ValueError("run usdb-public down before replacing this deployment configuration")


def configure(args, *, kit=KIT):
    """Opt into local RPC without replacing operator identity or a prepared deployment."""
    path = safe_directory(args.config)
    previous = path.read_bytes() if path.exists() else None
    config = read_json(path if previous is not None else kit / "config.example.json")
    rpc = config["rpc"]
    if args.rpc_url or rpc.get("mode") != "local-node":
        for key in ("read_url", "trace_url", "broadcast_url"):
            rpc[key] = args.rpc_url or "http://127.0.0.1:8545"
    rpc["mode"] = "local-node"
    resources = config.setdefault("resources", {})
    if resources.get("other_services_memory_gib", 0) == 0:
        resources["other_services_memory_gib"] = "auto"
    ingress = config["ingress"]
    if args.explorer_url:
        origin = endpoint(args.explorer_url, origin=True)
        private = origin.hostname in {"127.0.0.1", "localhost", "::1"}
        ingress.update(mode="bundled", explorer_url=args.explorer_url,
                       exposure="private" if private else "public",
                       bind_address=args.bind_address or ("127.0.0.1" if private else "0.0.0.0"))
        if origin.scheme == "http":
            ingress.pop("tls", None)
    if args.http_port is not None:
        ingress["http_port"] = args.http_port
    if args.bind_address:
        ingress["bind_address"] = args.bind_address
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".explorer-config-", dir=path.parent)
    staging = Path(temporary)
    os.close(fd)
    try:
        write_json(staging, config)
        load_config(staging, kit)
        if previous is not None:
            suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
            backup = path.with_name(path.name + ".backup-" + suffix)
            with backup.open("xb") as output:
                os.chmod(backup, 0o600)
                output.write(previous)
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)
    print(f"Configured local-node RPC: {path}")
    print("Run prepare; if a deployment already exists, run down then prepare --replace to apply it, retaining credentials and volumes.")


def prepare(config_path, root, *, replace=False, credentials_file=None, kit=KIT):
    """Render a new generation atomically, preserving credentials and previous configuration."""
    version = verify_kit(kit)
    config, identity = load_config(config_path, kit)
    lock = image_lock(kit)
    existing = root.exists()
    if existing and not replace:
        raise ValueError("state directory already exists; use --replace after down to preserve and replace its configuration")
    if existing:
        verify(root)
        old_config, old_identity = read_json(root / "config.json"), read_json(root / "identity.json")
        if (old_config["deployment_id"] != config["deployment_id"]
                or old_identity["chain_id"] != identity["chain_id"]
                or old_identity["genesis_block_hash"] != identity["genesis_block_hash"]):
            raise ValueError("replacement must preserve deployment and chain identity; use a new deployment for another network")
        if credentials_file:
            raise ValueError("replacement always retains existing database credentials")
        require_stopped(config["deployment_id"])
        credentials = read_json(root / "credentials.json")
    elif credentials_file:
        credentials = read_json(credentials_file)
    else:
        credentials = {"database": secrets.token_hex(32), "backend": secrets.token_hex(64)}
    import re
    if (set(credentials) != {"database", "backend"}
            or not all(re.fullmatch(r"[0-9a-f]{64,128}", str(v)) for v in credentials.values())):
        raise ValueError("invalid saved database credentials")
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".usdb-public-", dir=root.parent))
    try:
        for name, value in (("credentials.json", credentials), ("config.json", config), ("identity.json", identity),
                            ("images.lock.json", lock), ("network.json", wallet_network(config, identity)),
                            ("security-policy.json", {"enforcement": security_enforcement(config["network"]),
                             "qualified_for_public_exposure": lock["qualified_for_public_exposure"]})):
            write_json(staging / name, value)
        # Render mount paths for the final location while reading credentials from staging.
        doc = compose_document(config, identity, lock, staging)
        text = json.dumps(doc).replace(str(staging), str(root))
        write_json(staging / "compose.json", json.loads(text))
        for name, external in (("nginx.conf", False), ("nginx.locations.conf", True)):
            (staging / name).write_text(nginx_config(config, external=external))
        if config["rpc"]["mode"] == "local-node":
            for name, host in (("rpc-host.conf", True), ("rpc-relay.conf", False)):
                (staging / name).write_text(local_rpc_config(config, host=host))
        if "gateway" not in lock["images"]:
            build = staging / "build"
            build.mkdir(parents=True)
            shutil.copytree(kit.parent / "gateway", build / "gateway",
                            ignore=shutil.ignore_patterns("*_test.go"))
            shutil.copyfile(kit / "assets/Dockerfile.gateway", build / "Dockerfile.gateway")
        write_json(staging / "deployment.json", {"schema_version": DEPLOYMENT_SCHEMA,
                   "release_version": version, "state_dir": str(root), "files": file_hashes(staging)})
        # Keep secrets private even when prepare is run with a permissive shell umask.
        for path in staging.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
        # This file contains only public wallet metadata and is read by an unprivileged container.
        (staging / "network.json").chmod(0o644)
        backup = None
        if existing:
            suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
            backup = root.with_name(root.name + ".backup-" + suffix)
            root.rename(backup)
        try:
            staging.rename(root)
        except OSError:
            if backup:
                backup.rename(root)
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    print(f"Prepared {config['deployment_id']} from {version}: {root}; rpc={config['rpc']['mode']}; ingress={config['ingress']['mode']}")
    if not lock["qualified_for_public_exposure"]:
        print("WARNING: Testnet image security is report-only; image qualification remains incomplete.")
    return doc


def require_database_identity(root, document):
    """Fail on missing credentials or a different network rather than replacing a live database."""
    name = document["name"] + "_postgres-data"
    exists = docker(["volume", "ls", "-q", "--filter", f"name=^{name}$"]).stdout.strip()
    if not exists:
        return
    labels = json.loads(docker(["volume", "inspect", name]).stdout)[0].get("Labels") or {}
    expected = document["volumes"]["postgres-data"]["labels"]
    if any(labels.get(key) != value for key, value in expected.items()):
        raise ValueError("existing explorer database identity or credentials differ; restore credentials before up")


def memory_bytes(value):
    """Compose limits emitted here use whole MiB or GiB."""
    if isinstance(value, int):
        return value
    return int(value[:-1]) * ({"g": GIB, "m": 1024**2}[value[-1]])


def require_resources(config, document):
    """Account for the Docker host, this stack and declared colocated services without changing any of them."""
    info = json.loads(docker(["info", "--format", "{{json .}}"]).stdout)
    host = int(info["MemTotal"])
    limits = sum(memory_bytes(s["mem_limit"]) for s in document["services"].values())
    budget = config["resources"]["memory_budget_gib"] * GIB
    reservation = config["resources"]["other_services_memory_gib"]
    ids = docker(["ps", "-q"]).stdout.split()
    containers = json.loads(docker(["inspect", *ids]).stdout) if ids else []
    other_limits, seen = 0, set()
    for container in containers:
        labels = container["Config"].get("Labels") or {}
        memory = container["HostConfig"]["Memory"]
        if labels.get("com.docker.compose.project") != document["name"]:
            if not memory:
                raise ValueError("another running container has no memory cap; cap it before colocating public services")
            other_limits += memory
            continue
        service = labels.get("com.docker.compose.service")
        if service in seen or service not in document["services"] or memory != memory_bytes(document["services"][service]["mem_limit"]):
            raise ValueError("public containers have stale or unbudgeted limits; run down before up")
        seen.add(service)
    other = ((other_limits + GIB - 1) // GIB) * GIB if reservation == "auto" else reservation * GIB
    if limits > budget or budget + other + max(2 * GIB, host // 10) > host:
        raise ValueError("public service budget plus other services and system reserve exceeds Docker host RAM; reserve capacity in the node resource policy before retrying")
    if other_limits > other:
        raise ValueError("other_services_memory_gib is below existing container limits; reserve the colocated workloads explicitly")
    if reservation == "auto":
        print(f"Colocated Docker memory reservation: {other // GIB} GiB from running container limits.")


def require_local_docker():
    """Host loopback must mean this operator's Linux host in both acceptance and Docker."""
    context = docker(["context", "inspect"]).stdout
    endpoint_url = os.environ.get("DOCKER_HOST") if not os.environ.get("DOCKER_CONTEXT") else None
    endpoint_url = endpoint_url or json.loads(context)[0]["Endpoints"]["docker"]["Host"]
    info = json.loads(docker(["info", "--format", "{{json .}}"]).stdout)
    if (not endpoint_url.startswith("unix://") or info.get("OSType") != "linux"
            or "desktop" in info.get("OperatingSystem", "").lower()
            or any("rootless" in value for value in info.get("SecurityOptions", []))):
        raise ValueError("local-node RPC requires a local, rootful Linux Docker Engine; use external RPC mode for remote/rootless/Desktop Docker")


def check_local_relay(root, config, identity, report):
    """Check every private route and canonical identity before starting Explorer consumers."""
    def fetcher(url, payload):
        try:
            result = compose(root, ["exec", "-T", "rpc-relay", "wget", "-q", "-O", "-", "-T", "15",
                                   "--header=Content-Type:application/json", "--post-data=" + json.dumps(payload), url],
                             capture=True, timeout=20)
            if len(result.stdout) > 8 * 1024**2:
                raise ValueError("relay response exceeds the acceptance size limit")
            return json.loads(result.stdout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            raise ValueError("local RPC relay unavailable or malformed; inspect rpc-host/rpc-relay logs and the node loopback listener") from None
    checkpoint = {"number": hex(report["checkpoint"]["number"]), "hash": report["checkpoint"]["hash"]}
    routes = container_rpc(config)
    for key in ("read_url", "trace_url", "broadcast_url"):
        try:
            rpc = ReadRpc(routes[key], fetcher=fetcher)
            identity_check(rpc, identity)
            same_checkpoint(rpc, checkpoint)
        except ValueError as error:
            raise ValueError(f"container {key}: {error}") from None
    print("Local RPC relay: all container routes match the upstream checkpoint.")


def parser():
    result = argparse.ArgumentParser(description="Operate standalone USDB explorer and public RPC services")
    actions = result.add_subparsers(dest="command", required=True)
    for name in ("configure", "prepare", "up", "down", "status", "check", "preflight", "logs", "reload-proxy"):
        action = actions.add_parser(name)
        action.add_argument("--state-dir", type=Path, default=Path.home() / ".config/usdb-public/default",
                            help="Private deployment directory, independent of node.env")
        if name in {"configure", "prepare"}:
            action.add_argument("--config", type=Path, default=Path.home() / ".config/usdb-public/config.json",
                                help="Operator-owned configuration (default ~/.config/usdb-public/config.json)")
        if name == "configure":
            action.add_argument("--local-node", action="store_true", required=True, help="Use the node's host loopback RPC")
            action.add_argument("--rpc-url", help="Local HTTP RPC override (default http://127.0.0.1:8545)")
            action.add_argument("--explorer-url", help="Advertised origin; select bundled ingress and public binding for a non-loopback host")
            action.add_argument("--http-port", type=int, help="Local bundled HTTP port, independent of the advertised port (default 28080)")
            action.add_argument("--bind-address", help="Override the ingress IPv4 bind address")
        if name == "prepare":
            action.add_argument("--replace", action="store_true", help="Back up and replace a stopped generation, retaining credentials")
            action.add_argument("--credentials-file", type=Path, help="Restore saved credentials into a new state directory")
        if name == "logs":
            action.add_argument("--follow", action="store_true")
    return result


def execute(args, root):
    if args.command == "configure":
        configure(args)
        return 0
    if args.command == "prepare":
        prepare(args.config, root, replace=args.replace, credentials_file=args.credentials_file)
        return 0
    verify(root)
    config, identity = read_json(root / "config.json"), read_json(root / "identity.json")
    document = read_json(root / "compose.json")
    if args.command in {"preflight", "check"}:
        report = (preflight if args.command == "preflight" else check_explorer)(config, identity)
        print(json.dumps(report, indent=2))
    elif args.command == "up":
        if not read_json(root / "images.lock.json")["qualified_for_public_exposure"]:
            print("WARNING: Testnet image security is report-only; unresolved findings do not block startup.")
        version = docker(["version", "--format", "{{.Server.APIVersion}}"]).stdout.strip()
        import re
        if not re.fullmatch(r"[0-9]+\.[0-9]+", version) or tuple(map(int, version.split("."))) < (1, 44):
            raise ValueError("public services require Docker Engine 25+ for the isolated multi-network topology")
        require_resources(config, document)
        require_database_identity(root, document)
        if config["rpc"].get("mode") == "local-node":
            require_local_docker()
        report = preflight(config, identity)
        compose(root, ["config", "--quiet"], capture=True, timeout=30)
        if "build" in document["services"]["gateway"]:
            compose(root, ["build", "gateway"])
        else:
            compose(root, ["pull"])
        if config["ingress"]["mode"] == "bundled":
            compose(root, ["run", "--rm", "--no-deps", "proxy", "nginx", "-t"], capture=True, timeout=60)
        if config["rpc"].get("mode") == "local-node":
            compose(root, ["up", "-d", "--wait", "--wait-timeout", "60", "rpc-host", "rpc-relay"], timeout=90)
            check_local_relay(root, config, identity, report)
        compose(root, ["up", "-d"])
        print("Public services started; use status and check to verify indexing and the configured ingress.")
    elif args.command == "down":
        compose(root, ["down"])
        print("Public services stopped; database volumes and upstream nodes are preserved.")
    elif args.command == "reload-proxy":
        if config["ingress"]["mode"] != "bundled":
            raise ValueError("external ingress is managed by the server administrator; no bundled proxy to reload")
        compose(root, ["exec", "-T", "proxy", "nginx", "-t"], capture=True, timeout=30)
        compose(root, ["exec", "-T", "proxy", "nginx", "-s", "reload"], capture=True, timeout=30)
        print("Bundled proxy configuration checked and reloaded.")
    elif args.command == "logs":
        compose(root, ["logs", "--tail", "100", *(["--follow"] if args.follow else [])])
    else:
        compose(root, ["ps", "--all"], timeout=30)
        print(f"Ingress: {config['ingress']['mode']}; explorer: {config['ingress']['explorer_url']}")
        print("Container state does not prove synchronization; use check for canonical RPC/explorer samples.")
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        root = safe_directory(args.state_dir)
        if args.command == "configure":
            with operation_lock(safe_directory(args.config)):
                return execute(args, root)
        if args.command in {"prepare", "up", "down", "reload-proxy"}:
            with operation_lock(root):
                return execute(args, root)
        return execute(args, root)
    except subprocess.CalledProcessError:
        print("USDB public operation failed: Docker command failed; inspect service status/logs (configuration output is suppressed).", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("USDB public operation failed: Docker command timed out; inspect status before retrying.", file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        # URL transport errors are sanitized by public_checks before reaching this boundary.
        print(f"USDB public operation failed: {error}", file=sys.stderr)
    return 1

"""Operator entry points for the optional faucet, including one-shot miner-funded refills."""
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile

from faucet_config import DEFAULTS, amount


def add_parser(actions):
    """Keep all signing material off CLI arguments and out of prepared configuration."""
    group = actions.add_parser("faucet", help="Configure, inspect or fund the optional testnet faucet")
    commands = group.add_subparsers(dest="faucet_command", required=True)
    for name in ("configure", "status", "fund"):
        action = commands.add_parser(name)
        action.add_argument("--state-dir", type=Path, default=Path.home() / ".config/usdb-public/default")
        if name == "configure":
            action.add_argument("--config", type=Path, default=Path.home() / ".config/usdb-public/config.json")
            enabled = action.add_mutually_exclusive_group(required=True)
            enabled.add_argument("--enable", action="store_true")
            enabled.add_argument("--disable", action="store_true")
            for key in ("claim_amount", "daily_budget", *DEFAULTS):
                action.add_argument("--" + key.replace("_", "-"), type=int if type(DEFAULTS.get(key)) is int else str)
        if name == "fund":
            action.add_argument("--amount", help="USDB to transfer from the miner account")
            action.add_argument("--request-id", help="Stable recovery ID; generated and printed if omitted")
            action.add_argument("--retry", action="store_true", help="Reconcile or resend the saved transaction, without a private key")
            action.add_argument("--miner-key-env", metavar="NAME", help="Read and remove this variable from this process environment; default: hidden terminal input")


def configure(args):
    """Back up and validate the source configuration without changing running services."""
    from public_config import load_config, read_json
    from usdb_public import KIT, safe_directory, write_json
    path = safe_directory(args.config)
    previous = path.read_bytes() if path.exists() else None
    config = read_json(path if previous is not None else KIT / "config.example.json")
    value = config.setdefault("faucet", {})
    value["enabled"] = args.enable
    for key in ("claim_amount", "daily_budget", *DEFAULTS):
        if getattr(args, key) is not None:
            value[key] = getattr(args, key)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".faucet-config-", dir=path.parent)
    os.close(fd)
    staging = Path(temporary)
    try:
        write_json(staging, config)
        validated, _ = load_config(staging, KIT)
        write_json(staging, validated)
        if previous is not None:
            suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
            with path.with_name(path.name + ".backup-" + suffix).open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(previous)
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)
    print(f"Faucet {'enabled' if args.enable else 'disabled'} in source configuration: {path}")
    print("Apply with down, prepare --replace and up. Existing faucet wallet and ledger are retained.")


def execute(args, root):
    """Run private commands inside the prepared faucet container, passing a miner key on stdin only."""
    from public_config import read_json
    from usdb_public import verify
    if args.faucet_command == "configure":
        configure(args)
        return 0
    verify(root)
    if not read_json(root / "config.json").get("faucet", {}).get("enabled"):
        raise ValueError("faucet is disabled in the prepared deployment; configure it and re-prepare first")
    options = [args.faucet_command]
    private_key = ""
    if args.faucet_command == "fund":
        if args.retry:
            if not args.request_id or args.amount or args.miner_key_env:
                raise ValueError("--retry requires --request-id and does not accept --amount or a private key")
            options = ["retry", "--request-id", args.request_id]
        else:
            amount(args.amount)
            if args.miner_key_env:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.miner_key_env):
                    raise ValueError("invalid private key environment variable name")
                private_key = os.environ.pop(args.miner_key_env, "")
            else:
                if not sys.stdin.isatty():
                    raise ValueError("hidden private-key input requires a terminal; use --miner-key-env for automation")
                private_key = getpass.getpass("Miner private key (hidden, used once): ")
            if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", private_key.strip()):
                raise ValueError("invalid miner private key format")
            args.request_id = args.request_id or secrets.token_hex(16)
            options += ["--amount", args.amount, "--request-id", args.request_id]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.request_id):
            raise ValueError("request ID must contain 1..80 letters, digits, underscores or hyphens")
        print(f"Funding recovery ID: {args.request_id}", flush=True)
        print("If the result is uncertain, retry this ID; do not create another funding request.", flush=True)
    cmd = ["docker", "compose", "--project-directory", str(root), "-f", str(root / "compose.json"),
           "exec", "-T", "faucet", "/faucet", *options]
    try:
        result = subprocess.run(cmd, input=private_key, capture_output=True, text=True, timeout=110)
    finally:
        private_key = ""
    if result.stdout.strip():
        try:
            print(json.dumps(json.loads(result.stdout), indent=2))
        except ValueError:
            raise ValueError("invalid faucet command response; inspect status before retrying") from None
    if result.returncode:
        match = re.search(r"Faucet operation failed: ([A-Z_]+)\s*$", result.stderr)
        code = match.group(1) if match else "CONTAINER_UNAVAILABLE"
        raise ValueError(f"faucet command failed: {code}; use faucet status and the original funding recovery ID")
    return 0

"""Interactive source configuration; deployment lifecycle and signing stay explicit."""
from datetime import datetime, timezone
import ipaddress
import os
from pathlib import Path
import secrets
import shlex
import sys
import tempfile

from faucet_config import DEFAULTS, amount
from public_config import endpoint, load_config, read_json


class Prompts:
    """Retry invalid answers without discarding earlier choices or echoing private URLs."""

    def __init__(self, input_fn, output):
        self.input = input_fn
        self.output = output

    def say(self, text):
        print(text, file=self.output)

    def ask(self, label, default="", validate=None, *, private=False):
        suffix = f" [{'keep current' if private else default}]" if default != "" else ""
        while True:
            value = self.input(label + suffix + ": ").strip() or str(default)
            try:
                return validate(value) if validate else value
            except ValueError as error:
                self.say(str(error))

    def choice(self, label, choices, default):
        def validate(value):
            if value.lower() not in choices:
                raise ValueError("Choose one of: " + ", ".join(choices))
            return value.lower()
        return self.ask(label + " (" + "/".join(choices) + ")", default, validate)

    def yes(self, label, default=False):
        def validate(value):
            if value.lower() in {"y", "yes"}:
                return True
            if value.lower() in {"n", "no"}:
                return False
            raise ValueError("Enter yes or no.")
        return self.ask(label + " (y/n)", "y" if default else "n", validate)

    def integer(self, label, default, minimum=1, maximum=65535):
        def validate(value):
            if not value.isascii() or not value.isdecimal() or not minimum <= int(value) <= maximum:
                raise ValueError(f"Enter an integer from {minimum} to {maximum}.")
            return int(value)
        return self.ask(label, default, validate)

    def token_amount(self, label, default, decimals=18):
        def validate(value):
            amount(value, decimals)
            return value
        return self.ask(label, default, validate)


def rpc_endpoint(value, *, local=False):
    """Validate a private upstream without including the supplied value in errors."""
    try:
        parsed = endpoint(value)
        if local and (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}):
            raise ValueError()
    except ValueError:
        raise ValueError("Enter an HTTP loopback URL." if local else "Enter a private HTTP(S) URL without credentials, query or fragment.") from None
    return value


def configure_rpc(config, prompts):
    """Preserve explicit tracing/broadcast endpoints and pinned samples on reconfiguration."""
    rpc = config["rpc"]
    old_mode = rpc.get("mode", "external")
    prompts.say("\nNode connection: local-node uses this host's loopback RPC; external uses private endpoints reachable from the host and containers.")
    mode = prompts.choice("Node connection", ("local-node", "external"), old_mode)
    rpc["mode"] = mode
    if mode != old_mode:
        for key in ("read_url", "trace_url", "broadcast_url", "indexer_url"):
            rpc.pop(key, None)
    if mode == "local-node":
        rpc.setdefault("read_url", "http://127.0.0.1:8545")
        rpc.setdefault("indexer_url", "http://127.0.0.1:28020")
        resources = config.setdefault("resources", {})
        if mode != old_mode and resources.get("other_services_memory_gib", 0) == 0:
            resources["other_services_memory_gib"] = "auto"
    if "read_url" not in rpc or prompts.yes("Change private RPC endpoints"):
        validate = lambda value: rpc_endpoint(value, local=mode == "local-node")
        old_read = rpc.get("read_url")
        rpc["read_url"] = prompts.ask("Read RPC URL", old_read or "", validate, private=True)
        for key, label in (("trace_url", "Tracing RPC URL"), ("broadcast_url", "Broadcast RPC URL")):
            default = rpc.get(key, old_read)
            if default == old_read:
                default = rpc["read_url"]
            rpc[key] = prompts.ask(label, default or rpc["read_url"], validate, private=True)
        default = rpc.get("indexer_url") or ("http://127.0.0.1:28020" if mode == "local-node" else "-")
        def indexer(value):
            return None if value == "-" and mode == "external" else validate(value)
        value = prompts.ask("Private indexer RPC URL (- disables it in external mode)", default, indexer, private=True)
        if value is None:
            rpc.pop("indexer_url", None)
        else:
            rpc["indexer_url"] = value
    prompts.say("Full Explorer requires archive + private tracing. On the node, use usdb-node setup or set-query-mode --state-mode archive --tracing on.")
    prompts.say("Enabling archive does not recover already pruned history. Existing reference endpoints and sample pins are retained.")


def configure_ingress(config, prompts):
    """Keep visitor origins independent of local ports, including NAT and external TLS."""
    ingress = config["ingress"]
    old_mode = ingress["mode"]
    prompts.say("\nIngress: bundled runs Explorer's Nginx; external uses your own reverse proxy and TLS.")
    ingress["mode"] = prompts.choice("Ingress", ("bundled", "external"), old_mode)
    old_origin = ingress["explorer_url"].rstrip("/")
    def origin(value):
        endpoint(value, origin=True)
        return value.rstrip("/")
    prompts.say("Visitor URL is the actual browser address, including the external port. It can differ from the local listening port.")
    ingress["explorer_url"] = prompts.ask("Visitor URL", old_origin, origin)
    parsed = endpoint(ingress["explorer_url"], origin=True)
    if old_origin != ingress["explorer_url"]:
        ingress["exposure"] = "private" if parsed.hostname in {"127.0.0.1", "localhost", "::1"} else "public"
    ingress.setdefault("exposure", "private")
    if ingress["mode"] == "bundled":
        bind = ingress.get("bind_address", "127.0.0.1")
        if old_mode != "bundled" or old_origin != ingress["explorer_url"]:
            bind = "127.0.0.1" if ingress["exposure"] == "private" else "0.0.0.0"
        def address(value):
            try:
                parsed_address = ipaddress.IPv4Address(value)
                if ingress["exposure"] == "private" and not parsed_address.is_loopback:
                    raise ValueError()
            except ValueError:
                raise ValueError("Enter an IPv4 bind address; private exposure requires loopback.") from None
            return value
        ingress["bind_address"] = prompts.ask("Nginx host bind address", bind, address)
        ingress["http_port"] = prompts.integer("Nginx local HTTP port", ingress.get("http_port", 28080))
        if parsed.scheme == "https":
            ingress["https_port"] = prompts.integer("Nginx local HTTPS port", ingress.get("https_port", 28443))
            prompts.say("Provide an existing directory containing fullchain.pem and privkey.pem; certificate issuance/renewal remains operator-managed.")
            def certificate_dir(value):
                path = Path(value).expanduser().resolve()
                if not value or any(c in str(path) for c in "$:\n\r") or not all(
                        (path / name).is_file() and (path / name).resolve().is_relative_to(path)
                        for name in ("fullchain.pem", "privkey.pem")):
                    raise ValueError("Choose a certificate directory containing fullchain.pem and privkey.pem (no external symlinks).")
                return str(path)
            ingress["tls"] = {"certificate_dir": prompts.ask("Certificate directory", ingress.get("tls", {}).get("certificate_dir", ""), certificate_dir)}
        else:
            ingress.pop("tls", None)
    else:
        ingress["bind_address"] = "127.0.0.1"
        ingress.pop("tls", None)
        ingress["web_port"] = prompts.integer("Local frontend port for your proxy", ingress.get("web_port", 28080))
        ingress["gateway_port"] = prompts.integer("Local gateway port for your proxy", ingress.get("gateway_port", 28081))
        prompts.say("External ingress binds backend ports to loopback. Apply the generated nginx.locations.conf in your proxy, which owns HTTPS certificates.")


def configure_faucet(config, prompts):
    """Configure opt-in limits only; never ask for or store a signing key in setup."""
    value = config.setdefault("faucet", {"enabled": False})
    prompts.say("\nTestnet faucet uses its own persistent wallet. Miner-funded refills are a separate faucet fund command.")
    value["enabled"] = prompts.yes("Enable testnet faucet", value.get("enabled", False))
    if not value["enabled"]:
        return
    value["claim_amount"] = prompts.token_amount("USDB per claim", value.get("claim_amount", "1"))
    value["daily_budget"] = prompts.token_amount("Daily USDB budget including reserved fees (UTC, per instance)", value.get("daily_budget", "100"))
    for key, default in DEFAULTS.items():
        value.setdefault(key, default)
    if prompts.yes("Customize cooldown, IP limits and transaction policy"):
        for key, label, minimum, maximum in (
                ("cooldown_seconds", "Address cooldown in seconds", 60, 31536000),
                ("ip_daily_claims", "Claims per IP per rolling 24 hours", 1, 10000),
                ("ip_requests_per_minute", "Requests per IP per minute", 1, 1000),
                ("confirmations", "Transaction confirmation depth", 1, 100)):
            value[key] = prompts.integer(label, value[key], minimum, maximum)
        value["gas_reserve"] = prompts.token_amount("USDB gas reserve", value["gas_reserve"])
        value["max_gas_price_gwei"] = prompts.token_amount("Maximum gas price in Gwei", value["max_gas_price_gwei"], 9)
    if config["ingress"]["mode"] == "external":
        config["ingress"]["faucet_port"] = prompts.integer("Local faucet port for your proxy", config["ingress"].get("faucet_port", 28083))


def summary(config, prompts):
    """Review public settings and quota policy without printing private upstream endpoints."""
    ingress, faucet = config["ingress"], config.get("faucet", {})
    prompts.say(f"\nDeployment: {config['deployment_id']}; network: {config['network']}")
    prompts.say(f"RPC: {config['rpc']['mode']}; ingress: {ingress['mode']}; exposure: {ingress['exposure']}")
    prompts.say("Visitor URL: " + ingress["explorer_url"])
    if ingress["mode"] == "bundled":
        ports = ["http_port"] + (["https_port"] if "tls" in ingress else [])
    else:
        ports = ["web_port", "gateway_port"] + (["faucet_port"] if faucet.get("enabled") else [])
    for key in ports:
        prompts.say(f"  {key}: {ingress['bind_address']}:{ingress[key]}")
    if "tls" in ingress:
        prompts.say("Certificate directory: " + ingress["tls"]["certificate_dir"])
    prompts.say("Faucet: " + ("enabled" if faucet.get("enabled") else "disabled"))
    if faucet.get("enabled"):
        for key in ("claim_amount", "daily_budget", *DEFAULTS):
            prompts.say(f"  {key}: {faucet[key]}")
        prompts.say("CAPTCHA and scheduled refills are not implemented.")


def setup(args, root, *, kit, input_fn=None, output=None):
    """Save a reviewed, validated source atomically; leave prepared files and services intact."""
    from usdb_public import safe_directory, verify, verify_kit, write_json
    prompts = Prompts(input_fn or input, output or sys.stdout)
    path = safe_directory(args.config)
    if path.is_relative_to(root) or path.is_relative_to(kit.resolve()):
        raise ValueError("setup requires an operator source config outside the prepared deployment and release kit")
    verify_kit(kit)
    existing = root.exists()
    if existing:
        verify(root)
    previous = path.read_bytes() if path.exists() else None
    source = path if previous is not None else (root / "config.json" if existing else kit / "config.example.json")
    config = read_json(source)
    prompts.say(f"USDB Explorer setup\nConfiguration source: {source}\nSave to: {path}")
    prompts.say("Enter keeps the displayed value. Ctrl-C cancels before saving. This wizard configures Explorer; it does not reconfigure the node.")
    try:
        while True:
            configure_rpc(config, prompts)
            configure_ingress(config, prompts)
            configure_faucet(config, prompts)
            # Run the same full validation as prepare before review, without writing operator files.
            with tempfile.TemporaryDirectory(prefix="explorer-setup-") as temporary:
                candidate = Path(temporary) / "config.json"
                write_json(candidate, config)
                try:
                    validated, identity = load_config(candidate, kit)
                except ValueError as error:
                    prompts.say("Configuration not saved: " + str(error))
                    if prompts.yes("Review settings again", True):
                        continue
                    return 0
            if existing:
                old_config, old_identity = read_json(root / "config.json"), read_json(root / "identity.json")
                if (validated["deployment_id"] != old_config["deployment_id"] or identity["chain_id"] != old_identity["chain_id"]
                        or identity["genesis_block_hash"] != old_identity["genesis_block_hash"]):
                    raise ValueError("setup must preserve the existing deployment and chain identity; select a separate source config and state directory for another deployment")
            summary(validated, prompts)
            if not prompts.yes("Save this Explorer configuration", True):
                prompts.say("Setup cancelled; no configuration was written.")
                return 0
            break
    except (EOFError, KeyboardInterrupt):
        prompts.say("\nSetup cancelled; no configuration was written.")
        return 130
    # Detect operator edits made outside CLI locks while the interactive review was open.
    if (path.read_bytes() if path.exists() else None) != previous:
        raise ValueError("source configuration changed during setup; rerun setup to review the current settings")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".explorer-setup-", dir=path.parent)
    os.close(fd)
    staging = Path(temporary)
    try:
        write_json(staging, validated)
        if previous is not None:
            suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
            backup = path.with_name(path.name + ".backup-" + suffix)
            descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(previous)
            prompts.say(f"Previous source configuration backed up: {backup}")
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)
    prompts.say(f"Configuration saved: {path}\nNext: apply the configuration (existing credentials, wallet and database volumes are retained).")
    state_arg = " --state-dir " + shlex.quote(str(root))
    if existing:
        prompts.say("  usdb-explorer down" + state_arg)
    prompts.say("  usdb-explorer prepare" + (" --replace" if existing else "") + " --config " + shlex.quote(str(path)) + state_arg)
    for command in ("preflight", "up", "check"):
        prompts.say("  usdb-explorer " + command + state_arg)
    if validated["ingress"]["mode"] == "external":
        prompts.say(f"Before check, apply {root / 'nginx.locations.conf'} in your external proxy; preserve its private faucet proxy token.")
    if validated.get("faucet", {}).get("enabled"):
        prompts.say("After startup: usdb-explorer faucet status" + state_arg)
        prompts.say("To refill: usdb-explorer faucet fund --amount <USDB>" + state_arg + " (hidden miner key input; never saved in configuration).")
    prompts.say("Setup saved configuration only. Services, port forwarding and firewall rules have not been changed.")
    return 0

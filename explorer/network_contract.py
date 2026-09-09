"""Validate vendored USDB interface data without importing a node checkout."""
import hashlib
import json
from pathlib import Path
import re


def check_network(repo):
    """Bind the shipped catalog to a reviewed source commit and supported RPC profile."""
    directory = Path(repo) / "explorer/networks"
    contract = json.loads((directory / "usdb-testnet-v0.contract.json").read_bytes())
    source, catalog, rpc = contract["source"], contract["catalog"], contract["rpc"]
    if (contract.get("schema_version") != "usdb-explorer-network-contract:v1"
            or source.get("repository") != "buckyos/usdb"
            or not re.fullmatch(r"[0-9a-f]{40}", source.get("revision", ""))
            or source.get("path") != "docker/networks/testnet-v0"
            or catalog.get("file") != "usdb-testnet-v0.json"
            or rpc.get("profile_id") != "usdb-explorer-rpc:v1"):
        raise ValueError("unsupported explorer network contract or source identity")
    body = (directory / catalog["file"]).read_bytes()
    if hashlib.sha256(body).hexdigest() != catalog.get("sha256"):
        raise ValueError("network catalog differs from its pinned contract checksum")
    identity = json.loads(body)
    if (identity.get("bundle_id") != "usdb-testnet-v0"
            or type(identity.get("chain_id")) is not int or identity["chain_id"] <= 0
            or type(identity.get("network_id")) is not int or identity["network_id"] <= 0
            or not re.fullmatch(r"0x[0-9a-f]{64}", identity.get("genesis_block_hash", ""))
            or not {"eth_chainId", "net_version", "eth_getBlockByNumber", "eth_getBalance", "eth_getCode", "eth_call"}.issubset(rpc.get("read_methods", []))
            or set(rpc.get("trace_methods", [])) != {"debug_traceTransaction", "debug_traceBlockByNumber"}
            or rpc.get("tracer") != "callTracer"
            or rpc.get("broadcast_methods") != ["eth_sendRawTransaction"]
            or rpc.get("semantics") != {"extra_data": "preserve", "rewards_supply": "not_qualified", "fee_distribution": "not_qualified"}):
        raise ValueError("invalid network identity or unsupported RPC capabilities/semantics")
    return contract

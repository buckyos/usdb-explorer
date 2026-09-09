"""Read-only upstream and explorer acceptance, independent of node administration."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from public_config import HASH, endpoint

READ_METHODS = {"eth_chainId", "net_version", "eth_syncing", "eth_getBlockByNumber", "eth_getBalance",
                "eth_getCode", "eth_call", "eth_getTransactionReceipt", "debug_traceTransaction", "debug_traceBlockByNumber"}


def quantity(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", value):
        raise ValueError("RPC returned an invalid quantity")
    return int(value, 16)


def data(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", value):
        raise ValueError("RPC returned invalid hex data")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(url, payload=None):
    """Never log operator endpoints or upstream errors, which can contain access tokens."""
    request = urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            body = response.read(8 * 1024**2 + 1)
        if len(body) > 8 * 1024**2:
            raise ValueError("response exceeds the acceptance size limit")
        return json.loads(body)
    except (OSError, urllib.error.URLError, ValueError) as error:
        raise ValueError(f"HTTP endpoint unavailable or malformed ({type(error).__name__})") from None


class ReadRpc:
    def __init__(self, url):
        endpoint(url)
        self.url = url

    def __call__(self, method, params):
        if method not in READ_METHODS:
            raise ValueError("acceptance refuses non-read-only RPC methods")
        value = fetch(self.url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0" or value.get("id") != 1 or "error" in value or "result" not in value:
            raise ValueError(f"RPC {method} failed or returned an invalid envelope")
        return value["result"]


def block(rpc, tag):
    value = rpc("eth_getBlockByNumber", [tag, False])
    if (not isinstance(value, dict) or not HASH.fullmatch(str(value.get("hash", "")))
            or not isinstance(value.get("transactions"), list)):
        raise ValueError("RPC block is unavailable or malformed")
    number = quantity(value.get("number"))
    if tag != "latest" and number != quantity(tag):
        raise ValueError("RPC returned a different block height")
    return value


def identity_check(rpc, identity):
    genesis = rpc("eth_getBlockByNumber", ["0x0", False])
    if (quantity(rpc("eth_chainId", [])) != identity["chain_id"]
            or rpc("net_version", []) != str(identity["network_id"])
            or not isinstance(genesis, dict)
            or str(genesis.get("hash", "")).lower() != identity["genesis_block_hash"].lower()):
        raise ValueError("upstream network identity differs from the frozen network")
    if rpc("eth_syncing", []) is not False:
        raise ValueError("upstream is still syncing")


def same_checkpoint(rpc, checkpoint):
    if block(rpc, checkpoint["number"])["hash"].lower() != checkpoint["hash"].lower():
        raise ValueError("upstream canonical checkpoint changed or differs between endpoints")


def preflight(config, identity, *, rpc_factory=ReadRpc):
    """Require historical and trace samples, without claiming complete archive qualification."""
    settings = config["rpc"]
    clients = {name: rpc_factory(settings[name]) for name in ("read_url", "trace_url", "broadcast_url")}
    if settings.get("reference_url"):
        clients["reference_url"] = rpc_factory(settings["reference_url"])
    for name, client in clients.items():
        try:
            identity_check(client, identity)
        except ValueError as error:
            raise ValueError(f"{name}: {error}") from None
    read = clients["read_url"]
    head = block(clients.get("reference_url", read), "latest")
    # Pin the reference first. A stale archive cannot pass by agreeing with its own explorer.
    for client in clients.values():
        same_checkpoint(client, head)
    height = settings.get("historical_block", max(0, quantity(head["number"]) - 256))
    if height > quantity(head["number"]):
        raise ValueError("historical sample is above the observed head")
    historical = block(read, hex(height))
    address = head.get("miner", "0x" + "00" * 20)
    if not isinstance(address, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
        raise ValueError("invalid sample address")
    quantity(read("eth_getBalance", [address, hex(height)]))
    data(read("eth_getCode", [address, hex(height)]))
    data(read("eth_call", [{"to": "0x" + "00" * 20, "data": "0x"}, hex(height)]))
    transaction = settings.get("transaction")
    if transaction is None:
        # Bound discovery on quiet networks; the operator can choose an older transaction explicitly.
        for number in range(quantity(head["number"]), max(-1, quantity(head["number"]) - 32), -1):
            sample = block(read, hex(number))
            if sample["transactions"]:
                transaction = sample["transactions"][0]
                break
    if not isinstance(transaction, str) or not HASH.fullmatch(transaction):
        raise ValueError("no recent transaction found; set rpc.transaction to an existing mined transaction for tracing acceptance")
    receipt = read("eth_getTransactionReceipt", [transaction])
    if not isinstance(receipt, dict) or str(receipt.get("transactionHash", "")).lower() != transaction.lower():
        raise ValueError("sample receipt is unavailable")
    sample_block = block(read, receipt.get("blockNumber"))
    if (sample_block["hash"].lower() != str(receipt.get("blockHash", "")).lower()
            or transaction.lower() not in [str(h).lower() for h in sample_block["transactions"]]
            or quantity(sample_block["number"]) > quantity(head["number"])):
        raise ValueError("sample transaction is not canonical at the checkpoint")
    quantity(receipt.get("gasUsed"))
    if quantity(receipt.get("status")) not in (0, 1):
        raise ValueError("sample receipt status is invalid")
    trace = clients["trace_url"]
    same_checkpoint(trace, sample_block)
    result = trace("debug_traceTransaction", [transaction, {"tracer": "callTracer", "timeout": "5s"}])
    if not isinstance(result, dict) or not isinstance(result.get("type"), str) or not result["type"]:
        raise ValueError("upstream tracing sample failed")
    traced_block = trace("debug_traceBlockByNumber", [sample_block["number"], {"tracer": "callTracer", "timeout": "5s"}])
    if (not isinstance(traced_block, list) or len(traced_block) != len(sample_block["transactions"])
            or any(not isinstance(item, dict) or item.get("error") or not isinstance(item.get("result"), dict)
                   or not isinstance(item["result"].get("type"), str) or not item["result"]["type"] for item in traced_block)):
        raise ValueError("upstream block tracing sample failed")
    for client in clients.values():
        same_checkpoint(client, head)
    same_checkpoint(read, historical)
    same_checkpoint(read, sample_block)
    same_checkpoint(trace, sample_block)
    return {"schema_version": "usdb-public-check:v1", "status": "PREFLIGHT_PASSED",
            "checkpoint": {"number": quantity(head["number"]), "hash": head["hash"]},
            "historical_sample": {"number": height, "hash": historical["hash"]}, "transaction": transaction,
            "reference_check": "passed" if "reference_url" in clients else "not_configured",
            "historical_state_sample": "passed", "trace_sample": "passed",
            "archive_replay_qualification": "not_run", "wallet_broadcast_acceptance": "not_run",
            "public_tls_acceptance": "not_run", "reward_supply_qualification": "not_run"}


def check_explorer(config, identity, *, rpc_factory=ReadRpc, api_fetch=fetch):
    """Compare the public route and indexed data at a fixed upstream checkpoint."""
    report = preflight(config, identity, rpc_factory=rpc_factory)
    origin = config["ingress"]["explorer_url"]
    public = rpc_factory(origin + "/rpc")
    identity_check(public, identity)
    checkpoint = {"number": hex(report["checkpoint"]["number"]), "hash": report["checkpoint"]["hash"]}
    same_checkpoint(public, checkpoint)
    indexed = api_fetch(origin + "/api/v2/blocks/" + checkpoint["hash"])
    if indexed.get("hash") != checkpoint["hash"] or indexed.get("height") != report["checkpoint"]["number"]:
        raise ValueError("explorer has not indexed the observed upstream checkpoint")
    transaction = report["transaction"]
    receipt = public("eth_getTransactionReceipt", [transaction])
    indexed_tx = api_fetch(origin + "/api/v2/transactions/" + transaction)
    if (not isinstance(receipt, dict) or indexed_tx.get("hash") != transaction
            or indexed_tx.get("block_number") != quantity(receipt.get("blockNumber"))
            or indexed_tx.get("result") != ("success" if quantity(receipt.get("status")) == 1 else "execution reverted")
            or str(indexed_tx.get("gas_used")) != str(quantity(receipt.get("gasUsed")))):
        raise ValueError("explorer transaction does not match the RPC receipt")
    canonical = block(public, receipt["blockNumber"])
    if canonical["hash"] != receipt["blockHash"] or transaction not in canonical["transactions"]:
        raise ValueError("public receipt is no longer canonical")
    source_receipt = rpc_factory(config["rpc"]["read_url"])("eth_getTransactionReceipt", [transaction])
    for field in ("transactionHash", "blockHash", "blockNumber", "status", "gasUsed", "effectiveGasPrice"):
        if not isinstance(source_receipt, dict) or receipt.get(field) != source_receipt.get(field):
            raise ValueError("public receipt differs from the configured upstream")
    fee = quantity(receipt.get("gasUsed")) * quantity(receipt.get("effectiveGasPrice"))
    if str(indexed_tx.get("fee", {}).get("value")) != str(fee):
        raise ValueError("explorer transaction fee differs from the canonical receipt")
    for url in {config["rpc"]["read_url"], config["rpc"].get("reference_url", config["rpc"]["read_url"])}:
        same_checkpoint(rpc_factory(url), checkpoint)
        same_checkpoint(rpc_factory(url), canonical)
    same_checkpoint(public, checkpoint)
    report["status"] = "CHECKED"
    return report

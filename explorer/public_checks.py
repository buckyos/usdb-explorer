"""Read-only upstream and explorer acceptance, independent of node administration."""
from __future__ import annotations

import json
import re
import socket
import ssl
import urllib.error
import urllib.request

from public_config import HASH, endpoint

READ_METHODS = {"eth_chainId", "net_version", "eth_syncing", "eth_getBlockByNumber", "eth_getBalance",
                "eth_getCode", "eth_call", "eth_getTransactionReceipt", "debug_traceTransaction", "debug_traceBlockByNumber"}
TRACE_METHODS = {"debug_traceTransaction", "debug_traceBlockByNumber"}
STATE_METHODS = {"eth_getBalance", "eth_getCode", "eth_call"}
MISSING_TRANSACTION = "0x" + "00" * 32


class RpcFailure(ValueError):
    """Carry only a stable category and locally authored, credential-free guidance."""
    def __init__(self, category, detail):
        self.category = category
        self.detail = detail
        super().__init__(f"[{category}] {detail}")


def rpc_operation(method, params):
    """Include only known method names and bounded block quantities, never raw parameters."""
    position = 1 if method in STATE_METHODS else 0 if method in {"eth_getBlockByNumber", "debug_traceBlockByNumber"} else None
    if position is not None and isinstance(params, list) and len(params) > position:
        tag = params[position]
        if isinstance(tag, str) and re.fullmatch(r"0x[0-9a-fA-F]{1,16}", tag):
            return f"RPC {method} at block {int(tag, 16)} ({tag})"
    return f"RPC {method}"


def rpc_failure(method, params, code, message):
    """Classify a well-formed RPC error without echoing its message or data."""
    operation = rpc_operation(method, params)
    text = message.lower()
    if code == -32000 and params and (
            (method == "debug_traceBlockByNumber" and params[0] == "0x0" and text == "genesis is not traceable")
            or (method == "debug_traceTransaction" and params[0] == MISSING_TRANSACTION
                and text in {"genesis is not traceable", "transaction not found"})):
        # Geth 1.10 reports the genesis error for an unknown transaction too. Accept
        # it only for our absent-transaction probe, never for a real mined sample.
        category = "TRACE_SAMPLE_UNAVAILABLE"
        advice = "tracing method responded, but this probe has no executable transaction; real callTracer validation remains pending."
    elif code == 3 or text.startswith("execution reverted"):
        category = "RPC_EXECUTION_ERROR"
        advice = "EVM execution reverted; verify the sample and chain state in node logs. A contract revert does not establish missing archive or tracing support."
    elif any(marker in text for marker in ("unauthorized", "authentication required", "access denied", "forbidden", "invalid api key")):
        category = "RPC_AUTH"
        advice = "access was denied; check upstream credentials and private proxy access rules."
    elif code == -32601:
        if method in TRACE_METHODS:
            category = "TRACING_UNAVAILABLE"
            advice = ("tracing method is unavailable or filtered. On the upstream host with a compatible USDB release, "
                      "run 'usdb-node down', 'usdb-node set-query-mode --tracing on', then 'usdb-node up'; "
                      "ensure the private proxy allows debug_traceTransaction and debug_traceBlockByNumber. Keep debug RPC private.")
        else:
            category = "RPC_METHOD_UNAVAILABLE"
            advice = "required method is unavailable or filtered; check the configured RPC endpoint, API namespaces and proxy allowlist."
    elif method in STATE_METHODS | TRACE_METHODS and any(marker in text for marker in (
            "missing trie node", "historical state unavailable", "historical state is unavailable",
            "state is not available", "state not available", "no state available", "state has been pruned")):
        category = "HISTORICAL_STATE_UNAVAILABLE"
        advice = ("required historical state is unavailable, possibly pruned or incomplete. Full Explorer requires archive data covering this sample. "
                  "On a compatible USDB release, stop the node before 'usdb-node set-query-mode --state-mode archive'. "
                  "Enabling archive does not restore pruned history; preserve existing data and replay from genesis in a separate data directory "
                  "or restore a verified complete archive backup.")
    elif any(marker in text for marker in ("execution timeout", "timed out", "deadline exceeded", "request timeout")):
        category = "TRACING_TIMEOUT" if method in TRACE_METHODS else "RPC_TIMEOUT"
        advice = ("request execution timed out; check node load and upstream/indexer readiness, then retry. "
                  "A timeout does not prove archive or tracing is unavailable.")
    elif method in TRACE_METHODS and "tracer not found" in text:
        category = "TRACER_UNSUPPORTED"
        advice = "callTracer is unavailable; use a compatible USDB chain image and check private tracing proxy compatibility."
    else:
        category = "RPC_ERROR"
        advice = "upstream rejected the request; inspect node/proxy logs locally. The error does not establish missing archive or tracing support."
    status = f" (JSON-RPC code {code})" if code is not None else ""
    return RpcFailure(category, f"{operation}{status}: {advice}")


def named_rpc(client, name, *, guidance=""):
    """Identify a configured route without disclosing its URL."""
    def call(method, params):
        try:
            return client(method, params)
        except RpcFailure as error:
            raise RpcFailure(error.category, f"{name}: {error.detail}" + (f" {guidance}" if guidance else "")) from None
    return call


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
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            category, advice = "RPC_AUTH", "check upstream credentials and private proxy access rules"
        elif error.code == 429:
            category, advice = "RPC_RATE_LIMIT", "upstream rate limit reached; reduce concurrent requests or adjust the private RPC quota, then retry"
        elif error.code in {408, 504}:
            category, advice = "RPC_TIMEOUT", "upstream or proxy timed out; check node load, readiness and proxy timeouts, then retry"
        else:
            category, advice = "RPC_HTTP", "check the configured endpoint path, upstream RPC listener and proxy health"
        raise RpcFailure(category, f"HTTP endpoint returned status {error.code}; {advice}") from None
    except (OSError, urllib.error.URLError) as error:
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        if isinstance(reason, socket.gaierror):
            category = "RPC_DNS"
            message = "DNS lookup failed; configure a reachable RPC hostname or use configure --local-node"
        elif isinstance(reason, ConnectionRefusedError):
            category = "RPC_CONNECTION_REFUSED"
            message = "connection refused; check that the node is running and its RPC listen address/port match the configuration"
        elif isinstance(reason, TimeoutError):
            category = "RPC_TIMEOUT"
            message = "connection timed out; check RPC reachability and firewall rules"
        elif isinstance(reason, ssl.SSLError):
            category = "RPC_TLS"
            message = "TLS verification/connection failed; check the upstream certificate and trusted CA"
        else:
            category = "RPC_CONNECTION"
            message = "connection failed; check RPC address, routing and listener configuration"
        raise RpcFailure(category, message) from None
    except ValueError:
        raise RpcFailure("RPC_INVALID_RESPONSE", "HTTP endpoint returned invalid JSON or an oversized response; check that the configured endpoint serves RPC") from None


class ReadRpc:
    def __init__(self, url, *, fetcher=fetch):
        endpoint(url)
        self.url = url
        self.fetcher = fetcher

    def __call__(self, method, params):
        if method not in READ_METHODS:
            raise ValueError("acceptance refuses non-read-only RPC methods")
        operation = rpc_operation(method, params)
        try:
            value = self.fetcher(self.url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        except RpcFailure as error:
            raise RpcFailure(error.category, f"{operation}: {error.detail}") from None
        if (not isinstance(value, dict) or value.get("jsonrpc") != "2.0"
                or type(value.get("id")) is not int or value["id"] != 1
                or ("error" in value) == ("result" in value)):
            raise RpcFailure("RPC_INVALID_RESPONSE", f"{operation}: invalid JSON-RPC envelope; check endpoint and proxy compatibility")
        if "error" in value:
            error = value["error"]
            if (not isinstance(error, dict) or type(error.get("code")) is not int
                    or not -(2**31) <= error["code"] < 2**31 or not isinstance(error.get("message"), str)):
                raise RpcFailure("RPC_INVALID_RESPONSE", f"{operation}: malformed JSON-RPC error; check endpoint and proxy compatibility")
            raise rpc_failure(method, params, error["code"], error["message"])
        result = value["result"]
        # Geth may report per-transaction tracing failures in an otherwise successful
        # block response. An EVM revert inside result is a valid trace, not a transport failure.
        if method == "debug_traceBlockByNumber" and isinstance(result, list):
            for item in result:
                if isinstance(item, dict) and isinstance(item.get("error"), str) and item["error"]:
                    raise rpc_failure(method, params, None, item["error"])
        return result


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
        raise ValueError("upstream is still syncing; wait for node synchronization and upstream readiness, then rerun preflight")


def same_checkpoint(rpc, checkpoint):
    if block(rpc, checkpoint["number"])["hash"].lower() != checkpoint["hash"].lower():
        raise ValueError("upstream canonical checkpoint changed or differs between endpoints")


def pending_trace_sample(trace, head):
    """Require both trace methods even when no mined transaction is available."""
    try:
        trace("debug_traceTransaction", [MISSING_TRANSACTION, {"tracer": "callTracer", "timeout": "5s"}])
    except RpcFailure as error:
        if error.category != "TRACE_SAMPLE_UNAVAILABLE":
            raise
    else:
        raise RpcFailure("RPC_INVALID_RESPONSE", "trace_url: RPC debug_traceTransaction: expected an absent-transaction error for the capability probe")
    try:
        result = trace("debug_traceBlockByNumber", [head["number"], {"tracer": "callTracer", "timeout": "5s"}])
    except RpcFailure as error:
        if quantity(head["number"]) != 0 or error.category != "TRACE_SAMPLE_UNAVAILABLE":
            raise
    else:
        if quantity(head["number"]) == 0 or result != []:
            raise RpcFailure("RPC_INVALID_RESPONSE", "trace_url: RPC debug_traceBlockByNumber: unexpected empty-chain probe result")


def same_genesis_head(clients, head):
    """Do not mistake a lagging route or a newly mined block for a genesis-only chain."""
    for client in clients:
        current = block(client, "latest")
        if quantity(current["number"]) != 0 or current["hash"].lower() != head["hash"].lower():
            raise ValueError("upstream advanced or endpoints disagree during genesis checks; rerun preflight")


def preflight(config, identity, *, rpc_factory=ReadRpc):
    """Check full-mode capabilities; defer real tracing only when automatic sampling finds no transaction."""
    settings = config["rpc"]
    clients = {name: named_rpc(rpc_factory(settings[name]), name) for name in ("read_url", "trace_url", "broadcast_url")}
    if settings.get("reference_url"):
        clients["reference_url"] = named_rpc(rpc_factory(settings["reference_url"]), "reference_url")
    for name, client in clients.items():
        try:
            identity_check(client, identity)
        except RpcFailure:
            raise
        except ValueError as error:
            raise ValueError(f"{name}: {error}") from None
    read = clients["read_url"]
    head = block(clients.get("reference_url", read), "latest")
    # Pin the reference first. A stale archive cannot pass by agreeing with its own explorer.
    for client in clients.values():
        same_checkpoint(client, head)
    head_height = quantity(head["number"])
    height = settings.get("historical_block", max(0, head_height - 256))
    if height > head_height:
        raise RpcFailure("SAMPLE_ABOVE_HEAD", f"configured rpc.historical_block={height} exceeds observed head {head_height}; "
                         "wait for the intended block, or remove rpc.historical_block and rpc.transaction from the source config for automatic sampling, "
                         "then run down and prepare --replace. Local-node users can use configure --local-node --auto-samples.")
    historical = block(read, hex(height))
    address = head.get("miner", "0x" + "00" * 20)
    if not isinstance(address, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
        raise ValueError("invalid sample address")
    for method, params, validate in (
            ("eth_getBalance", [address, hex(height)], quantity),
            ("eth_getCode", [address, hex(height)], data),
            ("eth_call", [{"to": "0x" + "00" * 20, "data": "0x"}, hex(height)], data)):
        result = read(method, params)
        try:
            validate(result)
        except ValueError:
            raise RpcFailure("RPC_INVALID_RESPONSE", f"read_url: {rpc_operation(method, params)}: invalid result; check node/proxy RPC compatibility") from None
    transaction = settings.get("transaction")
    if transaction is None:
        # Bound discovery on quiet networks; the operator can choose an older transaction explicitly.
        for number in range(head_height, max(-1, head_height - 32), -1):
            sample = block(read, hex(number))
            if sample["transactions"]:
                transaction = sample["transactions"][0]
                break
    report = {"schema_version": "usdb-public-check:v1", "status": "PREFLIGHT_PASSED",
              "checkpoint": {"number": head_height, "hash": head["hash"]},
              "historical_sample": {"number": height, "hash": historical["hash"]}, "transaction": transaction,
              "reference_check": "passed" if "reference_url" in clients else "not_configured",
              "historical_state_sample": "passed", "trace_sample": "passed",
              "archive_replay_qualification": "not_run", "wallet_broadcast_acceptance": "not_run",
              "public_tls_acceptance": "not_run", "reward_supply_qualification": "not_run"}
    if transaction is None and "transaction" not in settings:
        pending_trace_sample(clients["trace_url"], head)
        for client in clients.values():
            same_checkpoint(client, head)
        same_checkpoint(read, historical)
        if head_height == 0:
            same_genesis_head(clients.values(), head)
        report.update(status="PREFLIGHT_READY_NO_TRANSACTION_SAMPLE",
                      chain_state="genesis_only" if head_height == 0 else "no_recent_transaction_sample",
                      historical_state_sample="genesis_only" if head_height == 0 else "passed",
                      trace_sample="pending_no_transaction_sample", trace_methods="available",
                      warnings=["No mined transaction found in the automatic sample window. Full Explorer may start; "
                                "receipt and callTracer execution validation are pending. Run check after transactions are mined, "
                                "or set rpc.transaction to an older canonical transaction and re-prepare. "
                                "This does not certify complete archive coverage or network synchronization."])
        return report
    if not isinstance(transaction, str) or not HASH.fullmatch(transaction):
        raise ValueError("invalid tracing sample; set rpc.transaction to an existing mined transaction")
    receipt = read("eth_getTransactionReceipt", [transaction])
    if not isinstance(receipt, dict) or str(receipt.get("transactionHash", "")).lower() != transaction.lower():
        raise ValueError("sample receipt is unavailable; choose a canonical mined rpc.transaction or remove it from the source config for automatic sampling, then prepare --replace")
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
        raise RpcFailure("RPC_INVALID_RESPONSE", "trace_url: RPC debug_traceTransaction: invalid callTracer result; check chain image and private proxy compatibility")
    traced_block = trace("debug_traceBlockByNumber", [sample_block["number"], {"tracer": "callTracer", "timeout": "5s"}])
    if (not isinstance(traced_block, list) or len(traced_block) != len(sample_block["transactions"])
            or any(not isinstance(item, dict) or item.get("error") or not isinstance(item.get("result"), dict)
                   or not isinstance(item["result"].get("type"), str) or not item["result"]["type"] for item in traced_block)):
        raise RpcFailure("RPC_INVALID_RESPONSE", "trace_url: RPC debug_traceBlockByNumber: invalid callTracer block results; check chain image and private proxy compatibility")
    for client in clients.values():
        same_checkpoint(client, head)
    same_checkpoint(read, historical)
    same_checkpoint(read, sample_block)
    same_checkpoint(trace, sample_block)
    return report


def check_explorer(config, identity, *, rpc_factory=ReadRpc, api_fetch=fetch, explorer_url=None):
    """Compare the public route and indexed data at a fixed upstream checkpoint."""
    origin = explorer_url if explorer_url is not None else config["ingress"]["explorer_url"]
    endpoint(origin, origin=True)
    origin = origin.rstrip("/")
    report = preflight(config, identity, rpc_factory=rpc_factory)
    report["ingress_check"] = "override_origin" if explorer_url is not None else "configured_origin"
    route = "check --url" if explorer_url is not None else "ingress.explorer_url"
    guidance = ("Upstream preflight passed. Check the Explorer origin, proxy listener, port forwarding and NAT loopback; "
                "use the actual visitor URL, not a documentation example. No mining is needed for a genesis RPC response.")
    public = named_rpc(rpc_factory(origin + "/rpc"), route + " /rpc", guidance=guidance)

    def api(path):
        try:
            return api_fetch(origin + path)
        except RpcFailure as error:
            raise RpcFailure(error.category, f"{route} /api/v2: {error.detail} "
                             "Check proxy, gateway and Blockscout backend readiness; absence of mined blocks does not explain a connection failure.") from None

    identity_check(public, identity)
    checkpoint = {"number": hex(report["checkpoint"]["number"]), "hash": report["checkpoint"]["hash"]}
    same_checkpoint(public, checkpoint)
    if report["checkpoint"]["number"] == 0:
        # Blockscout may omit genesis from its block index. Empty successful API
        # responses are valid here; stale blocks or mined transactions are not.
        for resource in ("blocks", "transactions?filter=validated"):
            page = api("/api/v2/" + resource)
            if not isinstance(page, dict) or not isinstance(page.get("items"), list) or page.get("next_page_params") is not None:
                raise ValueError("explorer returned an invalid genesis-only list response")
            if resource == "blocks":
                if any(not isinstance(item, dict) or item.get("height") != 0 or item.get("hash") != checkpoint["hash"] for item in page["items"]):
                    raise ValueError("explorer contains blocks beyond the genesis-only upstream")
            elif page["items"]:
                raise ValueError("explorer contains mined transactions beyond the genesis-only upstream")
    else:
        indexed = api("/api/v2/blocks/" + checkpoint["hash"])
        if indexed.get("hash") != checkpoint["hash"] or indexed.get("height") != report["checkpoint"]["number"]:
            raise ValueError("explorer has not indexed the observed upstream checkpoint")
    transaction = report["transaction"]
    if transaction is None:
        sources = [named_rpc(rpc_factory(config["rpc"][name]), name) for name in ("read_url", "trace_url", "broadcast_url", "reference_url")
                   if name in config["rpc"]]
        for client in [public, *sources]:
            same_checkpoint(client, checkpoint)
        if report["checkpoint"]["number"] == 0:
            same_genesis_head([public, *sources], checkpoint)
        report["status"] = "CHECKED_NO_TRANSACTION_SAMPLE"
        return report
    receipt = public("eth_getTransactionReceipt", [transaction])
    indexed_tx = api("/api/v2/transactions/" + transaction)
    if (not isinstance(receipt, dict) or indexed_tx.get("hash") != transaction
            or indexed_tx.get("block_number") != quantity(receipt.get("blockNumber"))
            or indexed_tx.get("result") != ("success" if quantity(receipt.get("status")) == 1 else "execution reverted")
            or str(indexed_tx.get("gas_used")) != str(quantity(receipt.get("gasUsed")))):
        raise ValueError("explorer transaction does not match the RPC receipt")
    canonical = block(public, receipt["blockNumber"])
    if canonical["hash"] != receipt["blockHash"] or transaction not in canonical["transactions"]:
        raise ValueError("public receipt is no longer canonical")
    source_receipt = named_rpc(rpc_factory(config["rpc"]["read_url"]), "read_url")("eth_getTransactionReceipt", [transaction])
    for field in ("transactionHash", "blockHash", "blockNumber", "status", "gasUsed", "effectiveGasPrice"):
        if not isinstance(source_receipt, dict) or receipt.get(field) != source_receipt.get(field):
            raise ValueError("public receipt differs from the configured upstream")
    fee = quantity(receipt.get("gasUsed")) * quantity(receipt.get("effectiveGasPrice"))
    if str(indexed_tx.get("fee", {}).get("value")) != str(fee):
        raise ValueError("explorer transaction fee differs from the canonical receipt")
    for name in ("read_url", "reference_url"):
        if name in config["rpc"]:
            client = named_rpc(rpc_factory(config["rpc"][name]), name)
            same_checkpoint(client, checkpoint)
            same_checkpoint(client, canonical)
    same_checkpoint(public, checkpoint)
    report["status"] = "CHECKED"
    return report

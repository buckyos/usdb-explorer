"""Compare host ingress paths without changing advertised origins, TLS trust or services."""
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import http.client
import ipaddress
import json
import socket
import ssl
import subprocess
import time
import urllib.request

from public_checks import NoRedirect, ReadRpc, RpcFailure, check_explorer, fetch, preflight, quantity
from public_config import endpoint


def host_addresses():
    """Read IPv4 addresses on default-route interfaces; never enumerate Docker bridge destinations."""
    try:
        routes = json.loads(subprocess.run(["ip", "-j", "-4", "route", "show", "default"],
                            check=True, capture_output=True, text=True, timeout=3).stdout)
        interfaces = json.loads(subprocess.run(["ip", "-j", "-4", "address", "show"],
                                check=True, capture_output=True, text=True, timeout=3).stdout)
        devices = {route["dev"] for route in routes if "dev" in route}
        result = set()
        for interface in interfaces:
            if interface["ifname"] not in devices or "UP" not in interface.get("flags", []):
                continue
            for item in interface.get("addr_info", []):
                if item.get("family") == "inet" and item.get("scope") == "global":
                    address = ipaddress.IPv4Address(item["local"])
                    if not (address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast):
                        result.add(str(address))
        if not result:
            return [], "No usable IPv4 address found on the host's default-route interfaces."
        addresses = sorted(result, key=ipaddress.IPv4Address)
        return addresses[:4], "Only the first four host addresses are checked." if len(addresses) > 4 else None
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        return [], "Host address discovery unavailable; install iproute2 or check the intended LAN origin explicitly with check --url."


def ingress_targets(config, *, discover=host_addresses):
    """Infer only listeners owned by bundled ingress; external proxies need explicit origins."""
    ingress = config["ingress"]
    origin = ingress["explorer_url"].rstrip("/")
    parsed = endpoint(origin, origin=True)
    configured = {"name": "configured", "origin": origin, "url": origin}
    if ingress["mode"] != "bundled":
        reason = "External proxy listener cannot be inferred from internal backend ports; use check --url for an explicit origin."
        return [{"name": name, "status": "SKIPPED", "reason": reason} for name in ("loopback", "lan")] + [configured], []
    bind = ipaddress.IPv4Address(ingress.get("bind_address", "127.0.0.1"))
    port = ingress.get("https_port", 28443) if parsed.scheme == "https" else ingress.get("http_port", 28080)
    def target(name, address):
        return {"name": name, "origin": origin, "url": f"{parsed.scheme}://{address}:{port}", "connect_to": [address, port]}
    targets, warnings = [], []
    if bind.is_unspecified or bind.is_loopback:
        targets.append(target("loopback", "127.0.0.1" if bind.is_unspecified else str(bind)))
    else:
        targets.append({"name": "loopback", "status": "SKIPPED", "reason": "Bundled ingress is bound to a specific non-loopback address."})
    if bind.is_loopback:
        targets.append({"name": "lan", "status": "SKIPPED", "reason": "Bundled ingress is bound to loopback only."})
    else:
        addresses, warning = discover() if bind.is_unspecified else ([str(bind)], None)
        if warning:
            warnings.append(warning)
        targets.extend(target("lan" if index == 0 else f"lan-{index + 1}", address) for index, address in enumerate(addresses))
        if not addresses:
            targets.append({"name": "lan", "status": "SKIPPED", "reason": warning or "No host address discovered."})
    return targets + [configured], warnings


def direct_opener(connect_to, *, context=None):
    """Connect locally while preserving the advertised HTTP Host and HTTPS SNI/certificate name."""
    destination = tuple(connect_to)

    class DirectHTTP(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.create_connection(destination, self.timeout, self.source_address)

    class DirectHTTPS(http.client.HTTPSConnection):
        def connect(self):
            sock = socket.create_connection(destination, self.timeout, self.source_address)
            try:
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
            except BaseException:
                sock.close()
                raise

    class HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, request):
            return self.do_open(DirectHTTP, request)

    class HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, request):
            return self.do_open(DirectHTTPS, request, context=context or ssl.create_default_context())

    # A local probe must not travel through environment proxies or follow a redirect back to the WAN.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), HTTPHandler(), HTTPSHandler(), NoRedirect())


def error_document(error):
    """Reuse sanitized RPC guidance and omit raw unexpected transport/payload exceptions."""
    if isinstance(error, RpcFailure):
        return {"category": error.category, "message": str(error)}
    if type(error) is ValueError:
        return {"category": "VALIDATION_ERROR", "message": str(error)}
    return {"category": "VALIDATION_ERROR", "message": "Unexpected response or local validation failure; inspect upstream and Explorer readiness."}


def probe_ingress(target, config, identity, upstream):
    """Separate basic reachability from strict canonical comparisons, including fresh-block index lag."""
    opener = direct_opener(target["connect_to"]) if "connect_to" in target else None
    transport = partial(fetch, opener=opener, timeout=5)
    origin = target["origin"]
    result = {"status": "PASSED", "checks": {"rpc": "NOT_RUN", "api": "NOT_RUN", "canonical": "NOT_RUN"}, "check_errors": {}}

    def rpc():
        if quantity(ReadRpc(origin + "/rpc", fetcher=transport)("eth_chainId", [])) != identity["chain_id"]:
            raise ValueError("public RPC chain ID differs from the frozen network")

    def api():
        page = transport(origin + "/api/v2/blocks")
        if not isinstance(page, dict) or not isinstance(page.get("items"), list):
            raise ValueError("explorer block list API returned an invalid response")

    def canonical():
        check_explorer(config, identity, preflight_report=upstream,
                       explorer_url=origin, api_fetch=transport,
                       public_rpc_factory=lambda url: ReadRpc(url, fetcher=transport), route_label="ingress." + target["name"])

    for name, operation in (("rpc", rpc), ("api", api), ("canonical", canonical)):
        if name == "canonical" and result["status"] == "FAILED":
            continue
        try:
            operation()
            result["checks"][name] = "PASSED"
        except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
            failure = error_document(error)
            result["checks"][name] = "FAILED"
            result["check_errors"][name] = failure
            result.update(status="FAILED")
            result.setdefault("error", failure)
    return result


def diagnosis(report):
    """Describe observed boundaries; a local pass never establishes external reachability or a NAT cause."""
    if report["upstream"]["status"] == "FAILED":
        return "Upstream preflight failed; resolve its capability/connection error before canonical ingress checks."
    rows = [row for row in report["ingress_results"] if row["status"] != "SKIPPED"]
    failed = [row for row in rows if row["status"] == "FAILED"]
    if not failed:
        return "All checked paths passed from this host. Verify the visitor origin separately from an external client; this is a point-in-time check."
    configured = next(row for row in rows if row["name"] == "configured")
    local_reachable = any(row["name"] != "configured" and (row["status"] == "PASSED" or
                          all(row.get("checks", {}).get(name) == "PASSED" for name in ("rpc", "api"))) for row in rows)
    if configured["status"] == "FAILED" and local_reachable:
        category = configured["error"]["category"]
        if category in {"RPC_TIMEOUT", "RPC_DNS", "RPC_CONNECTION", "RPC_CONNECTION_REFUSED"}:
            return ("Local Explorer RPC/API endpoints responded; the configured visitor path failed from this host. "
                    "Check DNS, HTTP proxy settings, routing, port forwarding, firewall and NAT loopback, and compare from an external client. "
                    "NAT is a possible cause, not confirmed. Inspect any canonical/indexing failures separately. Keep the actual visitor URL in configuration.")
        return "Local Explorer endpoints responded; inspect the specific TLS, HTTP, RPC or canonical/indexing errors before changing network settings."
    if configured["status"] == "PASSED":
        return "The configured visitor path passed, but a direct host path failed; inspect its bind address, host firewall and reported validation error."
    return "Ingress checks failed; inspect the proxy listener, service health and each reported RPC/API error. Upstream preflight passed."


def check_ingresses(config, identity, *, discover=host_addresses, upstream_check=preflight, probe=probe_ingress):
    """Collect all applicable results; failures remain failures even when another route passes."""
    targets, warnings = ingress_targets(config, discover=discover)
    started = time.monotonic()
    try:
        report = dict(upstream_check(config, identity))
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        failure = error_document(error)
        report = {"schema_version": "usdb-public-check:v1", "status": "CHECK_FAILED", "error": failure,
                  "upstream": {"status": "FAILED", "error": failure, "duration_ms": round((time.monotonic() - started) * 1000)}}
        report["ingress_results"] = [{**target, "status": "SKIPPED", "reason": "Upstream preflight failed; canonical comparisons were not run."}
                                     if "status" not in target else target for target in targets]
    else:
        upstream = dict(report)
        report["upstream"] = {"status": "PASSED", "duration_ms": round((time.monotonic() - started) * 1000)}

        def run(target):
            if target.get("status") == "SKIPPED":
                return dict(target)
            start = time.monotonic()
            result = dict(target)
            try:
                checked = probe(target, config, identity, upstream)
                result["status"] = "FAILED" if checked.get("status") == "FAILED" else "PASSED"
                for key in ("checks", "check_errors", "error"):
                    if key in checked:
                        result[key] = checked[key]
            except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
                result.update(status="FAILED", error=error_document(error))
            result["duration_ms"] = round((time.monotonic() - start) * 1000)
            return result

        with ThreadPoolExecutor(max_workers=4) as pool:
            report["ingress_results"] = list(pool.map(run, targets))
        failures = [row for row in report["ingress_results"] if row["status"] == "FAILED"]
        report["status"] = "CHECK_FAILED" if failures else (
            "CHECKED_NO_TRANSACTION_SAMPLE" if upstream.get("trace_sample") == "pending_no_transaction_sample" else "CHECKED")
        if failures:
            report["error"] = next((row["error"] for row in failures if row["name"] == "configured"), failures[0]["error"])
    report["ingress_check"] = "multiple_origins"
    report["warnings"] = list(report.get("warnings", [])) + warnings
    report["diagnosis"] = diagnosis(report)
    return report

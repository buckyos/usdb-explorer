"""Validate standalone deployment inputs and render optional ingress assets."""
from __future__ import annotations

import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

GIB = 1024**3
CONFIG_SCHEMA = "usdb-public-config:v1"
IMAGE_SCHEMA = "usdb-public-service-images:v1"
HASH = re.compile(r"0x[0-9a-fA-F]{64}")
DIGEST_IMAGE = re.compile(r"[a-z0-9./_-]+@sha256:[0-9a-f]{64}")


def read_json(path):
    """Reject duplicate keys instead of silently accepting a different configuration."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), object_pairs_hook=unique)


def fields(value, required, optional=()):
    """Catch misspelled settings before preparing persistent resources."""
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ValueError(f"invalid configuration fields; required={sorted(required)}, optional={sorted(optional)}")


def endpoint(value, *, origin=False):
    """Only accept unambiguous HTTP endpoints; keep credentials in the private network layer."""
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value) or any(c in value for c in '$;{}\\"'):
        raise ValueError("invalid HTTP(S) endpoint")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or re.fullmatch(r"[a-zA-Z0-9.:-]+", parsed.hostname) is None):
        raise ValueError("HTTP(S) endpoint without credentials, query or fragment required")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("invalid endpoint port")
    if origin and parsed.path not in {"", "/"}:
        raise ValueError("explorer_url must be an origin without a subpath")
    return parsed


def image_lock(root):
    """Release builds add a prebuilt gateway digest; source checkouts build it locally."""
    value = read_json(root / "assets/images.lock.json")
    required = {"backend", "frontend", "postgres", "redis", "nginx", "go"}
    if (value.get("schema_version") != IMAGE_SCHEMA or not isinstance(value.get("qualified_for_public_exposure"), bool)
            or set(value.get("images", {})) not in (required, required | {"gateway"})):
        raise ValueError("invalid public service image lock")
    for item in value["images"].values():
        if not isinstance(item, dict) or not DIGEST_IMAGE.fullmatch(str(item.get("reference", ""))):
            raise ValueError("every public service image must be pinned by digest")
    return value


def security_enforcement(network, requested=None):
    """Keep findings advisory on testnet; never let a mainnet request weaken enforcement."""
    if requested not in {None, "strict", "report-only"}:
        raise ValueError("security enforcement must be strict or report-only")
    if re.fullmatch(r"usdb-testnet-v[0-9]+", str(network)):
        return requested or "report-only"
    if re.fullmatch(r"usdb-mainnet-v[0-9]+", str(network)):
        if requested == "report-only":
            raise ValueError("mainnet security enforcement must remain strict")
        return "strict"
    raise ValueError("unsupported network security policy")


def load_config(path, kit):
    """Normalize one private configuration without reading a node installation."""
    value = read_json(path)
    fields(value, {"schema_version", "deployment_id", "network", "rpc", "ingress"}, {"resources"})
    if value["schema_version"] != CONFIG_SCHEMA or not re.fullmatch(r"[a-z][a-z0-9-]{1,47}", str(value["deployment_id"])):
        raise ValueError("invalid public configuration schema or deployment_id")
    if not re.fullmatch(r"usdb-testnet-v[0-9]+", str(value["network"])):
        raise ValueError("select a supported USDB testnet network")
    identity = read_json(kit / "networks" / (value["network"] + ".json"))
    if (identity.get("bundle_id") != value["network"] or type(identity.get("chain_id")) is not int
            or type(identity.get("network_id")) is not int or not HASH.fullmatch(str(identity.get("genesis_block_hash", "")))):
        raise ValueError("invalid frozen network identity")
    rpc = value["rpc"]
    fields(rpc, set(), {"mode", "read_url", "trace_url", "broadcast_url", "reference_url", "historical_block", "transaction"})
    # Missing mode preserves the original externally reachable RPC contract on upgrade.
    rpc.setdefault("mode", "external")
    if rpc["mode"] not in {"external", "local-node"}:
        raise ValueError("rpc.mode must be external or local-node")
    if rpc["mode"] == "local-node":
        rpc.setdefault("read_url", "http://127.0.0.1:8545")
    if "read_url" not in rpc:
        raise ValueError("rpc.read_url is required for external RPC mode")
    rpc.setdefault("trace_url", rpc["read_url"])
    rpc.setdefault("broadcast_url", rpc["read_url"])
    for key in ("read_url", "trace_url", "broadcast_url", "reference_url"):
        if key in rpc:
            parsed = endpoint(rpc[key])
            if rpc["mode"] == "local-node" and key != "reference_url":
                if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                    raise ValueError(f"local-node {key} must use an HTTP loopback endpoint")
    if "historical_block" in rpc and (type(rpc["historical_block"]) is not int or rpc["historical_block"] < 0):
        raise ValueError("historical_block must be a nonnegative integer")
    if "transaction" in rpc and not HASH.fullmatch(str(rpc["transaction"])):
        raise ValueError("transaction must be an existing mined transaction hash")
    ingress = value["ingress"]
    fields(ingress, {"mode", "explorer_url"}, {"bind_address", "web_port", "gateway_port", "http_port", "https_port", "tls", "exposure"})
    if ingress["mode"] not in {"external", "bundled"}:
        raise ValueError("ingress.mode must be external or bundled")
    ingress.setdefault("exposure", "private")
    if ingress["exposure"] not in {"private", "public"}:
        raise ValueError("ingress.exposure must be private or public")
    origin = endpoint(ingress["explorer_url"], origin=True)
    ingress["explorer_url"] = ingress["explorer_url"].rstrip("/")
    ingress.setdefault("bind_address", "127.0.0.1")
    address = ipaddress.ip_address(ingress["bind_address"])
    if address.version != 4:
        raise ValueError("this deployment currently supports IPv4 host bindings")
    if ingress["exposure"] == "private" and not address.is_loopback:
        raise ValueError("private deployments must bind loopback")
    if ingress["mode"] == "external" and not address.is_loopback:
        raise ValueError("external ingress requires loopback bindings behind the operator's proxy")
    if ingress["exposure"] == "public":
        if origin.scheme != "https" and security_enforcement(value["network"]) == "strict":
            raise ValueError("public exposure requires HTTPS")
        if security_enforcement(value["network"]) == "strict" and not image_lock(kit)["qualified_for_public_exposure"]:
            raise ValueError("strict public exposure requires qualified release images")
    for key, default in {"web_port": 28080, "gateway_port": 28081, "http_port": 28080, "https_port": 28443}.items():
        ingress.setdefault(key, default)
        if type(ingress[key]) is not int or not 1 <= ingress[key] <= 65535:
            raise ValueError(f"invalid ingress port: {key}")
    ports = [ingress["web_port"], ingress["gateway_port"]] if ingress["mode"] == "external" else [ingress["http_port"]]
    if ingress["mode"] == "bundled" and origin.scheme == "https":
        ports.append(ingress["https_port"])
        tls = ingress.get("tls", {})
        fields(tls, {"certificate_dir"})
        cert_dir = Path(tls["certificate_dir"]).expanduser().resolve()
        if any(c in str(cert_dir) for c in "$:\n\r"):
            raise ValueError("certificate paths cannot contain $, colon or newlines")
        # Mount the directory, not individual inodes, so atomic certificate replacement works on reload.
        for name in ("fullchain.pem", "privkey.pem"):
            if not (cert_dir / name).is_file() or not (cert_dir / name).resolve().is_relative_to(cert_dir):
                raise ValueError("certificate directory must contain readable fullchain.pem and privkey.pem; external symlinks are not supported")
        tls["certificate_dir"] = str(cert_dir)
    elif "tls" in ingress:
        raise ValueError("tls certificate_dir is only used by bundled HTTPS; external ingress owns its certificates")
    if len(set(ports)) != len(ports):
        raise ValueError("published ingress ports must be distinct")
    resources = value.setdefault("resources", {})
    fields(resources, set(), {"memory_budget_gib", "other_services_memory_gib"})
    resources.setdefault("memory_budget_gib", 6)
    resources.setdefault("other_services_memory_gib", "auto" if rpc["mode"] == "local-node" else 0)
    for key, number in resources.items():
        if key == "other_services_memory_gib" and number == "auto":
            continue
        if type(number) is not int or number < (6 if key == "memory_budget_gib" else 0):
            raise ValueError(f"invalid resource budget: {key}")
    return value, identity


def container_rpc(config):
    """Translate only the colocated node endpoints; host acceptance keeps loopback URLs."""
    rpc = dict(config["rpc"])
    if rpc.get("mode") == "local-node":
        for route in ("read", "trace", "broadcast"):
            rpc[route + "_url"] = "http://rpc-relay:8080/" + route
    return rpc


def local_rpc_config(config, *, host):
    """Bridge Docker to host loopback through a private socket, without a host TCP listener."""
    listen = "unix:/run/usdb-rpc/upstream.sock" if host else "8080"
    routes = []
    for route in ("read", "trace", "broadcast"):
        upstream = config["rpc"][route + "_url"] if host else "http://unix:/run/usdb-rpc/upstream.sock:/" + route
        parsed = endpoint(upstream) if host else None
        if host and not parsed.path:
            upstream += "/"
        # Use the configured node host, not rpc-relay, to satisfy the node's HTTP vhost policy.
        header = f"proxy_set_header Host {parsed.netloc};" if host else ""
        routes.append(f"    location = /{route} {{\n"
                      f"        if ($request_method != POST) {{ return 405; }}\n"
                      f"        {header}\n        proxy_pass {upstream};\n    }}\n")
    return ("worker_processes 1;\npid /tmp/nginx.pid;\nerror_log /dev/stderr warn;\n"
            "events { worker_connections 256; }\nhttp {\n    access_log off;\n    server_tokens off;\n"
            "    client_max_body_size 10m;\n    proxy_connect_timeout 5s;\n    proxy_read_timeout 120s;\n"
            "    proxy_send_timeout 120s;\n    proxy_http_version 1.1;\n    proxy_buffering off;\n"
            f"    server {{\n    listen {listen};\n"
            + ("" if host else "    location = /healthz { return 200 'relay ready'; }\n")
            + "".join(routes) + "    location / { return 404; }\n    }\n}\n")


def wallet_network(config, identity):
    """Use the advertised origin, independently of where TLS terminates."""
    origin = config["ingress"]["explorer_url"]
    return {"chainId": hex(identity["chain_id"]), "chainName": "USDB Testnet",
            "nativeCurrency": {"name": "USDB", "symbol": "USDB", "decimals": 18},
            "rpcUrls": [origin + "/rpc"], "blockExplorerUrls": [origin]}


def nginx_config(config, *, external=False):
    """Generate the same protected routing for an existing server or the optional container."""
    ingress = config["ingress"]
    origin = endpoint(ingress["explorer_url"], origin=True)
    host = "127.0.0.1" if ingress["bind_address"] == "0.0.0.0" else ingress["bind_address"]
    web = f"{host}:{ingress['web_port']}" if external else "frontend:3000"
    gateway = f"{host}:{ingress['gateway_port']}" if external else "gateway:8080"
    resolver = "" if external else "    resolver 127.0.0.11 valid=10s ipv6=off;\n"
    routes = resolver + f"""    set $usdb_gateway http://{gateway};
    set $usdb_frontend http://{web};
    client_max_body_size 256k;
    proxy_connect_timeout 5s;
    proxy_read_timeout 20s;
    proxy_send_timeout 20s;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    location = /rpc {{ proxy_pass $usdb_gateway/; }}
    location = /rpc/healthz {{ proxy_pass $usdb_gateway/healthz; }}
    location = /network.json {{ proxy_pass $usdb_gateway/network.json; }}
    location = /api {{ return 404; }}
    location /api/ {{ proxy_pass $usdb_gateway$request_uri; }}
    location /socket {{ return 404; }}
    location / {{ proxy_pass $usdb_frontend$request_uri; }}
"""
    if external:
        # Operators include only these locations inside an existing domain's server block.
        return "# Include inside the existing explorer server block; TLS and listeners remain operator-owned.\n" + routes
    listen = "    listen 8080;\n"
    redirect = ""
    if origin.scheme == "https":
        listen = "    listen 8443 ssl;\n    ssl_certificate /tls/fullchain.pem;\n    ssl_certificate_key /tls/privkey.pem;\n    ssl_protocols TLSv1.2 TLSv1.3;\n"
        redirect = f"server {{ listen 8080; server_name {origin.hostname}; return 308 {ingress['explorer_url']}$request_uri; }}\n"
    return f"events {{ worker_connections 512; }}\nhttp {{\nserver_tokens off;\n{redirect}server {{\n{listen}    server_name {origin.hostname};\n{routes}}}\n}}\n"


def compose_document(config, identity, lock, root):
    """Own only explorer services; upstream nodes are never a Compose dependency."""
    images = {key: value["reference"] for key, value in lock["images"].items()}
    ingress, rpc = config["ingress"], container_rpc(config)
    url = endpoint(ingress["explorer_url"], origin=True)
    credentials = read_json(root / "credentials.json")
    def service(image, memory, cpus, **options):
        return {"image": image, "platform": "linux/amd64", "restart": "unless-stopped", "mem_limit": memory,
                "cpus": cpus, "security_opt": ["no-new-privileges:true"],
                "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}, **options}
    import hashlib
    database_labels = {"io.usdb.genesis": identity["genesis_block_hash"],
                       "io.usdb.credentials-sha256": hashlib.sha256(credentials["database"].encode()).hexdigest()}
    services = {
        "postgres": service(images["postgres"], "1g", 1, networks=["database"], volumes=["postgres-data:/var/lib/postgresql/data"],
            environment={"POSTGRES_DB": "blockscout", "POSTGRES_USER": "blockscout", "POSTGRES_PASSWORD": credentials["database"]},
            command=["postgres", "-c", "shared_buffers=256MB", "-c", "max_connections=80"],
            healthcheck={"test": ["CMD-SHELL", "pg_isready -U blockscout -d blockscout"], "interval": "5s", "timeout": "3s", "retries": 30}),
        "redis": service(images["redis"], "128m", .25, networks=["database"], command=["redis-server", "--maxmemory", "64mb", "--maxmemory-policy", "allkeys-lru"]),
        "backend": service(images["backend"], "2g", 1.5, networks=["database", "app"],
            command=["sh", "-c", 'bin/blockscout eval "Elixir.Explorer.ReleaseTasks.create_and_migrate()" && bin/blockscout start'],
            volumes=["backend-data:/app/dets"], stop_grace_period="2m", extra_hosts=["host.docker.internal:host-gateway"],
            depends_on={"postgres": {"condition": "service_healthy"}, "redis": {"condition": "service_started"}},
            environment={"DATABASE_URL": f"postgresql://blockscout:{credentials['database']}@postgres:5432/blockscout",
                "ECTO_SSL_MODE": "disable", "ECTO_USE_SSL": "false", "SECRET_KEY_BASE": credentials["backend"],
                "PORT": "4000", "BLOCKSCOUT_HOST": url.netloc, "BLOCKSCOUT_PROTOCOL": url.scheme,
                "ETHEREUM_JSONRPC_VARIANT": "geth", "ETHEREUM_JSONRPC_HTTP_URL": rpc["read_url"],
                "ETHEREUM_JSONRPC_TRACE_URL": rpc["trace_url"], "ETHEREUM_JSONRPC_TRANSPORT": "http",
                "ETHEREUM_JSONRPC_DISABLE_ARCHIVE_BALANCES": "false", "ETHEREUM_JSONRPC_GETH_TRACE_BY_BLOCK": "true",
                "ETHEREUM_JSONRPC_DEBUG_TRACE_TRANSACTION_TIMEOUT": "5s", "ETHEREUM_JSONRPC_HTTP_TIMEOUT": "10000",
                "CHAIN_ID": str(identity["chain_id"]), "COIN": "USDB", "COIN_NAME": "USDB Testnet", "CHAIN_TYPE": "default",
                "DISABLE_MARKET": "true", "DISABLE_FILE_LOGGING": "true", "DISABLE_WEBAPP": "true",
                "API_V2_ENABLED": "true", "API_V1_READ_METHODS_DISABLED": "true", "API_V1_WRITE_METHODS_DISABLED": "true",
                "API_GRAPHQL_ENABLED": "false", "ACCOUNT_ENABLED": "false", "ADMIN_PANEL_ENABLED": "false",
                "POOL_SIZE": "10", "POOL_SIZE_API": "5", "ERL_FLAGS": "+S 2:2",
                "INDEXER_CATCHUP_BLOCKS_BATCH_SIZE": "10", "INDEXER_CATCHUP_BLOCKS_CONCURRENCY": "1",
                "INDEXER_INTERNAL_TRANSACTIONS_CONCURRENCY": "1", "INDEXER_COIN_BALANCES_CONCURRENCY": "1",
                "INDEXER_RECEIPTS_CONCURRENCY": "1", "INDEXER_DISABLE_BLOCK_REWARD_FETCHER": "true",
                "INDEXER_DISABLE_PENDING_TRANSACTIONS_FETCHER": "true", "INDEXER_DISABLE_BEACON_BLOB_FETCHER": "true",
                "INDEXER_DISABLE_WITHDRAWALS_FETCHER": "true", "REDIS_URL": "redis://redis:6379"}),
        "frontend": service(images["frontend"], "1g", .75, networks=["app"],
            environment={"HOSTNAME": "0.0.0.0", "NEXT_PUBLIC_NETWORK_NAME": "USDB Testnet", "NEXT_PUBLIC_NETWORK_SHORT_NAME": "USDB",
                "NEXT_PUBLIC_NETWORK_ID": str(identity["chain_id"]), "NEXT_PUBLIC_NETWORK_CURRENCY_NAME": "USDB",
                "NEXT_PUBLIC_NETWORK_CURRENCY_SYMBOL": "USDB", "NEXT_PUBLIC_NETWORK_CURRENCY_DECIMALS": "18",
                "NEXT_PUBLIC_NETWORK_RPC_URL": ingress["explorer_url"] + "/rpc", "NEXT_PUBLIC_IS_TESTNET": "true",
                "NEXT_PUBLIC_API_HOST": url.hostname, "NEXT_PUBLIC_API_PORT": str(url.port or ""), "NEXT_PUBLIC_API_PROTOCOL": url.scheme,
                "NEXT_PUBLIC_API_WEBSOCKET_PROTOCOL": "wss" if url.scheme == "https" else "ws",
                "NEXT_PUBLIC_APP_HOST": url.hostname, "NEXT_PUBLIC_APP_PORT": str(url.port or ""), "NEXT_PUBLIC_APP_PROTOCOL": url.scheme,
                "NEXT_PUBLIC_API_BASE_PATH": "/", "NEXT_PUBLIC_HOMEPAGE_CHARTS": "[]", "NEXT_PUBLIC_HOMEPAGE_STATS": "[]",
                "NEXT_PUBLIC_VIEWS_BLOCK_HIDDEN_FIELDS": '["burnt_fees","total_reward"]',
                "NEXT_PUBLIC_VIEWS_TX_HIDDEN_FIELDS": '["burnt_fees","gas_fees"]',
                "NEXT_PUBLIC_MAINTENANCE_ALERT_MESSAGE": "USDB 预览：奖励、供应量及手续费分账尚未验收；实时推送暂未开放。",
                "NEXT_PUBLIC_NETWORK_VERIFICATION_TYPE": "mining"}),
        "gateway": service(images.get("gateway", config["deployment_id"] + "-gateway:local"), "256m", .25, networks=["app"],
            environment={"RPC_UPSTREAM": rpc["read_url"], "BROADCAST_UPSTREAM": rpc["broadcast_url"],
                         "CHAIN_ID_HEX": hex(identity["chain_id"]), "GENESIS_HASH": identity["genesis_block_hash"],
                         "NETWORK_FILE": "/config/network.json"},
            extra_hosts=["host.docker.internal:host-gateway"], volumes=[f"{root}/network.json:/config/network.json:ro"],
            read_only=True, cap_drop=["ALL"]),
    }
    if "gateway" not in images:
        services["gateway"]["build"] = {"context": str(root / "build"), "dockerfile": "Dockerfile.gateway", "args": {"GO_IMAGE": images["go"]}}
    binding = ingress["bind_address"]
    if ingress["mode"] == "external":
        services["frontend"]["ports"] = [f"{binding}:{ingress['web_port']}:3000"]
        services["gateway"]["ports"] = [f"{binding}:{ingress['gateway_port']}:8080"]
    else:
        ports = [f"{binding}:{ingress['http_port']}:8080"]
        volumes = [f"{root}/nginx.conf:/etc/nginx/nginx.conf:ro"]
        if url.scheme == "https":
            ports.append(f"{binding}:{ingress['https_port']}:8443")
            volumes.append(f"{ingress['tls']['certificate_dir']}:/tls:ro")
        services["proxy"] = service(images["nginx"], "128m", .25, networks=["app"], ports=ports, volumes=volumes,
            depends_on={name: {"condition": "service_started"} for name in ("frontend", "gateway")})
    document = {"name": config["deployment_id"], "services": services,
            "networks": {"database": {"internal": True}, "app": {}},
            "volumes": {"postgres-data": {"labels": database_labels}, "backend-data": {}}}
    if rpc.get("mode") == "local-node":
        document["networks"]["rpc"] = {"internal": True}
        document["volumes"]["rpc-socket"] = {}
        for name in ("rpc-host", "rpc-relay"):
            services[name] = service(images["nginx"], "64m", .25,
                entrypoint=["nginx", "-g", "daemon off;"], read_only=True,
                cap_drop=["NET_RAW", "NET_BIND_SERVICE"],
                tmpfs=["/tmp:size=16m", "/var/cache/nginx:size=16m"],
                volumes=[f"{root}/{name}.conf:/etc/nginx/nginx.conf:ro",
                         "rpc-socket:/run/usdb-rpc" + (":ro" if name == "rpc-relay" else "")])
        services["rpc-host"]["network_mode"] = "host"
        services["rpc-host"]["healthcheck"] = {"test": ["CMD", "test", "-S", "/run/usdb-rpc/upstream.sock"],
                                              "interval": "2s", "timeout": "2s", "retries": 15}
        services["rpc-relay"].update(networks=["rpc"], depends_on={"rpc-host": {"condition": "service_healthy"}},
            healthcheck={"test": ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/healthz"],
                         "interval": "2s", "timeout": "2s", "retries": 15})
        for name in ("backend", "gateway"):
            services[name]["networks"].append("rpc")
            services[name].pop("extra_hosts")
            services[name].setdefault("depends_on", {})["rpc-relay"] = {"condition": "service_healthy"}
    return document

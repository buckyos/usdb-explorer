#!/usr/bin/env python3
"""Exercise the built frontend without upstream nodes, credentials or public listeners.

Run with a pinned Playwright installation (CI installs playwright==1.59.0).
"""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

HASH = "a" * 64
PASS = HASH + "i0"
SCHEMA = "usdb-explorer-public:v1"
STATE = {"btc_height": 963900, "stable_block_hash": HASH, "snapshot_id": HASH,
         "system_state_id": HASH, "activation_registry_id": HASH}
PROFILE = {"pass_id": PASS, "owner_script_hash": HASH, "owner_btc_addr": "bc1qfixture",
           "usdb_main": "0x" + "11" * 20, "state": "active", "pass_kind": "standard",
           "raw_energy": "340282366920938463463374607431768211455", "collab_contribution": "0",
           "effective_energy": "340282366920938463463374607431768211455", "level": 1,
           "difficulty_factor_bps": 9900, "collab_breakdown_count": 0}


def response(resource, mode):
    base = {"schema_version": SCHEMA, "updated_at": "2026-09-19T00:00:00Z"}
    if resource.startswith("blocks/"):
        if mode == "unavailable":
            return 503, {**base, "error": {"code": "ECONOMICS_NODE_UPGRADE_REQUIRED"}}
        if mode == "reorg":
            return 409, {**base, "error": {"code": "BLOCK_NOT_CANONICAL"}}
        report = {"status": "genesis_not_applicable" if "/0/" in resource else "verified",
                  "block_hash": "0x" + HASH, "block_number": "0" if "/0/" in resource else "215", "parent_hash": "0x" + "b" * 64,
                  "state_root": "0x" + HASH, "receipts_root": "0x" + HASH, "miner": PROFILE["usdb_main"], "dividend": "0x" + "22" * 20,
                  "versions": {"rewardRuleVersion": 1, "feeSplitPolicyVersion": 1},
                  "selector": {**STATE, "pass_id": PASS, "btc_anchor_age_blocks": 2},
                  "amounts": {"issued_before_atoms": "9007199254740993000000000000", "issued_after_atoms": "9007199254740993000000000001",
                              "emission_atoms": "1", "miner_emission_atoms": "1", "fees_atoms": "63000", "miner_fees_atoms": "37800", "dao_fees_atoms": "25200"},
                  "transactions": [] if mode == "empty" else [{"hash": "0x" + HASH, "status": 0, "gas_used": "21000", "effective_gas_price_atoms": "3",
                                                              "fee_atoms": "63000", "miner_fee_atoms": "37800", "dao_fee_atoms": "25200", "fee_route": "miner_and_dividend"}]}
        if report["status"] == "genesis_not_applicable":
            for key in ("amounts", "selector", "versions"):
                report.pop(key)
            report["transactions"] = []
        if mode == "empty" and "amounts" in report:
            report["amounts"].update(fees_atoms="0", miner_fees_atoms="0", dao_fees_atoms="0")
        return 200, {**base, "economics": report}
    if mode == "reorg":
        return 409, {**base, "error": {"code": "STATE_CHANGED"}}
    if mode == "unavailable":
        return 503, {**base, "error": {"code": "INDEXER_NOT_READY"}}
    if resource.startswith("overview"):
        return 200, {**base, "network": {"name": "USDB Testnet", "chain_id": "202608250", "chain_id_hex": hex(202608250),
                     "genesis_hash": "0x" + HASH, "bundle_id": "usdb-testnet-v0", "btc_network": "btc-mainnet",
                     "btc_index_origin_height": 963800, "rpc_urls": ["http://127.0.0.1:28080/rpc"],
                     "explorer_urls": ["http://127.0.0.1:28080"],
                     "native_currency": {"name": "USDB", "symbol": "USDB", "decimals": 18}},
                     "chain": {"height": "215", "hash": "0x" + HASH, "timestamp": "1789776000"},
                     "explorer": {"status": "ready", "height": "213", "hash": "0x" + HASH},
                     "indexer": {"status": "ready", "btc_height": 963900, "btc_stable_height": 963901}}
    if resource.startswith("passes/"):
        if "b" * 64 in resource:
            return 404, {**base, "error": {"code": "PASS_NOT_FOUND"}}
        return 200, {**base, "external_state": STATE, "pass": PROFILE,
                     "inscription": {"mint_block_height": 963810, "leader_pass_id": None, "leader_btc_addr": None, "prev": []}}
    return 200, {**base, "external_state": STATE, "total": "0" if mode == "empty" else "2",
                 "next_cursor": "opaque-cursor" if mode != "empty" and "cursor=" not in resource else None,
                 "items": [] if mode == "empty" else [PROFILE]}


@contextmanager
def frontend(image):
    # Upstream's Playwright mode lengthens tooltip closeDelay so automated
    # pointer transitions can reach the interactive sidebar submenu.
    env = {"HOSTNAME": "0.0.0.0", "NEXT_PUBLIC_NETWORK_NAME": "USDB Testnet", "NEXT_PUBLIC_NETWORK_SHORT_NAME": "USDB",
           "NEXT_PUBLIC_APP_INSTANCE": "pw",
           "NEXT_PUBLIC_NETWORK_ID": "202608250", "NEXT_PUBLIC_IS_TESTNET": "true", "NEXT_PUBLIC_APP_HOST": "localhost",
           "NEXT_PUBLIC_APP_PROTOCOL": "http", "NEXT_PUBLIC_API_HOST": "localhost", "NEXT_PUBLIC_API_PROTOCOL": "http",
           "NEXT_PUBLIC_API_BASE_PATH": "/", "NEXT_PUBLIC_API_WEBSOCKET_PROTOCOL": "ws", "NEXT_PUBLIC_NETWORK_RPC_URL": "http://localhost/rpc",
           "NEXT_PUBLIC_NETWORK_CURRENCY_NAME": "USDB", "NEXT_PUBLIC_NETWORK_CURRENCY_SYMBOL": "USDB",
           "NEXT_PUBLIC_NETWORK_CURRENCY_DECIMALS": "18", "NEXT_PUBLIC_HOMEPAGE_CHARTS": "[]",
           "NEXT_PUBLIC_HOMEPAGE_STATS": "[]", "NEXT_PUBLIC_AD_BANNER_PROVIDER": "none", "NEXT_PUBLIC_AD_TEXT_PROVIDER": "none",
           "DISABLE_TRACKING": "true", "NEXT_TELEMETRY_DISABLED": "1"}
    cmd = ["docker", "run", "-d", "--rm", "--memory", "2g", "-p", "127.0.0.1::3000"]
    for name, value in env.items():
        cmd.extend(["-e", name + "=" + value])
    container = subprocess.check_output([*cmd, image], text=True).strip()
    try:
        endpoint = subprocess.check_output(["docker", "port", container, "3000"], text=True).strip()
        url = "http://" + endpoint
        for _ in range(120):
            try:
                with urllib.request.urlopen(url + "/api/healthz", timeout=2) as request:
                    if request.status == 200:
                        break
            except (OSError, urllib.error.HTTPError):
                time.sleep(1)
        else:
            raise RuntimeError(subprocess.check_output(["docker", "logs", container], text=True))
        yield url
    finally:
        subprocess.run(["docker", "stop", "-t", "2", container], check=False, stdout=subprocess.DEVNULL)


def exercise(url, output):
    from playwright.sync_api import sync_playwright, expect
    mode = {"value": "ready"}
    requests = []
    faucet = {"status": "ready", "claim_status": "queued", "applications": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1100}, service_workers="block")
        # Record construction as well as network events, including attempts that
        # the browser rejects before opening a connection.
        context.add_init_script("""window.__socketAttempts = [];
          window.WebSocket = new Proxy(window.WebSocket, {
            construct(target, args) {
              window.__socketAttempts.push(String(args[0]));
              return Reflect.construct(target, args);
            }
          });""")
        def route_request(route):
            target = route.request.url
            if "/api/faucet/v1/" in target:
                resource = target.split("/api/faucet/v1/", 1)[1]
                if resource == "status":
                    route.fulfill(json={"enabled": faucet["status"] != "disabled", "status": faucet["status"],
                        "address": "0x" + "22" * 20, "claim_amount": "1", "daily_budget": "100", "cooldown_seconds": 86400, "confirmations": 3})
                elif route.request.method == "POST":
                    application = route.request.post_data_json
                    faucet["applications"].append(application)
                    if faucet.get("lose_response"):
                        faucet["lose_response"] = False
                        route.abort()
                    elif faucet.get("reject"):
                        route.fulfill(status=429, json={"error": {"code": faucet["reject"]}})
                    else:
                        route.fulfill(status=202, json={"id": "c_" + application["request_id"], "address": application["address"], "status": "queued"})
                elif faucet["applications"]:
                    application = faucet["applications"][-1]
                    route.fulfill(json={"id": "c_" + application["request_id"], "address": application["address"],
                                        "status": faucet["claim_status"], "transaction_hash": "0x" + HASH})
                else:
                    route.fulfill(status=404, json={"error": {"code": "NOT_FOUND"}})
            elif "/api/usdb/v1/" in target:
                resource = target.split("/api/usdb/v1/", 1)[1]
                requests.append(resource)
                status, body = response(resource, mode["value"])
                route.fulfill(status=status, json=body)
            elif target.startswith(url):
                route.continue_()
            else:
                route.abort()
        context.route("**/*", route_request)
        page = context.new_page()
        errors = []
        sockets = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("websocket", lambda socket: sockets.append(socket.url))
        page.goto(url + "/usdb", wait_until="domcontentloaded")
        expect(page.get_by_role("heading", name="Network overview", exact=True)).to_be_visible()
        expect(page.get_by_text("2 blocks behind the observed chain head", exact=True)).to_be_visible()
        assert not page.evaluate("window.__socketAttempts"), "Realtime is unavailable; WebSocket constructed"
        assert not sockets, f"Realtime is unavailable; unexpected WebSocket attempts: {sockets}"
        page.get_by_role("button", name="Add network to wallet").click()
        expect(page.get_by_text("Open this page in a compatible wallet", exact=False)).to_be_visible()
        page.screenshot(path=str(output / "network-overview.png"), full_page=True)
        page.locator('[aria-label="USDB link group"]:visible').hover()
        expect(page.get_by_role("link", name="Miner Passes link", exact=True).first).to_be_visible()
        page.get_by_role("link", name="Miner Passes link", exact=True).first.click()
        expect(page.get_by_role("heading", name="Active standard passes")).to_be_visible()
        expect(page.get_by_role("cell", name="340,282,366,920,938,463,463,374,607,431,768,211,455", exact=True)).to_be_visible()
        page.get_by_role("button", name="Next page", exact=True).click()
        expect(page.get_by_role("button", name="First page at this height")).to_be_visible()
        assert any("height=963900" in r and "state=" + HASH in r and "cursor=opaque-cursor" in r for r in requests)
        page.get_by_role("link", name="aaaaaaaaaaaa…aaaaaai0", exact=True).click()
        expect(page.get_by_role("heading", name="Pass details")).to_be_visible()
        expect(page.get_by_text(PROFILE["usdb_main"], exact=True)).to_be_visible()
        page.screenshot(path=str(output / "miner-pass.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(output / "miner-pass-mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), "horizontal page overflow"
        page.get_by_label("Inscription ID", exact=True).fill("b" * 64 + "i0")
        page.get_by_role("button", name="Search", exact=True).click()
        expect(page.locator('section [role="alert"]')).to_contain_text("was not found")
        mode["value"] = "empty"
        page.get_by_role("button", name="Latest active passes").click()
        expect(page.get_by_text("No active standard passes at this Bitcoin height.", exact=True)).to_be_visible()
        mode["value"] = "unavailable"
        page.reload(wait_until="domcontentloaded")
        expect(page.locator('section [role="alert"]')).to_contain_text("catching up or recovering")
        mode["value"] = "reorg"
        page.get_by_role("button", name="Retry", exact=True).click()
        expect(page.locator('section [role="alert"]')).to_contain_text("historical state changed")
        mode["value"] = "ready"
        page.set_viewport_size({"width": 1440, "height": 1100})
        page.goto(url + "/usdb/economics?block=215", wait_until="domcontentloaded")
        expect(page.get_by_role("heading", name="Block economics", exact=True)).to_be_visible()
        expect(page.get_by_text("Verified for this block:", exact=False)).to_be_visible()
        expect(page.get_by_role("cell", name="Reverted", exact=True)).to_be_visible()
        expect(page.get_by_text("9,007,199,254.740993000000000001 USDB", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name=PASS, exact=True)).to_have_attribute("href", "/usdb/passes?id=" + PASS + "&height=963900&state=" + HASH)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), "economics desktop overflow"
        page.screenshot(path=str(output / "block-economics.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(output / "block-economics-mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), "economics horizontal overflow"
        mode["value"] = "empty"
        page.get_by_role("button", name="Verify block", exact=True).click()
        expect(page.get_by_text("This block has no transactions.", exact=False)).to_be_visible()
        mode["value"] = "unavailable"
        page.get_by_role("button", name="Verify block", exact=True).click()
        expect(page.locator('section [role="alert"]')).to_contain_text("upgrade USDB-chain")
        expect(page.get_by_role("heading", name="New emission", exact=True)).to_have_count(0)
        mode["value"] = "reorg"
        page.get_by_role("button", name="Retry", exact=True).click()
        expect(page.locator('section [role="alert"]')).to_contain_text("no longer canonical")
        mode["value"] = "ready"
        page.get_by_label("USDB block number or hash").fill("0")
        page.get_by_role("button", name="Verify block", exact=True).click()
        expect(page.get_by_text("Genesis has no mined block reward", exact=False)).to_be_visible()
        expect(page.get_by_role("heading", name="New emission", exact=True)).to_have_count(0)
        page.set_viewport_size({"width": 1440, "height": 1100})
        page.goto(url + "/usdb/faucet", wait_until="domcontentloaded")
        expect(page.get_by_role("heading", name="Testnet faucet", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Request test USDB", exact=True)).to_be_enabled()
        page.get_by_label("Receiving address", exact=True).fill(PROFILE["usdb_main"])
        faucet["lose_response"] = True
        page.get_by_role("button", name="Request test USDB", exact=True).click()
        expect(page.locator('section [role="alert"]')).to_contain_text("Retry the same application")
        page.get_by_role("button", name="Retry same application", exact=True).click()
        expect(page.get_by_text("Queued for transfer", exact=True)).to_be_visible()
        assert faucet["applications"][0] == faucet["applications"][1], "retry created another application"
        page.screenshot(path=str(output / "faucet.png"), full_page=True)
        faucet["claim_status"] = "confirmed"
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_text("Test USDB delivered", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="View transfer in explorer")).to_have_attribute("href", "/tx/0x" + HASH)
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(output / "faucet-mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), "faucet horizontal overflow"
        page.get_by_role("button", name="Use another address", exact=True).click()
        faucet["reject"] = "ADDRESS_COOLDOWN"
        page.get_by_label("Receiving address", exact=True).fill(PROFILE["usdb_main"])
        page.get_by_role("button", name="Request test USDB", exact=True).click()
        expect(page.locator('section [role="alert"]')).to_contain_text("cooldown")
        page.get_by_role("button", name="Use another address", exact=True).click()
        faucet["status"] = "INSUFFICIENT_FUNDS"
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_text("The faucet needs a refill.", exact=False)).to_be_visible()
        expect(page.get_by_role("button", name="Request test USDB", exact=True)).to_be_disabled()
        faucet["status"] = "disabled"
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_text("This explorer does not currently offer a faucet.", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Request test USDB", exact=True)).to_have_count(0)
        assert not errors, errors
        assert not sockets, f"Page navigation attempted unavailable WebSockets: {sockets}"
        browser.close()
    print("Frontend browser checks passed: overview, passes, economics, faucet, idempotent retry, recovery, mobile, unavailable, reorg.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--screenshots", type=Path, default=Path(tempfile.gettempdir()) / "usdb-explorer-browser")
    args = parser.parse_args()
    args.screenshots.mkdir(parents=True, exist_ok=True)
    with frontend(args.image) as url:
        exercise(url, args.screenshots)


if __name__ == "__main__":
    main()

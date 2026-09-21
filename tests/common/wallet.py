"""Browser wallet fixtures; no real extension, accounts, signatures or transactions."""

WALLET = """window.__walletCalls = [];
window.__walletError = null;
window.__walletHold = false;
window.ethereum = {
  isMetaMask: true, _events: {}, on() {}, removeListener() {},
  async request(request) {
    window.__walletCalls.push(request);
    if (!['wallet_addEthereumChain', 'wallet_switchEthereumChain'].includes(request.method)) {
      throw new Error('Unexpected wallet method: ' + request.method);
    }
    if (window.__walletError) { throw window.__walletError; }
    if (window.__walletHold) {
      return new Promise(resolve => { window.__walletResolve = resolve; });
    }
    return null;
  }
};"""


def exercise_wallet(page, context, expect, network, output):
    """Test real rendered callbacks, both entrypoints and the shared pending guard."""
    context.add_init_script(WALLET)
    page.reload(wait_until="domcontentloaded")
    button = page.get_by_role("button", name="Add network to wallet", exact=True)
    footer = page.get_by_role("button", name="Add USDB Testnet", exact=True)
    feedback = page.locator("#wallet-network-feedback")
    expect(button).to_be_enabled()
    button.click()
    expect(feedback).to_contain_text("Network is available in your wallet")
    calls = page.evaluate("window.__walletCalls")
    assert len(calls) == 1 and calls[0]["method"] == "wallet_addEthereumChain"
    assert calls[0]["params"][0]["chainId"] == hex(202608250)
    assert calls[0]["params"][0]["nativeCurrency"]["symbol"] == "USDB"
    assert calls[0]["params"][0]["rpcUrls"] == ["http://127.0.0.1:28080/rpc"]

    for error, message in (
        ({"code": 4001}, "cancelled in your wallet"),
        ({"code": -32002}, "already pending"),
        ({"code": 4200}, "does not support automatic network setup"),
        ({"code": -32601}, "does not support automatic network setup"),
        ({"code": 4100}, "not authorized"),
        ({"code": 4900}, "disconnected"),
        ({"code": 4901}, "disconnected"),
        ({"code": -32602}, "rejected the network settings"),
        ({"code": -32603, "data": {"originalError": {"code": "4001"}}}, "cancelled in your wallet"),
        ({"code": -32603, "message": "private upstream detail"}, "could not complete the network request"),
    ):
        page.evaluate("error => { window.__walletError = error; }", error)
        button.click()
        expect(feedback).to_contain_text(message)
        expect(button).to_be_enabled()
        assert "private upstream detail" not in page.locator("body").inner_text()

    page.evaluate("window.__walletError = null; window.__walletHold = true; window.__walletCalls = []")
    button.click()
    expect(page.get_by_role("button", name="Waiting for wallet…", exact=True)).to_be_disabled()
    expect(feedback).to_contain_text("Open your wallet to review")
    # A second entrypoint cannot issue another request while Overview is waiting.
    footer.click()
    expect(page.get_by_text("A wallet request is already pending.", exact=False).last).to_be_visible()
    assert len(page.evaluate("window.__walletCalls")) == 1
    page.evaluate("window.__walletHold = false; window.__walletResolve(null)")
    expect(feedback).to_contain_text("Network is available in your wallet")
    expect(button).to_be_enabled()

    page.evaluate("window.__walletError = {code: 4001}")
    footer.click()
    expect(page.get_by_text("Network request cancelled in your wallet.", exact=False).last).to_be_visible()
    page.evaluate("window.__walletError = null; window.__walletCalls = []")
    footer.click()
    expect(page.get_by_text("Successfully added network to your wallet", exact=True)).to_be_visible()
    assert [item["method"] for item in page.evaluate("window.__walletCalls")] == ["wallet_addEthereumChain", "wallet_switchEthereumChain"]

    for urls, message in (
        ({"rpc_urls": ["http://usdb-testnet.example:28080/rpc"]}, "HTTPS public RPC URL"),
        ({"rpc_urls": ["http://localhost.example/rpc"]}, "HTTPS public RPC URL"),
        ({"rpc_urls": ["http://192.168.1.119:28080/rpc"]}, "HTTPS public RPC URL"),
        ({"rpc_urls": []}, "Public RPC settings are missing"),
        ({"rpc_urls": ["not-a-url"]}, "network URLs are invalid"),
        ({"rpc_urls": ["https://rpc.example/rpc"], "explorer_urls": ["http://explorer.example"]}, "HTTPS public Explorer URL"),
    ):
        network.clear()
        network.update(urls)
        page.reload(wait_until="domcontentloaded")
        expect(feedback).to_contain_text(message)
        expect(button).to_be_disabled()
        assert page.evaluate("window.__walletCalls") == []
        if urls.get("rpc_urls") == ["http://usdb-testnet.example:28080/rpc"]:
            page.screenshot(path=str(output / "wallet-https-required.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            page.screenshot(path=str(output / "wallet-https-required-mobile.png"), full_page=True)
            page.set_viewport_size({"width": 1440, "height": 1100})

    for rpc, explorer in (("http://localhost:28080/rpc", "http://localhost:28080"),
                          ("https://rpc.example:28443/rpc", "https://explorer.example:28443")):
        network.update(rpc_urls=[rpc], explorer_urls=[explorer])
        page.reload(wait_until="domcontentloaded")
        expect(button).to_be_enabled()
        button.click()
        expect(feedback).to_contain_text("Network is available in your wallet")
        assert page.evaluate("window.__walletCalls[0].params[0].rpcUrls") == [rpc]
    network.clear()
    page.reload(wait_until="domcontentloaded")


def exercise_public_wallet(url, output, overview):
    """The upstream button must reject an advertised HTTP public RPC before the provider call."""
    from playwright.sync_api import sync_playwright, expect
    overview["network"].update(rpc_urls=["http://usdb-testnet.example:28080/rpc"],
                               explorer_urls=["http://usdb-testnet.example:28080"])
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1100}, service_workers="block")
        context.add_init_script(WALLET)
        def route_request(route):
            if "/api/usdb/v1/overview" in route.request.url:
                route.fulfill(json=overview)
            elif route.request.url.startswith(url):
                route.continue_()
            else:
                route.abort()
        context.route("**/*", route_request)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url + "/usdb", wait_until="domcontentloaded")
        expect(page.locator("#wallet-network-feedback")).to_contain_text("HTTPS public RPC URL")
        expect(page.get_by_role("button", name="Add network to wallet", exact=True)).to_be_disabled()
        page.get_by_role("button", name="Add USDB Testnet", exact=True).click()
        expect(page.get_by_text("Automatic network setup requires an HTTPS public RPC URL.", exact=False).last).to_be_visible()
        assert page.evaluate("window.__walletCalls") == []
        assert not errors, errors
        page.screenshot(path=str(output / "wallet-upstream-https-required.png"), full_page=True)
        browser.close()

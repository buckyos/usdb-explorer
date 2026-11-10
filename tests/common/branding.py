"""Check shipped branding assets and rendered layouts through the production frontend."""
from pathlib import Path
import re
import struct
from urllib.parse import urlsplit


def exercise_branding(page, context, expect, url, output):
    original = (Path(__file__).resolve().parents[2] / "frontend/overlay/public/assets/usdb/logo.png").read_bytes()
    response = context.request.get(url + "/assets/usdb/logo.png")
    assert response.ok and response.body() == original, "public logo differs from approved source"
    for name, size in (("favicon-16x16.png", 16), ("favicon-32x32.png", 32), ("favicon-48x48.png", 48),
                       ("apple-touch-icon-180x180.png", 180), ("android-chrome-192x192.png", 192),
                       ("android-chrome-512x512.png", 512)):
        asset = context.request.get(url + "/assets/favicon/" + name)
        assert asset.ok and asset.body()[:8] == b"\x89PNG\r\n\x1a\n", name
        assert struct.unpack(">II", asset.body()[16:24]) == (size, size), name
    icon = context.request.get(url + "/favicon.ico")
    assert icon.ok and icon.body()[:4] == b"\x00\x00\x01\x00"
    assert icon.body() == context.request.get(url + "/assets/favicon/favicon.ico").body()
    manifest = context.request.get(url + "/assets/favicon/manifest.webmanifest").json()
    assert manifest["name"] == "USDB Explorer" and manifest["start_url"] == "/usdb"
    for item in manifest["icons"]:
        assert context.request.get(url + item["src"]).ok, item
    expect(page.locator('link[rel="manifest"]')).to_have_attribute("href", "/assets/favicon/manifest.webmanifest")
    share = page.locator('meta[property="og:image"]').get_attribute("content")
    assert share and context.request.get(url + urlsplit(share).path).body() == original
    for theme in ("light", "dark"):
        context.add_cookies([{"name": "chakra-ui-color-mode", "value": theme, "url": url},
                             {"name": "chakra-ui-color-theme", "value": theme, "url": url}])
        page.evaluate("theme => { localStorage.setItem('theme', theme); localStorage.setItem('chakra-ui-color-mode', theme); }", theme)
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("html")).to_have_class(re.compile(theme))
        for width, height, label in ((1440, 1100, "desktop"), (1024, 900, "compact"), (390, 844, "mobile")):
            page.set_viewport_size({"width": width, "height": height})
            logo = page.locator('[data-testid="usdb-brand"]:visible img').first
            expect(logo).to_be_visible()
            expect(logo).to_have_js_property("naturalWidth", 512)
            assert logo.evaluate("image => getComputedStyle(image).filter") == "none", "dark mode inverted the logo"
            expect(page.get_by_role("img", name="USDB coin", exact=True)).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), label
            page.screenshot(path=str(output / f"branding-{theme}-{label}.png"), full_page=True)
    context.clear_cookies()
    page.evaluate("localStorage.setItem('theme', 'light'); localStorage.setItem('chakra-ui-color-mode', 'light')")
    page.set_viewport_size({"width": 1440, "height": 1100})
    page.reload(wait_until="domcontentloaded")

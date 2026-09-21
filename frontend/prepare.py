#!/usr/bin/env python3
"""Verify upstream source and apply the small Explorer-owned overlay."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent


def replace_once(root, name, before, after):
    path = root / name
    value = path.read_text()
    if value.count(before) != 1:
        raise ValueError(f"upstream patch no longer applies: {name}")
    path.write_text(value.replace(before, after))


def prepare(archive, destination):
    lock = json.loads((ROOT / "upstream.lock.json").read_text())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != lock["archive_sha256"]:
        raise ValueError("upstream archive checksum mismatch")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        for entry in source:
            parts = PurePosixPath(entry.name).parts
            if not parts or parts[0] != "frontend-" + lock["revision"] or ".." in parts or entry.name.startswith("/"):
                raise ValueError("unsafe upstream archive path")
            target = destination.joinpath(*parts[1:])
            if entry.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif entry.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(entry) as stream, target.open("wb") as output:
                    shutil.copyfileobj(stream, output)
                target.chmod(entry.mode & 0o777)
            else:
                raise ValueError("unsupported upstream archive member")
    replace_once(destination, "lib/hooks/useNavItems.tsx",
                 "    const mainNavItems: ReturnType['mainNavItems'] = [",
                 """    const mainNavItems: ReturnType['mainNavItems'] = [
      {
        text: 'USDB', icon: 'globe-b' as const,
        isActive: pathname === '/usdb' || pathname === '/usdb/passes' || pathname === '/usdb/economics' || pathname === '/usdb/faucet',
        subItems: [
          { text: 'Network overview', nextRoute: { pathname: '/usdb' as const }, icon: 'globe-b' as const, isActive: pathname === '/usdb' },
          { text: 'Miner Passes', nextRoute: { pathname: '/usdb/passes' as const }, icon: 'block' as const, isActive: pathname === '/usdb/passes' },
          { text: 'Block economics', nextRoute: { pathname: '/usdb/economics' as const }, icon: 'block' as const, isActive: pathname === '/usdb/economics' },
          { text: 'Testnet faucet', nextRoute: { pathname: '/usdb/faucet' as const }, icon: 'globe-b' as const, isActive: pathname === '/usdb/faucet' },
        ],
      },""")
    for name, marker, value in (
        ("lib/metadata/templates/title.ts", "const TEMPLATE_MAP: Record<Route['pathname'], string> = {", "'%network_name% network overview'"),
        ("lib/metadata/templates/description.ts", "const TEMPLATE_MAP: Record<Route['pathname'], string> = {", "'USDB chain status and Bitcoin Miner Pass queries.'"),
        ("lib/mixpanel/getPageType.ts", "export const PAGE_TYPE_DICT: Record<Route['pathname'], string> = {", "'USDB pages'"),
        ("lib/metadata/getPageOgType.ts", "const OG_TYPE_DICT: Record<Route['pathname'], OGPageType> = {", "'Regular page'"),
    ):
        passes_value = "'%network_name% Miner Passes'" if "title.ts" in name else value
        economics_value = "'%network_name% block economics'" if "title.ts" in name else value
        faucet_value = "'%network_name% testnet faucet'" if "title.ts" in name else value
        replace_once(destination, name, marker, marker + "\n  '/usdb': " + value + ",\n  '/usdb/passes': " + passes_value + ",\n  '/usdb/economics': " + economics_value + ",\n  '/usdb/faucet': " + faucet_value + ",")
    # Next checks types before the routes plugin regenerates its declarations.
    replace_once(destination, "nextjs/nextjs-routes.d.ts", "  export type Route =",
                 '  export type Route =\n    | StaticRoute<"/usdb">\n    | StaticRoute<"/usdb/passes">\n    | StaticRoute<"/usdb/economics">\n    | StaticRoute<"/usdb/faucet">')
    # Public ingress does not expose Phoenix channels. Keep the socket context
    # for upstream components, but do not connect or retry an unavailable route.
    replace_once(destination, "pages/_app.tsx", "import getSocketUrl from 'lib/api/getSocketUrl';\n", "")
    replace_once(destination, "pages/_app.tsx",
                 "  const socketUrl = !config.features.opSuperchain.isEnabled ? getSocketUrl() : undefined;\n\n", "")
    replace_once(destination, "pages/_app.tsx", "<SocketProvider url={ socketUrl }>", "<SocketProvider>")
    # Both the upstream wallet button and the USDB overview use the same URL
    # checks and safe error categories; the wallet provider flow stays upstream.
    replace_once(destination, "lib/web3/useAddChain.tsx", "import useProvider from './useProvider';",
                 "import { requestNetworkSetup } from './networkAddFeedback';\nimport useProvider from './useProvider';")
    replace_once(destination, "lib/web3/useAddChain.tsx", """    await provider.request({
      method: 'wallet_addEthereumChain',
      params: [ getParams(chainConfig) ],
    });""", """    const params = getParams(chainConfig);
    await requestNetworkSetup(provider, params, () => provider.request({
      method: 'wallet_addEthereumChain',
      params: [ params ],
    }));""")
    replace_once(destination, "lib/web3/useAddChainClick.ts", "import useAddChain from './useAddChain';",
                 "import { networkRequestError } from './networkAddFeedback';\nimport useAddChain from './useAddChain';")
    replace_once(destination, "lib/web3/useAddChainClick.ts", """        title: 'Error',
        description: (error as Error)?.message || 'Something went wrong',""", """        title: 'Network request not completed',
        description: networkRequestError(error),""")
    replace_once(destination, "ui/pages/Block.tsx", "          <BlockDetails query={ blockQuery }/>",
                 """          <BlockDetails query={ blockQuery }/>
          { blockQuery.data?.hash && !blockQuery.isPlaceholderData && <p style={{ marginTop: 20 }}>
            <a href={ '/usdb/economics?block=' + blockQuery.data.hash } style={{ color: '#217bb5', textDecoration: 'underline' }}>Verify USDB block economics →</a>
          </p> }""")
    # Keep the upstream lockfile, but allow patched Node 22 images instead of its exact old patch release.
    replace_once(destination, "package.json", '"node": "22.11.0"', '"node": "22.x"')
    replace_once(destination, "package.json", '"npm": "10.9.0"', '"npm": ">=10"')
    replace_once(destination, "ui/snippets/footer/Footer.tsx", "https://github.com/blockscout/frontend/tree/",
                 "https://github.com/buckyos/usdb-explorer/tree/")
    replace_once(destination, "ui/snippets/footer/Footer.tsx", "https://github.com/blockscout/frontend/commit/",
                 "https://github.com/buckyos/usdb-explorer/commit/")
    shutil.copytree(ROOT / "overlay", destination, dirs_exist_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="explorer-upstream-") as temporary:
        archive = args.archive or Path(temporary) / "source.tar.gz"
        if args.archive is None:
            lock = json.loads((ROOT / "upstream.lock.json").read_text())
            url = f"https://codeload.github.com/blockscout/frontend/tar.gz/{lock['revision']}"
            with urllib.request.urlopen(url, timeout=120) as response, archive.open("wb") as output:
                shutil.copyfileobj(response, output)
        prepare(archive, args.output)


if __name__ == "__main__":
    main()

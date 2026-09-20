# USDB frontend integration

This directory owns the small USDB overlay for the pinned Blockscout frontend.
It adds the `USDB` navigation group, `/usdb` network overview, `/usdb/passes`, and
`/usdb/economics`, and the optional `/usdb/faucet` claim page. The ordinary block detail page links to hash-bound economics.
Economic amounts come from the bounded gateway API; the frontend does not reproduce consensus formulas.

`upstream.lock.json` pins the source commit and archive SHA-256. `prepare.py`
verifies the archive before extraction, rejects unsafe paths, applies checked
navigation/metadata patches, and copies `overlay/`. An upstream update must
review these patches and pass the production build and browser fixtures.

`Dockerfile` preserves upstream's dependency lockfiles, Next standalone build,
and runtime environment handling. Its source is Blockscout frontend v2.3.5
(commit `95feb0ba3245c67b1ced38e71a38569951add266`), licensed under GPL-3.0;
the upstream license is retained in the runtime image as `LICENSE.blockscout`.
The frontend overlay is provided under the same GPL-3.0 terms. Source for the
modified frontend is reconstructed with the pinned upstream archive and this
public repository's `frontend/` directory at the image's OCI source revision.
No changes to the upstream backend are required.

```bash
node_image=$(python3 -c 'import json; print(json.load(open("explorer/assets/images.lock.json"))["images"]["node"]["reference"])')
docker build --build-arg NODE_IMAGE="$node_image" -f frontend/Dockerfile -t usdb-explorer-frontend:development .
python3 -m venv /tmp/explorer-browser
/tmp/explorer-browser/bin/pip install playwright==1.59.0
/tmp/explorer-browser/bin/playwright install chromium
/tmp/explorer-browser/bin/python tests/test_frontend_browser.py --image usdb-explorer-frontend:development
```

The build needs network access to download the checksummed upstream archive,
locked packages and Alpine build dependencies. Source deployments build this
frontend during `up`; release installations pull the prebuilt immutable image.
The archive/lockfile/base image pins identify inputs; they do not promise
bit-for-bit reproducibility of third-party package lifecycle scripts.

The read-only USDB pages call same-origin GET endpoints under `/api/usdb/v1/`;
the faucet uses `/api/faucet/v1/` for status and fixed-policy claim submissions.
Neither contacts the private indexer or receives signing keys. Browser fixtures use an isolated container and
intercept public API responses, so they do not require a node or mining keys.

The release pipeline publishes `usdb-explorer-frontend` with the Explorer tag,
pins its digest in the kit, records it in the release change assets, and scans
it with the same source-identity and testnet security policy as the gateway.

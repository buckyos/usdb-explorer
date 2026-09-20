# USDB public read API v1

The gateway owns `/api/usdb/v1/` independently of Blockscout's `/api/v2/`.
All routes accept GET only. Responses use `schema_version: usdb-explorer-public:v1`,
`Cache-Control: no-store`, and a UTC `updated_at` on success. Browser pages use
same-origin requests. No private indexer method is added to the public JSON-RPC
allowlist.

The optional faucet uses a separate signer process and `/api/faucet/v1/` routes;
its claim POST does not change this read API or the gateway RPC allowlist.
See [Faucet API](faucet-api.md) and [operator handbook](handbook/faucet.md).

| Route | Query | Result |
| --- | --- | --- |
| `/overview` | none | Public wallet/network identity, USDB head, Blockscout indexed height and indexer readiness |
| `/passes` | optional `height`, `state`, `cursor` | Active Standard candidates, total, exact external state, next cursor |
| `/passes/{inscription_id}` | optional `height`, `state` | Pass profile and inscription relations from one external state |

Inscription IDs use 64 lowercase hex characters, `i`, and a canonical uint32
index. Height is canonical decimal uint32. State is a lowercase 32-byte hex
system state ID and requires height. Cursor is opaque, at most 4096 bytes, and
requires both height and state. Unknown, duplicate and malformed parameters are
rejected. The fixed page size is 25; order is
`uip-0006:effective-energy-desc-pass-id-asc:v1`. Candidates exclude Collab and
inactive passes; those can still be queried by ID.

Energy and total fields are decimal strings. Chain/indexed heights and chain
timestamps are also decimal strings; BTC heights are uint32 numbers. Clients
must not convert energy to JavaScript Number. Optional address mapping can be
absent; it must not be interpreted as a missing owner.

The private adapter requires `get_rpc_info`, `get_readiness`, historical-state,
pass-snapshot, pass-economic-profile and candidate-set capabilities, API 1.0.0,
and `uip-0006-usdb-economic-state-view:v1`. It compares the BTC network and
activation registry with Explorer's checked-in catalog and verifies chain
ID/genesis before serving data. Profile and inscription reads use the same
height and expected historical identity; cursor queries carry that identity.
These are upstream assertions bound to a specific snapshot, not independently
verified consensus proofs or reward settlement evidence.

The overview distinguishes unavailable optional services from the USDB head.
Pass queries require a query-ready and consensus-ready indexer. A chain identity
failure closes both routes. Requests share the gateway's peer rate limiter and
eight-request concurrency bound; the full operation is limited to 12 seconds.
The optional indexer part of overview has its own four-second deadline so an
unreachable indexer does not consume the entire overview budget. Upstream bodies
remain bounded by the existing gateway response limit.

Errors have `{ "schema_version": "usdb-explorer-public:v1", "error": { "code": "..." } }`.
They never include upstream endpoint URLs, RPC messages or `error.data`.

| HTTP | Codes |
| --- | --- |
| 400 | `INVALID_QUERY` |
| 404 | `PASS_NOT_FOUND`, `NOT_FOUND` |
| 405 | `READ_ONLY` |
| 409 | `STATE_CHANGED` |
| 429 | `RATE_LIMITED` |
| 502 | `INVALID_UPSTREAM_RESPONSE` |
| 503 | `INDEXER_NOT_CONFIGURED`, `INDEXER_NOT_READY`, `INDEXER_INCOMPATIBLE`, `INDEXER_NETWORK_MISMATCH`, `HISTORY_UNAVAILABLE`, `UPSTREAM_UNAVAILABLE`, `CHAIN_IDENTITY_UNAVAILABLE`, `BUSY` |

Public routing is the existing bundled/external Nginx `/api/` route. The local
`/indexer` route exists only on the private socket relay; no host/public indexer
listener is added. Operations and recovery guidance: [USDB pages handbook](handbook/usdb-pages.md).

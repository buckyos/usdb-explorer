# Testnet faucet API v1

The optional faucet owns `/api/faucet/v1/`, routed directly by the configured
ingress to a separate signer process. It does not add signing methods to the
public RPC gateway. Amounts and balances use exact decimal strings; never parse
atom values through a JavaScript Number.

| Method and route | Request | Response |
| --- | --- | --- |
| `GET /status` | None | `enabled`, `status`, wallet `address`, `chain_id`, policy and last worker observation |
| `POST /claims` | JSON with `request_id` and `address` | HTTP 202 and the durable application receipt |
| `GET /claims/{id}` | The complete returned `c_...` ID | Latest recorded application receipt, or HTTP 404 |

Disabled deployments return `{"enabled":false,"status":"disabled"}` through
their generated ingress. When enabled, status includes `claim_amount` and
`daily_budget` in USDB, `balance_atoms` and `reserved_atoms` as integer strings,
`cooldown_seconds`, `confirmations` and Unix-seconds `updated_at`. A `ready`
snapshot does not reserve funds or guarantee acceptance of a subsequent claim.

## Claim contract

```json
{
  "request_id": "3186e969b5874733bfb7d2b690f3b7d4",
  "address": "0x1111111111111111111111111111111111111111"
}
```

Generate a random request ID once and persist it before sending. IDs contain
16–80 ASCII letters, digits, underscores or hyphens. Retry the same ID and
address after a timeout. Reusing an existing ID for another address is rejected.
The service returns the same receipt for repeated accepted requests. Quotas and
cooldowns apply independently of the ID. A 202 response means the request was
recorded; it is not an on-chain success receipt.

Responses contain `id` (the request ID prefixed with `c_`), `address`,
`amount_atoms`, Unix-seconds `created_at`, `status`, and `transaction_hash` once
signed bytes have been durably prepared. States are `queued`, `pending`,
`confirming`, `confirmed`, `failed`, or `rejected`. The hash may exist before the
upstream acknowledges broadcast. `confirmed` requires a canonical receipt at
the configured depth; later deep reorgs remain an operational recovery concern.

Only fixed-policy native USDB transfers to ordinary accounts are supported.
Clients cannot choose the amount, sender, transaction data, gas price or nonce.
Bodies are limited to 1024 bytes. Public responses never include private keys,
signed bytes, private upstream URLs or raw upstream errors.

## Limits and errors

| HTTP | Examples |
| --- | --- |
| 400 | `INVALID_ADDRESS`, `INVALID_REQUEST_ID` |
| 403 | `ORIGIN_MISMATCH` |
| 409 | `REQUEST_ID_CONFLICT`, `ADDRESS_PENDING` |
| 415 | `JSON_REQUIRED` |
| 429 | `ADDRESS_COOLDOWN`, `IP_DAILY_LIMIT`, `IP_RATE_LIMIT`, `DAILY_BUDGET_EXHAUSTED` |
| 503 | `INSUFFICIENT_FUNDS`, `WRONG_NETWORK`, `RPC_UNAVAILABLE`, `GAS_PRICE_TOO_HIGH`, `QUEUE_FULL`, `SERVICE_BUSY` |

Errors use `{"error":{"code":"..."}}`; unrecognized routes and missing claim
IDs may use a plain HTTP 404 response. `Retry-After: 60` is a minimum backoff,
not a promise that an address cooldown or daily budget has reset.

The ingress overwrites the IP header and authenticates it to the private service
with a generated token. Browsers do not supply that token. Per-IP request limits
also apply to status reads (120/minute), IPv6 uses a /64 grouping, and handlers
have a 32-request concurrency bound. There is no cross-origin CORS permission.
Do not publish the private signer port or copy the ingress token into frontend
configuration. Funding is available only through local operator commands, never
through this API. Recovery and policy details: [faucet handbook](handbook/faucet.md).

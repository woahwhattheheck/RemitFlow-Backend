# Abuse controls and correlation IDs

RemitFlow Backend bounds high-volume retries and automated abuse on mutation
and provider-adjacent routes, and propagates a safe correlation id on every
request so incidents can be traced without leaking account secrets.

## Global limit

Every `/api/*` request is subject to an IP-keyed fixed-window limit:

| Setting | Env | Default |
|---|---|---|
| Window | `RATE_LIMIT_WINDOW_MS` | `60000` |
| Max requests | `RATE_LIMIT_MAX` | `100` |
| Max tracked keys | `RATE_LIMIT_MAX_KEYS` | `10000` |

Responses carry `X-RateLimit-Limit`, `X-RateLimit-Remaining`,
`X-RateLimit-Reset`, and `X-RateLimit-Policy`. Exhausted budgets answer
`429` with `Retry-After` and a JSON error that includes `requestId`.

## Mutation / route-family limits

Authenticated write paths use a **stricter actor-keyed** budget on top of the
global limit. The actor key is a truncated SHA-256 fingerprint of the API
token (or admin key) — never the raw secret. Public quote uses the client IP.

| Family | Routes | Env (window / max) | Default max / min |
|---|---|---|---|
| `transfers` | `POST /api/transfers`, claim, cancel, archive, unarchive | `MUTATION_RATE_LIMIT_TRANSFERS_*` | 30 / 60s |
| `users` | `POST /api/users` | `MUTATION_RATE_LIMIT_USERS_*` | 20 / 60s |
| `quote` | `GET /api/quote` | `MUTATION_RATE_LIMIT_QUOTE_*` | 60 / 60s |
| `admin` | `GET /api/admin/diagnostics` | `MUTATION_RATE_LIMIT_ADMIN_*` | 30 / 60s |

Shared cap: `MUTATION_RATE_LIMIT_MAX_KEYS` (default `10000`).

Actors are isolated: one token burning its transfer budget does not exhaust
another token's budget. Limiter tables are bounded — under an identity flood,
expired windows are pruned first, then the oldest key is dropped.

## Proxy trust

`TRUST_PROXY=true` opts into honouring `X-Forwarded-For` (left-most hop) and
Express `trust proxy`. **Leave it off** unless a reverse proxy strips
untrusted hops. With the default, forged `X-Forwarded-For` values cannot
rotate the client identity used for rate limiting.

## Correlation IDs

Every request gets a correlation id:

- Honour inbound `X-Request-Id` or `X-Correlation-Id` when the value is at
  most 128 characters and matches `[A-Za-z0-9._:-]+`.
- Otherwise generate a fresh UUID.
- Echo the same value on both `X-Request-Id` and `X-Correlation-Id`.
- Expose it as `req.id` / `req.correlationId` and on every error envelope
  as `error.requestId`.

Unsafe inbound values (spaces, quotes, oversized strings) are discarded so
callers cannot smuggle tokens or free-form PII into logs via the header.

## Test behaviour

When `NODE_ENV=test`, rate limiters are no-ops unless
`ENABLE_RATE_LIMIT_IN_TEST=1` (or a limiter is constructed with
`forceInTest: true`). This keeps the functional suite independent of the
abuse budget while still allowing focused regression coverage.

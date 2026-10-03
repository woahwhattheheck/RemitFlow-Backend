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
another token's budget. Limiter tables are bounded. Expired windows are pruned
first; when every key is still live, a new identity receives 429 with
`Retry-After` until a slot expires. This preserves active budgets under an
identity flood, at the cost of delaying new identities when the table is full.

## Proxy trust

`TRUST_PROXY=true` uses Express `trust proxy` for one proxy hop. Rate
limiting reads Express's resolved `req.ip`, so an attacker-controlled left-most
`X-Forwarded-For` value cannot rotate the identity when the trusted proxy
appends the connecting address. Enable this only when the deployment has
exactly one trusted reverse proxy; otherwise leave it off until the app's
trust proxy setting matches the deployment topology.

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

## Reproduce saturated-table HTTP load

With this checkout's normal dependencies installed, run:

```sh
node scripts/benchmark-rate-limit.cjs rate-limit-result.json > /dev/null
```

Use a new result filename. The script starts the real application on an ephemeral
loopback port, enables the limiter explicitly in test mode, and uses one trusted
proxy hop to generate distinct local identities. It admits 10,000 identities,
then measures three batches of 2,000 rejected newcomers at 32 concurrent
connections. Every measured response must be 429 with the global policy and
matching correlation ID; an exhausted original identity must remain blocked.
The script closes its server and connections when finished.

JSON output retains each elapsed/CPU sample, Node and Express versions, and
SHA-256 hashes of the application and limiter source. CPU includes both the
HTTP client and server in the same process. Standard application logs go to
stdout, so keep the same redirection for both versions being compared.

To compare another checkout with its dependencies already installed, pass its
path as the second argument. Alternate original/repaired/original/repaired runs
using the same script and distinct output filenames; report medians and ranges
because load on the host can vary. The workload uses generated local traffic and
does not measure deployed throughput or external provider behavior. Repeated
rejection avoids table scans until the next expiry; expiry-triggered cleanup
still takes time proportional to the number of tracked identities.

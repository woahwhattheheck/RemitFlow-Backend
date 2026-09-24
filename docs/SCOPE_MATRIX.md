# Scope matrix

Canonical scope strings live in `src/config/scopes.js` (`SCOPES`, `SCOPE_MATRIX`).
Route middleware (`requireScope` / `adminAuth`) is the first gate; service helpers
in `src/utils/authz.js` re-check the same scopes so a forgotten middleware cannot
expose a privileged mutation.

| Method | Path | Required scopes | Surface |
|--------|------|-----------------|---------|
| GET | `/api/transfers` | `transfers:read` | list |
| GET | `/api/transfers/stats` | `transfers:read` | direct |
| GET | `/api/transfers/:id` | `transfers:read` | direct |
| POST | `/api/transfers` | `transfers:write` | direct |
| POST | `/api/transfers/bulk` | `transfers:write` | bulk |
| POST | `/api/transfers/:id/claim` | `transfers:write` | direct |
| POST | `/api/transfers/:id/cancel` | `transfers:write` | direct |
| POST | `/api/transfers/:id/archive` | `transfers:write` | direct |
| POST | `/api/transfers/:id/unarchive` | `transfers:write` | direct |
| GET | `/api/users` | `users:read` | list |
| GET | `/api/users/:id` | `users:read` | direct |
| POST | `/api/users` | `users:write` | direct |
| GET | `/api/audit` | `audit:read` | list |
| GET | `/api/admin/diagnostics` | `admin:read` | admin |

## Admin credentials

`GET /api/admin/diagnostics` accepts either:

1. `Authorization: Bearer <api-token>` whose catalog entry includes `admin:read`, or
2. Legacy `X-Admin-Token: <ADMIN_API_KEY>` / `Authorization: Bearer <ADMIN_API_KEY>`,
   which is mapped onto `[admin:read]` so the path stays scope-gated.

## Non-enumeration

Transfer lookups (direct and bulk) return the same `404 Transfer not found`
envelope for malformed ids and for unknown well-formed ids. Bulk per-id failures
use a stable `error.code` of `not_found` in both cases.

## Bulk mutations

```http
POST /api/transfers/bulk
Authorization: Bearer <token with transfers:write>
Content-Type: application/json

{ "action": "claim" | "cancel" | "archive" | "unarchive", "ids": ["txn_…", …] }
```

Up to 50 ids per call. The response is `{ results: [{ id, ok, transfer? | error? }] }`.

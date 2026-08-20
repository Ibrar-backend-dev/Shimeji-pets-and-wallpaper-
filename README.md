# Shimeji Pets & Wallpaper — Backend

One Django backend serving three Android apps: **Shimeji animations**, **Wallpapers**, and
**Battery emoji & animations**.

Its job is narrow on purpose: handle admin-side uploads, store bytes on Backblaze B2, manage
the database, and expose a read-only API that hands the apps **URLs** — never bytes.

```
Admin browser ──presign──► Django ──────────────► Postgres (Neon)
      │                                                │
      └──────── PUT bytes directly ──────► B2 ◄─── URLs only
                                            │
Android apps ──── GET /api/v1/… ──► Django ─┘
                                    (returns CDN URLs)
```

Bulk media never passes through the app server. That is what lets a 50-file upload work inside
a free tier's ~512 MB of RAM and 30–60 s request ceiling.

---

## Content model

Three levels. `Trending` and `Latest` are ordinary admin-created categories, **not** computed
feeds — nothing here ranks or scores anything.

```
type (wallpaper | shimeji | battery)      ← 3 rows, seeded by migration
└── category (Trending, Anime, Car, …)    ← admin-created
    └── subcategory (optional)            ← admin-created
        └── item                          ← created only by the ingest pipeline
```

An item belongs to exactly one category and at most one subcategory. There are **no like or
download counters** anywhere: the backend serves URLs, engagement is the app's concern.

---

## Quick start

Needs Python 3.11+ (3.12 is the deploy target). Postgres and B2 are both optional locally.

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements/dev.txt

cp .env.example .env              # works as-is: falls back to SQLite

python manage.py migrate          # also seeds the three types
python manage.py createsuperuser
python manage.py runserver
```

Then mint a key and call the API:

```bash
python manage.py create_app_client "Wallpaper Android" --features wallpaper
# prints the key once — it is stored hashed and cannot be recovered

curl -H "X-API-Key: <key>" http://localhost:8000/api/v1/wallpapers?limit=20
```

### With Postgres and a local S3

`docker compose up` brings up Postgres plus MinIO, which speaks the same S3 API as B2, so the
application code is identical and only the endpoint differs.

---

## The response envelope

Every response — success and error — has the same three keys:

```json
{
  "status": 200,
  "data": { "items": [ … ], "total": 69, "skip": 0, "limit": 20 },
  "message": "Wallpapers fetched successfully"
}
```

```json
{
  "status": 400,
  "data": null,
  "message": "'limit' must not exceed 100.",
  "errors": { "limit": ["Must be <= 100."] },
  "request_id": "8f2c…"
}
```

Wrapping happens in one renderer and one exception handler, so no view can forget it or spell
it differently. `request_id` is echoed in the `X-Request-ID` header too, so a bug report from
an app maps directly onto a log line.

Full endpoint and parameter reference: **[API.md](API.md)**.

---

## Layout

| Path | What lives there |
| --- | --- |
| `config/settings/` | `base` / `dev` / `prod` / `test`. `prod` refuses to start on missing secrets |
| `apps/core/` | Envelope, exception handler, pagination, UUIDv7 ids, JSON logging, audit log, cron jobs |
| `apps/catalog/` | `Feature` / `Category` / `Subcategory` / `Tag` / `MediaItem` and the public read API |
| `apps/clients/` | `AppClient` API keys, key resolution, per-client throttling |
| `apps/ingest/` | `UploadTicket`, B2 storage layer, upload validators, presign/commit/abort |
| `tests/` | 198 tests; the ingest and contract files are the ones that matter most |

---

## Design notes

Decisions that are load-bearing and would be expensive to revisit.

**UUIDv7 primary keys, generated monotonically.** The contract exposes UUID strings, but random
`uuid4` keys scatter every insert across the index, so indexes bloat and bulk uploads get slower
over time. UUIDv7 puts a millisecond timestamp in the leading bits, keeping inserts at the right
edge of the B-tree. The generator is also monotonic within a millisecond, which matters more than
it sounds: ordering is `priority, -created_at, -id`, and twelve rapid inserts were measured
producing only **seven** distinct `created_at` values — so `id` is the real tiebreak far more
often than expected. See [`apps/core/ids.py`](apps/core/ids.py).

**`skip`/`limit`/`total` offset paging, with both of its costs bounded.** Offset paging is what
the apps expect, so it is what ships. But `total` means a `COUNT(*)` per request, and a deep
`skip` degrades into a scan. So counts are cached per filter signature, `skip` is capped at
`API_MAX_SKIP` with an explicit 400, and `?cursor=` offers keyset paging past the cap. Invalid
values are **rejected, never silently clamped** — a client asking for `limit=500` and receiving
100 rows would assume it had everything and stop paging.

**Partial indexes matching the exact ordering clause.** Every public read filters to
`status=READY AND is_active`, so the indexes are conditioned on precisely that, keeping them a
fraction of full-table size.

**API keys resolved in middleware, not a DRF authenticator.** A key identifies an *app*, not a
user, which makes it request context rather than authentication. DRF authenticates lazily on
first touch of `request.user`, so a permission class reading `request.app_client` could run
before authentication had happened at all — and attributes set on DRF's request wrapper never
reach plain Django middleware, which would cost the access log its client name.

**Nothing the client declares is treated as a fact.** At commit the server re-derives everything
from the bytes actually in B2: magic-byte sniffing (an `.exe` sent as `image/png` dies here),
Pillow `verify()` plus a pixel budget for decompression bombs, and zip inspection from the
central directory alone — so a zip bomb is rejected on its own metadata rather than by attempting
the extraction and running out of memory. Object keys are always server-minted, so a filename
can never reach a storage path.

**Commit is per-item, not all-or-nothing.** One corrupt file in a batch of fifty must not discard
the other forty-nine successful uploads, so commit returns `207` with per-item results.

**Deletes are soft.** Archiving flips a status; the reaper purges bytes after
`ARCHIVE_RETENTION_DAYS`. Deleting inline would mean a storage hiccup either rolls back a delete
the admin believes succeeded or orphans the row — and the delay leaves a window to undo a mistake.

---

## Operations

```bash
python manage.py check_deploy        # config, DB, storage, seed data — exits non-zero on problems
python manage.py reap_orphans        # abandoned uploads + archived items past retention
python manage.py reconcile_counts    # rebuild denormalized counters from source
python manage.py create_app_client "Name" --features wallpaper --rate-limit 120
```

Both maintenance jobs are also HTTP endpoints under `/internal/cron/`, guarded by
`X-Cron-Secret`, because Render's free tier has neither cron nor a background worker. Same code
path either way, and both are idempotent and batch-bounded.

Health: `/healthz` is liveness and does no I/O (so it never fails on a sleeping database);
`/readyz` checks Postgres and B2 and caches the verdict for 30 s.

---

## Tests

```bash
pytest -q                                    # 198 tests
pytest -q --cov=apps --cov-report=term-missing
DATABASE_URL=postgres://… pytest -q          # exercises the Postgres-only composite FK
```

`moto` mocks S3 in-process, so the suite never touches a real bucket and needs no credentials.
CI runs the suite against both SQLite and Postgres, checks for missing migrations, and builds
the production image.

---

## Deploying

`render.yaml` and `railway.json` are both committed; pick either. Database is Neon, storage is
B2 behind Cloudflare. Two things are easy to get wrong and are documented in
**[DEPLOY.md](DEPLOY.md)**:

- Use Neon's **pooled** (`-pooler`) DSN with `CONN_MAX_AGE=0`. It is pgbouncer in transaction
  mode, which is incompatible with persistent connections and server-side cursors; getting this
  wrong surfaces as intermittent `InterfaceError` under load rather than a clean failure.
- Add a Cloudflare cache rule that **ignores `X-API-Key`** in the cache key, or `Vary` fragments
  the edge cache per app and the CDN stops absorbing traffic.

# API Reference

Base URL: `/api/v1/`

Every public endpoint requires an `X-API-Key` header. Every response — success and error — uses
the same envelope, so a client parses one shape and never branches on status code to find the
payload.

```
X-API-Key: wallpa_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

---

## Envelope

**Success**

```json
{
  "status": 200,
  "data": { "items": [ … ], "total": 69, "skip": 0, "limit": 20 },
  "message": "Wallpapers fetched successfully"
}
```

**Error**

```json
{
  "status": 400,
  "data": null,
  "message": "'limit' must not exceed 100.",
  "errors": { "limit": ["Must be <= 100."] },
  "request_id": "3f9c1b0a4e7d4f2b8c1a"
}
```

`data` is always `null` on an error. `errors` is present when there is field-level detail.
`request_id` is also returned in the `X-Request-ID` response header — quote it in a bug report
and the matching log line can be found directly.

| Status | Meaning |
| --- | --- |
| `200` | OK |
| `201` | Created (whole ingest batch succeeded) |
| `207` | Multi-Status — a mixed batch; read per-item results |
| `304` | Not Modified (you sent `If-None-Match`) |
| `400` | Invalid parameters or payload |
| `403` | Missing, unknown, inactive, or tampered API key |
| `404` | No such item, or not visible to this key |
| `429` | Rate limited; `errors.retry_after_seconds` says how long to wait |
| `503` | `/readyz` only — a dependency is down |

---

## Public endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/manifest` | Whole taxonomy in one call — use at app launch |
| GET | `/categories?type=wallpaper` | Categories for a type |
| GET | `/subcategories?category_id=…` | Subcategories of a category |
| GET | `/wallpapers` · `/wallpapers/{id}` | Wallpaper feed and detail |
| GET | `/shimeji` · `/shimeji/{id}` | Shimeji feed and detail |
| GET | `/battery` · `/battery/{id}` | Battery feed and detail |
| GET | `/items?type=a,b` | Mixed feed across every type the key may read |
| GET | `/items/{id}` · `/items/{id}/related` | Detail, and others in the same subcategory |

Routes are named per type so each app calls its own path and cannot be handed another type's
content by forgetting a parameter. A key scoped to one type receives an **empty page** for
others rather than a 403 — a 403 would confirm that content it may not see exists.

---

## `GET /wallpapers` — the item feed

### Parameters

| Name | Type | Notes |
| --- | --- | --- |
| `skip` | int | Default `0`. Max `10000`; past that use `cursor` |
| `limit` | int | Default `20`, max `100` |
| `cursor` | string | Keyset paging for deep pages. Requires default ordering |
| `category_id` | uuid | Must belong to this route's type |
| `subcategory_id` | uuid | Must belong to `category_id` if both are given |
| `media_type` | csv | `IMAGE`, `GIF`, `VIDEO`, `ZIP` |
| `is_live` | bool | Live wallpaper / animated asset |
| `premium` | bool | |
| `orientation` | csv | `PORTRAIT`, `LANDSCAPE`, `SQUARE` |
| `resolution` | csv | `SD`, `HD`, `FHD`, `QHD`, `UHD_4K` |
| `tags` | csv | Tag slugs, OR semantics |
| `q` | string | Name search, max 100 chars |
| `ordering` | enum | `priority` (default), `-priority`, `newest`, `oldest`, `name`, `-name` |
| `updated_since` | iso8601 | Incremental sync — only rows changed since |
| `type` | csv | `/items` only: narrow the mixed feed |

Booleans accept `true/false/1/0/yes/no/on/off`. Unrecognised parameters are ignored, so
cache-busters and analytics tags are harmless. Recognised parameters are strict: a bad value is
a `400` explaining what was wrong, never a silently empty page.

### Response

```json
{
  "status": 200,
  "data": {
    "items": [
      {
        "id": "7fbaae21-3707-487f-b34e-45de21cb0d01",
        "name": "neon city",
        "type": "wallpaper",
        "category_id": "61899ee4-a4e7-49b2-a0e5-871a577b743d",
        "category_name": "Trending",
        "subcategory_id": null,
        "subcategory_name": null,
        "premium": false,
        "preview_url": "https://cdn.example.com/wallpaper/trending/previews/2026/08/…jpg",
        "image_url":   "https://cdn.example.com/wallpaper/trending/assets/2026/08/…mp4",
        "media_type": "VIDEO",
        "is_live": true,
        "width": 2160,
        "height": 3840,
        "file_bytes": 8412160,
        "duration_ms": 6000,
        "orientation": "PORTRAIT",
        "resolution": "UHD_4K",
        "dominant_color": "#1a2b3c",
        "tags": ["anime", "neon"],
        "priority": 1,
        "created_at": "2026-08-20T04:51:49.002715"
      }
    ],
    "total": 69,
    "skip": 0,
    "limit": 20
  },
  "message": "Wallpapers fetched successfully"
}
```

### Fields

| Field | Notes |
| --- | --- |
| `image_url` | **The asset, whatever its type** — `.png`, `.mp4` or a Shimeji `.zip`. Pair with `media_type` to decide how to render |
| `preview_url` | Always an image. Server-generated for images and GIFs; uploader-supplied for video and zip |
| `media_type` | `IMAGE` / `GIF` / `VIDEO` / `ZIP` |
| `is_live` | Render as animated. Defaults from `media_type` at upload, admin-overridable |
| `subcategory_id` / `_name` | Both `null` when the item sits directly in its category |
| `orientation`, `resolution` | Derived from the dimensions at save time and indexed, so they are filterable. `resolution` buckets by the longer edge, so a 2160×3840 portrait is `UHD_4K` |
| `dominant_color` | Average colour, for a placeholder behind a loading thumbnail. May be `""` |
| `color_code` | Optional admin-defined `#RRGGBB` colour for Shimeji and Battery items. `null` when not supplied |
| `duration_ms` | Uploader-declared; there is no ffmpeg on the host to probe it. May be `null` |
| `priority` | Lower sorts first |
| `created_at` | Naive UTC with microseconds |

Ordering is **`priority` ascending, then `created_at` descending**: every `priority: 1` row
first, newest first within each band.

URLs are composed from `MEDIA_CDN_BASE_URL` at serialization time and never stored, so changing
CDN host is one environment variable.

---

## Paging

### Offset (default)

```
GET /api/v1/wallpapers?skip=0&limit=20
GET /api/v1/wallpapers?skip=20&limit=20
```

`total` always describes the whole filtered set, so it does not shift as you page.

### Cursor (deep pages)

`skip` is capped at 10,000. Past that, page by cursor:

```
GET /api/v1/wallpapers?limit=20&cursor=eyJwcmlvcml0eSI6MSwi…
```

```json
{ "items": [ … ], "total": 69, "limit": 20,
  "cursor": "eyJwcmlv…", "next_cursor": "eyJwcmlv…" }
```

Pass `next_cursor` back as `cursor`. A `null` `next_cursor` means the end. Cursor mode requires
the default ordering — combining it with `?ordering=` is rejected rather than silently returning
the wrong page.

---

## `GET /categories`

```json
{
  "status": 200,
  "data": {
    "items": [
      {
        "id": "61899ee4-a4e7-49b2-a0e5-871a577b743d",
        "name": "Trending",
        "type": "wallpaper",
        "thumbnail": "https://cdn.example.com/wallpaper/category-thumbnails/….png",
        "priority": 1,
        "has_subcategories": false,
        "item_count": 24
      }
    ],
    "total": 6, "skip": 0, "limit": 20
  },
  "message": "Categories fetched successfully"
}
```

`has_subcategories` is maintained on write, so the app can decide whether to render a
subcategory row without a second request. `item_count` counts published items only.

---

## `GET /manifest`

One request at launch instead of one per type plus one per category. The most cacheable response
in the system; counts come from denormalized columns, never a live `COUNT`.

```json
{
  "status": 200,
  "data": {
    "config_version": "a4f2c81b9e3d5f60",
    "types": [
      {
        "id": "…", "type": "wallpaper", "name": "Wallpapers", "priority": 1,
        "categories": [
          {
            "id": "…", "name": "Trending", "priority": 1,
            "has_subcategories": false, "item_count": 24,
            "subcategories": []
          },
          {
            "id": "…", "name": "Anime", "priority": 2,
            "has_subcategories": true, "item_count": 40,
            "subcategories": [
              { "id": "…", "name": "4K Anime", "priority": 1, "item_count": 12 }
            ]
          }
        ]
      }
    ]
  },
  "message": "Manifest fetched successfully"
}
```

Cache `config_version` locally and refetch the taxonomy only when it changes.

---

## Caching

List responses carry `Cache-Control`, `ETag` and `Last-Modified`. Send the tag back to save
bandwidth:

```bash
curl -H "X-API-Key: $KEY" -H 'If-None-Match: "a1b2c3"' \
  https://api.example.com/api/v1/wallpapers      # -> 304, empty body
```

The ETag is derived from the newest `updated_at` in the matching set, so any edit to any matching
row invalidates it.

---

## Admin ingest

Staff only — session auth for a browser tool, JWT for a standalone uploader. An API key grants
nothing here. Bytes go **straight from the browser to B2**; the app server only ever sees
metadata.

```
1. POST /api/v1/admin/uploads/presign     -> presigned PUT URLs + ticket ids
2. PUT  <upload_url>                      -> browser uploads directly to B2
3. POST /api/v1/admin/uploads/commit      -> server validates and creates rows
```

### `POST /admin/uploads/presign`

```json
{
  "type": "wallpaper",
  "category_id": "61899ee4-…",
  "subcategory_id": null,
  "files": [
    { "filename": "neon.png", "content_type": "image/png", "size": 482113 },
    { "filename": "poster.jpg", "content_type": "image/jpeg", "size": 40211, "kind": "PREVIEW" }
  ]
}
```

Up to 50 files per request; one file and fifty take the identical path.

```json
{
  "status": 201,
  "data": {
    "slots": [
      {
        "ticket_id": "01a01e04-…",
        "object_key": "wallpaper/trending/assets/2026/08/9f8e…png",
        "upload_url": "https://s3.us-west-004.backblazeb2.com/…?X-Amz-Signature=…",
        "required_headers": { "Content-Type": "image/png", "Content-Length": "482113" },
        "expires_at": "2026-08-20T05:06:49+00:00",
        "kind": "ASSET",
        "filename": "neon.png"
      }
    ],
    "rejected": [],
    "accepted_count": 1,
    "rejected_count": 0
  },
  "message": "1 upload slot(s) created"
}
```

`required_headers` must be sent **exactly** on the PUT: both are signed into the URL, so a
presign issued for a 482 KB PNG cannot be redeemed for anything else.

Unacceptable files come back in `rejected` rather than failing the batch, so an uploader learns
about all fifty problems in one round trip. `207` means partial, `400` means nothing was accepted.

### `POST /admin/uploads/commit`

```json
{
  "items": [
    {
      "asset_ticket_id": "01a01e04-…",
      "preview_ticket_id": null,
      "name": "Neon City",
      "premium": false,
      "priority": 1,
      "is_live": null,
      "duration_ms": null,
      "tags": ["anime", "neon"]
    }
  ]
}
```

`is_live: null` derives it from the media type. `preview_ticket_id` is **required** for `VIDEO`
(no ffmpeg to derive a poster) and for images above `INLINE_PROCESS_MAX_BYTES`.

```json
{
  "status": 207,
  "data": {
    "results": [
      { "index": 0, "ok": true, "id": "01a01e05-…", "media_type": "IMAGE",
        "width": 1080, "height": 1920,
        "image_url": "https://cdn…png", "preview_url": "https://cdn…jpg" },
      { "index": 1, "ok": false, "error": "File contents are 'application/zip' but 'image/png' was declared.",
        "code": "content_mismatch" }
    ],
    "committed_count": 1,
    "failed_count": 1
  },
  "message": "1 item(s) committed, 1 failed"
}
```

Each item succeeds or fails on its own — one bad file never discards the good ones.

### Rejection codes

| Code | Cause |
| --- | --- |
| `mime_not_allowed` | Content type not in this type's whitelist |
| `file_too_large` | Over the type's `max_file_bytes` |
| `extension_mismatch` | Filename extension disagrees with the declared content type |
| `object_missing` | Ticket committed without completing the PUT |
| `size_mismatch` | Stored object is not the size the ticket declared |
| `unrecognised_content` | Bytes match no known signature (e.g. an `.exe` sent as PNG) |
| `content_mismatch` | Real type differs from the declared one |
| `too_many_pixels`, `decompression_bomb` | Over the type's pixel budget |
| `zip_traversal`, `zip_absolute_path` | Archive entry escapes its directory |
| `zip_bomb`, `zip_too_large`, `zip_too_many_entries` | Archive expansion limits |
| `zip_missing_conf` | Strict Shimeji structure enabled and no `conf/` present |
| `preview_required` | Video, or an oversized image, with no preview supplied |
| `duplicate_file` | These exact bytes are already in the catalog |
| `ticket_unusable` | Unknown, expired, already used, or another user's ticket |
| `ticket_wrong_kind` | A `PREVIEW` ticket supplied as an asset, or vice versa |

### Other admin endpoints

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/admin/uploads/abort` | `{"ticket_ids": [...]}` — cancels tickets and deletes any uploaded bytes |
| PATCH | `/admin/items/{id}` | Editorial fields only: `name`, `premium`, `priority`, `is_live`, `is_active`, `color_code`, `tags`, `subcategory_id` |
| DELETE | `/admin/items/{id}` | Archives (soft delete). Bytes purged after `ARCHIVE_RETENTION_DAYS` |

Storage keys, sizes, checksums, dimensions and status are derived at commit and cannot be
patched — allowing that would let the database disagree with the bytes in the bucket.

---

## Internal

| Method | Path | Auth |
| --- | --- | --- |
| GET | `/healthz` | none — liveness, does no I/O |
| GET | `/readyz` | none — checks Postgres and B2, cached 30 s |
| POST | `/internal/cron/reap-orphans` | `X-Cron-Secret` |
| POST | `/internal/cron/reconcile-counts` | `X-Cron-Secret` |

Both cron endpoints accept `?dry_run=1` and `?limit=N`, are idempotent, and are batch-bounded —
a response with `"more_pending": true` means work remains for the next run.

Health endpoints are deliberately **not** enveloped: a load balancer wants a status code, not a
contract to parse.

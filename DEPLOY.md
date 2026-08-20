# Deploying

Target: a free-tier stack — **Render or Railway** for the app, **Neon** for Postgres,
**Backblaze B2 behind Cloudflare** for media. Both host configs are committed; pick one.

Total cost at low traffic: nothing, provided Cloudflare fronts B2 (their Bandwidth Alliance
makes that egress free; serving straight from B2 is not).

Three things on this stack are easy to get wrong and painful to diagnose. They are marked
**⚠ Gotcha** below.

---

## 1. Database — Neon

1. Create a project at [neon.tech](https://neon.tech) and a database.
2. Copy the connection string from **Connection Details** — the one whose host contains
   **`-pooler`**.

```
postgresql://user:pass@ep-cool-name-123456-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require
                                             ^^^^^^^
```

> **⚠ Gotcha — the pooled DSN needs `CONN_MAX_AGE=0`.**
>
> Neon's pooled endpoint is pgbouncer in **transaction mode**. Persistent connections and
> server-side cursors are both incompatible with it. Leaving Django's defaults produces
> intermittent `InterfaceError` and `server closed the connection unexpectedly` under load —
> never a clean, obvious failure, which is what makes it expensive to track down.
>
> `config/settings/prod.py` already pins `CONN_MAX_AGE=0` and
> `DISABLE_SERVER_SIDE_CURSORS=True`, and `manage.py check_deploy` fails if a pooled host is
> paired with a non-zero `CONN_MAX_AGE`. Just don't override it.

Neon's free tier auto-suspends after ~5 minutes idle, so the first query after a quiet spell
takes 1–3 s. See [Cold starts](#6-cold-starts) below.

---

## 2. Storage — Backblaze B2

1. Create a **public** bucket (Cloudflare needs to read it; your credentials stay server-side).
2. **Application Keys → Add a New Application Key**, scoped to that bucket, with read *and*
   write.
3. Note the **S3 endpoint** from the bucket detail page, e.g.
   `https://s3.us-west-004.backblazeb2.com`.

```
B2_KEY_ID=0045f0…
B2_APPLICATION_KEY=K004xY…
B2_BUCKET_NAME=shimeji-media
B2_ENDPOINT_URL=https://s3.us-west-004.backblazeb2.com
B2_REGION=us-west-004
```

### CORS — required for direct browser uploads

The admin uploader PUTs from a browser to B2, so the bucket must allow it. In **Bucket
Settings → CORS Rules**, add a custom rule:

```json
[
  {
    "corsRuleName": "adminUploads",
    "allowedOrigins": ["https://your-app.onrender.com"],
    "allowedOperations": ["s3_put", "s3_head"],
    "allowedHeaders": ["content-type", "content-length"],
    "exposeHeaders": ["etag"],
    "maxAgeSeconds": 3600
  }
]
```

Without this, presign succeeds and the PUT fails with an opaque browser CORS error.

---

## 3. CDN — Cloudflare in front of B2

1. Add your domain to Cloudflare.
2. Create a CNAME, e.g. `cdn.example.com` → your bucket's B2 friendly URL host, **proxied**
   (orange cloud).
3. Set `MEDIA_CDN_BASE_URL=https://cdn.example.com`.

Because URLs are composed at serialization time and never stored, changing this later is one
environment variable and no migration.

> **⚠ Gotcha — tell Cloudflare to ignore `X-API-Key` in the cache key.**
>
> API responses are identical for every client with the same scope, but a `Vary`-style split on
> the key would give each app its own cache entry and collapse the hit rate. Since the edge cache
> is the main thing keeping a free dyno alive, that matters.
>
> **Caching → Cache Rules → Create rule:**
>
> - **When**: `URI Path starts with /api/v1/`
> - **Then**: Eligible for cache · Edge TTL *"Use cache-control header"* · **Cache Key → Custom →
>   omit `X-API-Key`**
>
> The API already sends `Cache-Control: public, max-age=300, stale-while-revalidate=86400` plus
> an `ETag`, so the edge revalidates rather than refetching.
>
> Note the trade-off this accepts: the edge will serve a cached response to *any* caller for a
> path already fetched by a valid key. The content is a public wallpaper catalog, so that is fine
> — the key exists for attribution, revocation and rate limiting, not confidentiality. If that
> ever stops being true, scope the rule to exclude `/api/v1/admin/`, which it already does by
> virtue of those paths requiring staff auth and sending no cacheable headers.

---

## 4. App — Render

`render.yaml` is a blueprint: **New → Blueprint** and point it at the repo.

Render generates `DJANGO_SECRET_KEY` and `CRON_SECRET`. Set the rest in the dashboard:

```
DJANGO_ALLOWED_HOSTS=your-app.onrender.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://your-app.onrender.com
DJANGO_ADMIN_URL=manage-a7f3c9/          # keep the trailing slash
DATABASE_URL=<Neon pooled DSN>
B2_KEY_ID / B2_APPLICATION_KEY / B2_BUCKET_NAME / B2_ENDPOINT_URL / B2_REGION
MEDIA_CDN_BASE_URL=https://cdn.example.com
```

> **⚠ Gotcha — the free plan has no release phase.**
>
> There is nowhere to run `migrate` before the new instance takes traffic, which is why
> `startCommand` runs it inline. Migrations are idempotent, so a restart or a scaled second
> instance is harmless.

Then bootstrap:

```bash
# Render dashboard → Shell
python manage.py createsuperuser
python manage.py check_deploy
python manage.py create_app_client "Wallpaper Android" --features wallpaper
python manage.py create_app_client "Shimeji Android"   --features shimeji
python manage.py create_app_client "Battery Android"   --features battery
```

Each key is printed **once**. Store it in the app's build config; it cannot be recovered, only
rotated (`--rotate`).

### App — Railway (alternative)

**New Project → Deploy from GitHub**. `railway.json` and `Procfile` are both present, so no
extra configuration is needed beyond the same environment variables. Railway also has built-in
cron, which simplifies the next section.

---

## 5. Scheduled maintenance

Two jobs keep things tidy:

| Job | What it does | Suggested cadence |
| --- | --- | --- |
| `reap_orphans` | Deletes abandoned upload tickets and their orphaned B2 objects; purges archived items past retention | hourly |
| `reconcile_counts` | Rebuilds `item_count` and `has_subcategories` from source | daily |

Both are idempotent and batch-bounded, so overlapping or repeated runs are safe. A response
containing `"more_pending": true` means work remains for the next run.

**Railway** — add two cron services using the `Procfile` entries:

```
0 * * * *   python manage.py reap_orphans
0 3 * * *   python manage.py reconcile_counts
```

**Render free** — there is no cron, so drive the HTTP endpoints externally. Any free scheduler
works ([cron-job.org](https://cron-job.org), or a GitHub Actions `schedule`):

```bash
curl -fsS -X POST -H "X-Cron-Secret: $CRON_SECRET" \
  https://your-app.onrender.com/internal/cron/reap-orphans

curl -fsS -X POST -H "X-Cron-Secret: $CRON_SECRET" \
  https://your-app.onrender.com/internal/cron/reconcile-counts
```

Add `?dry_run=1` to see what a run *would* do without changing anything.

The secret is compared in constant time, and a blank configured secret denies everything rather
than allowing everything — the failure mode of a forgotten environment variable is a closed door.

---

## 6. Cold starts

| Layer | Behaviour | Effect |
| --- | --- | --- |
| Render free web service | Sleeps after ~15 min idle | 30–50 s first request |
| Neon free database | Auto-suspends after ~5 min idle | 1–3 s first query |

A ping every 10 minutes keeps both warm:

```bash
curl -fsS https://your-app.onrender.com/readyz
```

`/readyz` touches the database, so it warms both layers; `/healthz` deliberately does no I/O and
would only wake the dyno. Both are excluded from the access log, so pinging does not bury real
traffic.

Whether to bother is a product call. It costs nothing but does keep a free instance permanently
awake, which some hosts consider abuse of the tier — check current terms.

---

## Post-deploy checklist

```bash
# 1. Configuration, database, storage, and seed data
python manage.py check_deploy

# 2. Health
curl -fsS https://your-app.onrender.com/healthz     # {"status":"ok"}
curl -fsS https://your-app.onrender.com/readyz      # database + storage "ok"

# 3. The three types were seeded
curl -sS -H "X-API-Key: $KEY" .../api/v1/manifest | jq '.data.types[].type'
# "wallpaper" "shimeji" "battery"

# 4. Edge caching is live — second call should be 304, and CF-Cache-Status a HIT
ETAG=$(curl -sSI -H "X-API-Key: $KEY" .../api/v1/wallpapers | grep -i etag | cut -d' ' -f2-)
curl -sSI -H "X-API-Key: $KEY" -H "If-None-Match: $ETAG" .../api/v1/wallpapers | head -1

# 5. A real upload round trip, against the real bucket
#    presign -> PUT -> commit; confirm the item is READY and its preview exists

# 6. Negative paths, each a clean 4xx
curl -sS .../api/v1/wallpapers                                    # 403, no key
curl -sS -H "X-API-Key: bogus" .../api/v1/wallpapers              # 403
curl -sS -H "X-API-Key: $KEY" '.../api/v1/wallpapers?limit=500'   # 400
curl -sS -X POST .../internal/cron/reap-orphans                   # 403, no secret

# 7. Cron jobs work end to end
curl -fsS -X POST -H "X-Cron-Secret: $CRON_SECRET" .../internal/cron/reconcile-counts
```

---

## Scaling past the free tier

In rough order of value per pound:

1. **Confirm the edge cache is working** before paying for anything. A correct Cloudflare cache
   rule removes most traffic from the app entirely; without it, no amount of dyno is enough.
2. **Paid Render/Railway instance** — removes sleep, raises RAM. Then `WEB_CONCURRENCY=4`.
3. **Redis** — set `REDIS_URL` and the cache and throttle counters become shared across workers
   instead of per-process. No code change.
4. **Neon paid tier** — removes auto-suspend and raises the connection ceiling. Only then is
   `CONN_MAX_AGE > 0` worth considering, and only against a **non-pooled** host.
5. **A worker** — if media processing grows beyond Pillow (ffmpeg-generated posters, multiple
   preview sizes), add Celery. Until then the inline commit path is simpler and cheaper.

---

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Intermittent `InterfaceError` / `server closed the connection` | `CONN_MAX_AGE > 0` against Neon's pooled host. Set it to 0 |
| Infinite redirect loop | Missing `SECURE_PROXY_SSL_HEADER`. `prod.py` sets it; check nothing overrode it |
| `image_url` empty on every item | `MEDIA_CDN_BASE_URL` unset |
| Browser upload fails after a successful presign | B2 CORS rule missing or the origin does not match |
| Upload rejected as `content_mismatch` | The declared `content_type` does not match the real bytes — working as intended |
| CDN never reports a HIT | Cache rule missing, or it is not omitting `X-API-Key` from the cache key |
| App refuses to start with `ImproperlyConfigured` | `prod.py` is telling you exactly which variable is missing — read the message |
| Admin CSS missing | `collectstatic` did not run in the build |
| `404` on every admin URL | `DJANGO_ADMIN_URL` is set to something else; it needs a trailing slash |

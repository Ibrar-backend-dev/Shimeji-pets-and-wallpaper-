# Insomnia API testing guide

The collection is at `insomnia/shimeji-api.insomnia.json`. It uses Insomnia's legacy JSON (v4) import format, which current Insomnia versions still support.

## 1. Start the API and create an app key

In PowerShell, from the project directory:

```powershell
.\.venv\Scripts\Activate.ps1
python manage.py migrate
python manage.py createsuperuser
python manage.py create_app_client "Insomnia local" --features wallpaper,shimeji,battery
python manage.py runserver
```

Copy the key printed by `create_app_client` immediately. It is stored as a hash and cannot be shown again. You can use `http://localhost:8000/admin/` to create categories and subcategories before trying the upload endpoints.

For the staff-only collection folder, create a short-lived access token for the superuser (replace `YOUR_USERNAME`):

```powershell
python manage.py shell -c "from django.contrib.auth import get_user_model; from rest_framework_simplejwt.tokens import AccessToken; user = get_user_model().objects.get(username='YOUR_USERNAME'); print(str(AccessToken.for_user(user)))"
```

## 2. Import and configure the collection

1. In Insomnia, select **Import** then **From File**.
2. Select `insomnia/shimeji-api.insomnia.json`.
3. Open **Base Environment** in the imported workspace.
4. Set `api_key` to the app key from the command above.
5. Keep `base_url` as `http://localhost:8000`, or replace it with your deployed server URL. The remaining `*_base_url` variables are calculated from it.
6. Replace `cron_secret` with the `CRON_SECRET` value from your local `.env` when testing maintenance routes.

Never save real API keys, staff JWTs, or cron secrets in a collection you commit or share. In Insomnia, place those values in a private environment/vault for normal use.

## 3. Test public reads first

Send requests in this order:

1. **GET Health (liveness)** — expect `200` and `{ "status": "ok" }`.
2. **GET Ready (database and storage)** — expect `200` locally if the database is available. Storage may be `unconfigured` in development.
3. **GET Manifest** — expect `200`, plus the API envelope with `data.types`.
4. **GET Categories (wallpaper)** — copy a real `data.items[0].id` into `category_id`.
5. **GET Subcategories** — it may correctly return an empty list if that category has no children.
6. **GET Wallpapers**, **GET Shimeji**, or **GET Battery** — copy an item ID from `data.items[0].id` into `item_id`, then try the matching detail and related-item requests.

No uploaded content means the feed endpoints return a valid `200` response with an empty `data.items` array. That is expected.

## 4. Reusable Insomnia scripts

The collection includes base variables. Add the following scripts from a request's **Scripts** tab when you want automatic response chaining.

### Save a returned item ID

Put this in the **After-response** script of a feed request:

```javascript
const body = insomnia.response.json();
const item = body.data?.items?.[0];
if (item?.id) {
  insomnia.environment.set("item_id", item.id);
}
```

### Save a returned category ID

Put this in the **After-response** script of **GET Categories**:

```javascript
const body = insomnia.response.json();
const category = body.data?.items?.[0];
if (category?.id) {
  insomnia.environment.set("category_id", category.id);
}
```

### Save an ETag for the conditional request

Put this in the **After-response** script of any public list request:

```javascript
const etag = insomnia.response.headers.get("ETag");
if (etag) {
  insomnia.environment.set("etag", etag);
}
```

### Save an upload ticket ID

Put this in the **After-response** script of **POST Create upload slot**:

```javascript
const body = insomnia.response.json();
const slot = body.data?.slots?.find((entry) => entry.kind === "ASSET");
if (slot?.ticket_id) {
  insomnia.environment.set("asset_ticket_id", slot.ticket_id);
}
```

## 5. Test ingestion safely

1. Create a staff JWT with the command above, then set `staff_jwt`. This project configures JWT validation but does not expose a token-login endpoint, so generate this local test token from Django's shell.
2. Set `category_id` to an active category whose feature matches the payload `type`.
3. Use **POST Create upload slot**, supplying the exact file name, MIME type, and size.
4. Send a separate `PUT` request to the returned `upload_url`, using the returned `required_headers` exactly. This PUT targets B2 directly, not Django.
5. Save the returned ticket ID, then send **POST Commit uploaded file**.
6. Confirm the new item through the relevant public feed, then use PATCH only if you need to alter editorial fields.

Use **POST Abort upload slots** for unused test tickets. The included delete request archives an item, so only run it for content you intentionally want to remove.

## 6. Expected results and troubleshooting

- `403` public request: missing, wrong, inactive, or tampered `api_key`.
- `401`/`403` admin request: `staff_jwt` is absent, expired, or not a staff user.
- `400` upload request: check that category/type match and that file metadata is accurate.
- `207` upload result: a partial batch success; inspect each entry in `data.results` or `data.rejected`.
- `304` ETag request: success; the feed has not changed and the body is empty.
- `404` detail request: use a real item ID visible to the configured API key.

Maintenance requests are configured with `dry_run=1`. Leave that setting on while validating credentials and results; remove it only when you intend to make changes.

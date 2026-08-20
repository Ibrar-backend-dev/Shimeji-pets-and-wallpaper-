# Resumable upload API (v2)

This API is for a staff web uploader. It requires a staff session or JWT and does not replace the v1 upload API.

1. `POST /api/v2/admin/upload-batches` with `type`, `category_id`, optional `subcategory_id`, and `publish_mode` (`DRAFT` or `IMMEDIATE`).
2. `POST /api/v2/admin/upload-batches/{batch_id}/sessions` for each asset or preview. Supply `kind`, `filename`, `content_type`, `size`, and editorial metadata for assets.
3. Request URLs in bounded groups with `POST /api/v2/admin/multipart-sessions/{session_id}/part-urls`, body `{"part_numbers":[1,2,3]}`. Upload every returned URL directly to B2 with `PUT`.
4. After a reload or connection loss, call `GET .../parts`; request URLs only for missing part numbers. Part lists come from B2, not browser state.
5. `POST .../complete` asks the server to complete the B2 upload after every part is present.
6. `POST .../finalize` validates real bytes and creates the media row. Send `preview_session_id` for videos and large images.
7. Draft batches return `PENDING` media. Publish selected IDs or every pending item with `POST /api/v2/admin/upload-batches/{batch_id}/publish`.

## Browser and B2 requirements

Use at most `MULTIPART_MAX_CONCURRENCY` simultaneous PUTs (default 3) and the returned `part_size` (default 10 MiB). Part URLs are intentionally short-lived; asking for fresh ones never removes already uploaded parts.

Configure the B2 bucket CORS policy for the staff web origin to allow `PUT`, `Content-Type`, and signed request headers, and expose `ETag`. Do not route direct B2 uploads through the CDN hostname.

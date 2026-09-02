# Production image synchronization

## Runtime architecture

```text
MobiWork API
  -> report synchronization
  -> SharePoint monthly visit master
  -> SharePointMonthlyImageSource
  -> image planner
  -> allow-listed streaming downloader
  -> ImageSharePointClient
  -> Data anh/YYYY-MM/<employee>/<customer>/...
```

The persisted SharePoint monthly workbook is the metadata source of truth. The image pipeline does not call VisitData again to rediscover image metadata.

## Production guarantees

- Idempotent deterministic destination paths; an existing non-empty image is skipped.
- Only current and previous calendar month folders are retained.
- Image URLs are accepted only from `IMAGE_ALLOWED_HOSTS` (default `dmsimages.mobiwork.vn`). The production workflow also allows the trusted redirect host `image2.mobiwork.vn`.
- Redirect targets are validated against the same allow-list.
- Images are downloaded as streams and rejected when they exceed `IMAGE_MAX_BYTES` (default 20 MB).
- Empty or non-image payloads are rejected.
- Network timeouts and retryable HTTP failures use bounded retries.
- Individual image failures are recorded in the manifest instead of aborting all later images.
- SharePoint binary writes go through the public `ImageSharePointClient.upload_bytes()` contract rather than domain code reaching into SharePoint private methods.

## State semantics

`Data anh/_state.json` uses schema version 3.

- `last_completed_sync_date`: cursor for the latest completed run, including partial-failure runs.
- `last_successful_sync_date`: latest run with zero image failures.
- `last_run_status`: `success` or `partial_failure`.
- `failed_count`: number of failed images in the last run.
- `retry_from_date`: earliest business date with an unresolved image failure, or `null`.

The next automatic run normally uses a one-day overlap from `last_completed_sync_date`. If a partial run contains an older failed image, `retry_from_date` temporarily widens the scan window so that failure is retried automatically until it succeeds or ages out of the rolling retention window.

## Repair / backfill

Set `IMAGE_FORCE_FROM_DATE=YYYY-MM-DD` for a targeted manual rescan. The value is clamped to the rolling retention window, so the tool cannot accidentally recreate months that should already be expired.

## Important environment settings

```text
IMAGE_REQUEST_TIMEOUT_SECONDS=30
IMAGE_MAX_DOWNLOAD_RETRIES=2
IMAGE_MAX_BYTES=20971520
IMAGE_ALLOWED_HOSTS=dmsimages.mobiwork.vn
IMAGE_FORCE_FROM_DATE=
IMAGE_FAIL_ON_PARTIAL=false
```

`IMAGE_FAIL_ON_PARTIAL=false` keeps the hourly data pipeline running when a small number of individual image URLs are broken. The manifest remains the audit record for those failures.

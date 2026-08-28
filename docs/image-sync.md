# Rolling MobiWork image sync

The image workflow downloads MobiWork visit images into SharePoint and keeps only the current and previous calendar month.

## Source fields

By default the sync reads the enabled `visit` report and uses:

- `ngay`: image/visit date
- `hinh_anh`: image URL or URLs
- `ten_nhan_vien`: employee folder name
- `ma_kh`: customer folder name and filename prefix
- `stt_hinh`: image sequence
- `ghi_ton`: optional filter when `IMAGE_REQUIRE_GHI_TON=true`

All field names can be overridden with environment variables from `.env.example`.

## SharePoint layout

The production workflow targets the existing `MobiWorkDMS` document library and writes:

```text
Data anh/
  _state.json
  2026-07/
    <ten_nhan_vien>/
      <ma_kh>/
        <ma_kh>_<YYYYMMDD>_<stt_hinh>_<url_hash>.<ext>
  2026-08/
    ...
```

Folder/file segments are sanitized for SharePoint-invalid path characters. A URL hash prevents collisions when a row contains multiple images.

## Rolling two-month policy

For a run on 2026-08-28, retained month folders are `2026-07` and `2026-08`.

For a run on 2026-09-01, retained month folders become `2026-08` and `2026-09`; `Data anh/2026-07` is deleted recursively after the sync.

Only folders whose names exactly match `YYYY-MM` are automatically deleted. Other manual folders under `Data anh` are left untouched.

## Backfill and incremental behavior

- First production run: fetch from the first day of the previous month through today.
- Later runs: read `Data anh/_state.json` and fetch from one day before the last successful sync date through today. The one-day overlap covers late-arriving images.
- Existing deterministic image paths are skipped instead of re-uploaded.
- The state file is updated after each production run.

## Workflow

`.github/workflows/mobiwork-images.yml` runs every day at 09:30 in `Asia/Ho_Chi_Minh` and can also be launched manually.

Manual runs default to `dry_run=true`, which scans the MobiWork rows and counts image links but does not download, upload, or delete files.

The workflow reuses the same secrets and Microsoft OIDC configuration as the existing report sync; no additional credential is required.

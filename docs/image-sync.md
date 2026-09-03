# Rolling MobiWork image sync

Image synchronization is driven from the MobiWork visit workbook that has already been persisted to SharePoint. The image job no longer calls the MobiWork VisitData API to rebuild image metadata.

Production order is:

```text
MobiWork API
  -> hourly report sync
  -> SharePoint monthly visit master
  -> image sync reads that SharePoint master
  -> Data anh
```

This keeps report rows and image rows on the same persisted source of truth.

## Source fields

The source report is `visit` / `BaoCaoViengTham` under `01_BaoCaoViengTham/YYYY/MM`.

By default image sync uses:

- `_sync_date`: preferred partition date in the current monthly-master format
- `ngay`: fallback visit date for older History workbooks
- `hinh_anh`: one or more image URLs
- `ten_nhan_vien`: employee folder name
- `ma_kh`: customer folder name and filename prefix
- `stt_hinh`: optional image sequence; image-list position is used when this field is absent
- `ghi_ton`: optional filter when `IMAGE_REQUIRE_GHI_TON=true`

The downloader keeps MobiWork credentials only for retrieving image bytes from MobiWork-hosted URLs. Metadata comes from SharePoint Excel files.

## SharePoint source selection

For each month touched by the incremental window, image sync first looks for the canonical monthly master:

```text
01_BaoCaoViengTham/YYYY/MM/BaoCaoViengTham_YYYY-MM.xlsx
```

For compatibility with older data, if the canonical master is missing it falls back to a compatible `BaoCaoViengTham_History_...xlsx` workbook in the same month folder. Legacy History rows receive a derived `_sync_date` from the calendar portion of `ngay`; values such as `2026-07-18T17:00:00.000Z` remain business date `2026-07-18` rather than being timezone-shifted.

## Image destination

The production target is the existing `MobiWorkDMS` document library:

```text
Data anh/
  _state.json
  2026-08/
    <ten_nhan_vien>/
      <ma_kh>/
        <ma_kh>_<YYYYMMDD>_<stt_hinh>_<url_hash>.<ext>
  2026-09/
    ...
```

Folder/file segments are sanitized for SharePoint-invalid path characters. A URL hash prevents collisions when one visit contains multiple images.

## Rolling two-month policy

Only the current and previous calendar month are retained.

For a run on 2026-08-29, retained folders are `2026-07` and `2026-08`.

For a run on 2026-09-01, retained folders become `2026-08` and `2026-09`; `Data anh/2026-07` is deleted recursively after the sync.

Only folders whose names exactly match `YYYY-MM` are automatically deleted. Manual folders under `Data anh` are not removed.

## Incremental behavior

- First production run scans from the first day of the previous month through today.
- Later runs read `Data anh/_state.json` and scan from one day before the last completed sync date through today.
- Even when a whole monthly workbook is loaded, only rows inside that incremental date window are planned for image work.
- Existing deterministic image paths are skipped instead of re-uploaded.
- Current monthly masters use their exact `_sync_date`; legacy History workbooks preserve the business calendar date encoded by `ngay`.
- Individual broken image links are recorded as `partial_failure` but do not stop the hourly production workflow when `IMAGE_FAIL_ON_PARTIAL=false`.

## Automatic workflow

`.github/workflows/mobiwork-sync.yml` cập nhật báo cáo lúc phút 05 mỗi giờ. Lượt chốt `yesterday` lúc 09:00 và các lượt production chạy thủ công sẽ dispatch `.github/workflows/mobiwork-images.yml` sau khi báo cáo thành công. Image workflow đọc dữ liệu từ SharePoint, tải một batch và tự dispatch batch kế tiếp khi còn backlog và có tiến triển.

Hourly `today` chỉ cập nhật monthly master, tránh tải lại ảnh quá thường xuyên. Image workflow vẫn có thể chạy thủ công để repair/backfill.

No new GitHub secrets are required.

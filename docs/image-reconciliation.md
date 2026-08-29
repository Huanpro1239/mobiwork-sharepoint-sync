# Image reconciliation

Hourly image synchronization remains incremental for normal operation. Incremental state is efficient, but historical gaps that predate retry tracking cannot always be discovered from a cursor alone.

`MobiWork Image Reconciliation` is the integrity safety net:

- scheduled every Sunday at 02:35 Asia/Ho_Chi_Minh;
- reads the persisted SharePoint visit masters for the current and previous calendar month;
- derives every expected image path from `hinh_anh` using the same deterministic naming logic as hourly sync;
- skips non-empty image files already present in `Data anh`;
- downloads only missing images;
- fails the reconciliation workflow when any expected image still cannot be synchronized, while preserving the diagnostic manifest;
- shares the `mobiwork-sharepoint-production` concurrency lock with the hourly workflow so the two processes never write the SharePoint library simultaneously.

The workflow can also be started manually with an optional `from_date`. Scheduled and one-shot marker runs automatically use the first day of the previous calendar month, matching the two-month retention policy.

A one-time `.github/image-reconcile-now` marker is used to trigger an immediate full reconciliation after this safety net is first deployed. Leaving the marker unchanged does not trigger future runs.

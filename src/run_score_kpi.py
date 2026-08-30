from __future__ import annotations

import argparse
import logging
import os

from score_kpi_pipeline import run


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Image Scoring V2.3 + Sales KPI V2.4")
    parser.add_argument(
        "--period",
        help="Optional KPI month in YYYY-MM. Default: current Asia/Ho_Chi_Minh month.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build local outputs without uploading KPI artifacts to SharePoint.",
    )
    return parser


def _bootstrap_cloud_assets() -> None:
    if os.environ.get("AI_RUNTIME_MODE", "").strip().casefold() != "cloud":
        return
    from image_storage import ImageSharePointClient
    from sharepoint_cloud_runtime import sync_cloud_assets

    client = ImageSharePointClient.from_env()
    result = sync_cloud_assets(client)
    logging.getLogger("mobiwork_sync").info(
        "Cloud AI assets ready: downloaded=%s root=%s asset_drive=%s",
        result.downloaded,
        result.root,
        result.asset_drive_id,
    )


def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    try:
        _bootstrap_cloud_assets()
        result = run(period=args.period, dry_run=args.dry_run)
    except Exception:
        logging.getLogger("mobiwork_sync").exception("Image scoring + KPI pipeline failed")
        return 2
    print(f"status={result.status}")
    print(f"workbook={result.workbook_path}")
    print(f"detail_csv={result.detail_csv_path}")
    print(f"manifest={result.manifest_path}")
    if not args.dry_run:
        print(f"sharepoint={result.remote_workbook_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

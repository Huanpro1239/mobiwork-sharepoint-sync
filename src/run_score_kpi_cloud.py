from __future__ import annotations

import argparse
import logging
import os

from image_storage import ImageSharePointClient
from score_kpi_pipeline import run
from sharepoint_cloud_runtime import sync_cloud_assets


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run SharePoint-hosted Image Scoring + Sales KPI on a cloud runner"
    )
    parser.add_argument("--period", help="Optional KPI month in YYYY-MM")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build outputs without publishing KPI artifacts to MobiWorkDMS.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    logger = logging.getLogger("mobiwork_sync")
    try:
        client = ImageSharePointClient.from_env()
        assets = sync_cloud_assets(client)
        logger.info(
            "SharePoint cloud assets ready: root=%s drive=%s files=%s",
            assets.root,
            assets.asset_drive_id,
            assets.downloaded,
        )
        os.environ["AI_PREBUILT_BUNDLE"] = "true"
        os.environ["AI_SYNC_ASSETS"] = "false"

        from scoring.cloud_image_path import install_robust_image_path_lookup
        from scoring.cloud_sample_compat import (
            install_history_sanitizer,
            install_legacy_url_scoring,
        )

        install_robust_image_path_lookup()
        install_history_sanitizer()
        legacy_rows = install_legacy_url_scoring(client)
        logger.info(
            "Cloud checkpoint/catch-up enabled: optional_legacy_urls=%s production_batch_limit=%s",
            legacy_rows,
            os.environ.get("AI_PRODUCTION_MAX_PENDING_IMAGES", "0"),
        )

        result = run(period=args.period, dry_run=args.dry_run)
    except Exception:
        logger.exception("SharePoint cloud image scoring + KPI pipeline failed")
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

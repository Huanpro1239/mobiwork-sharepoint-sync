from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from planning.config import PlanningConfig
from planning.engine import run_shadow
from sharepoint_semantic import SemanticSharePointClient


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    config_path = Path(os.environ.get("PLANNING_CONFIG", "config/planning_sources.json"))
    output_dir = Path(os.environ.get("PLANNING_OUTPUT_DIR", "output/planning"))
    dry_run = os.environ.get("DRY_RUN", "false").strip().lower() in {"1", "true", "yes"}

    config = PlanningConfig.load(config_path)
    client = SemanticSharePointClient.from_env()
    site_id = client.get_site_id()
    drive_id = os.environ.get("SHAREPOINT_DRIVE_ID") or client.get_drive_id(site_id)

    manifest = run_shadow(config, client, drive_id, output_dir)

    if not dry_run:
        remote = config.shadow_output_folder.strip("/")
        client.upload_file(drive_id, output_dir / "planning_shadow.xlsx", remote)
        client.upload_json(drive_id, f"{remote}/planning_manifest.json", manifest)

    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

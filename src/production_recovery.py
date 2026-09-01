from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MAX_REPAIR_LOOKBACK_DAYS = 31


def build_recovery_plan(
    manifest: dict[str, Any],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Return one bounded self-healing plan for a failed production smoke.

    Recovery is intentionally conservative. Only failures explicitly marked repairable
    by production_smoke.py may trigger a production write. Any unrepairable failure
    disables automatic recovery for the whole smoke so source/auth/code failures are
    never hidden by repeated writes.
    """
    target_raw = str(manifest.get("target_date") or "").strip()
    try:
        target = date.fromisoformat(target_raw)
    except ValueError as exc:
        return {
            "eligible": False,
            "reason": f"invalid target_date: {target_raw!r}",
            "report_repair": False,
            "image_repair": False,
            "lookback_days": 0,
            "from_date": target_raw,
            "repair_attempt_budget": 1,
            "unrepairable_failures": [f"target_date: {exc}"],
        }

    current = today or datetime.now(VN_TZ).date()
    age_days = (current - target).days

    failed_reports = [
        item for item in manifest.get("reports", []) if item.get("status") == "failed"
    ]
    repairable_reports = [item for item in failed_reports if item.get("repairable") is True]
    unrepairable = [
        f"report:{item.get('report', '?')}:{item.get('failure_stage', 'unknown')}"
        for item in failed_reports
        if item.get("repairable") is not True
    ]

    image = manifest.get("image_state") or {}
    image_failed = image.get("status") == "failed"
    image_repairable = image_failed and image.get("repairable") is True
    if image_failed and not image_repairable:
        unrepairable.append(f"images:{image.get('failure_stage', 'unknown')}")

    report_repair = bool(repairable_reports)
    image_repair = bool(image_repairable)
    reasons: list[str] = []
    if manifest.get("status") != "failed":
        reasons.append("smoke is not failed")
    if age_days < 1:
        reasons.append("target date is today or in the future")
    if age_days > MAX_REPAIR_LOOKBACK_DAYS:
        reasons.append(
            f"target date is outside {MAX_REPAIR_LOOKBACK_DAYS}-day automatic repair window"
        )
    if unrepairable:
        reasons.append("one or more failures are not safe for automatic repair")
    if not report_repair and not image_repair:
        reasons.append("no repairable failure was found")

    eligible = not reasons
    return {
        "eligible": eligible,
        "reason": "eligible" if eligible else "; ".join(reasons),
        "target_date": target.isoformat(),
        "lookback_days": age_days if 1 <= age_days <= MAX_REPAIR_LOOKBACK_DAYS else 0,
        "from_date": target.isoformat(),
        "report_repair": report_repair,
        "image_repair": image_repair,
        "repair_attempt_budget": 1,
        "repairable_reports": [item.get("report") for item in repairable_reports],
        "unrepairable_failures": unrepairable,
    }


def write_plan(plan: dict[str, Any], path: Path = Path("output/recovery_plan.json")) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def write_github_outputs(plan: dict[str, Any]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not output_path:
        return
    values = {
        "eligible": str(bool(plan.get("eligible"))).lower(),
        "report_repair": str(bool(plan.get("report_repair"))).lower(),
        "image_repair": str(bool(plan.get("image_repair"))).lower(),
        "lookback_days": str(int(plan.get("lookback_days") or 0)),
        "from_date": str(plan.get("from_date") or ""),
        "reason": str(plan.get("reason") or "").replace("\n", " "),
    }
    with open(output_path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def main() -> None:
    manifest_path = Path(
        os.environ.get("SMOKE_MANIFEST_PATH", "output/production_smoke_manifest.json")
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    plan = build_recovery_plan(manifest)
    write_plan(plan)
    write_github_outputs(plan)
    print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol
import logging
LOG = logging.getLogger("mobiwork_sync")


BOOTSTRAP_STATE_PATH = "_sync_state/bootstrap.json"


class BootstrapStateStorage(Protocol):
    def download_json(self, drive_id: str, remote_path: str) -> dict[str, Any] | None: ...


def evaluate_bootstrap_state(state: dict[str, Any] | None) -> tuple[bool, str]:
    if not state:
        return False, "bootstrap state is missing"
    status = str(state.get("status") or "").strip().casefold()
    complete = state.get("bootstrap_complete") is True
    if status != "complete" or not complete:
        return False, f"bootstrap status={status or 'unknown'} complete={complete}"
    return True, "bootstrap history is complete"


def read_bootstrap_state(
    storage: BootstrapStateStorage,
    drive_id: str,
) -> tuple[bool, str, dict[str, Any] | None]:
    state = storage.download_json(drive_id, BOOTSTRAP_STATE_PATH)
    ready, reason = evaluate_bootstrap_state(state)
    return ready, reason, state


def require_bootstrap_ready(
    storage: BootstrapStateStorage,
    drive_id: str,
) -> dict[str, Any]:
    ready, reason, state = read_bootstrap_state(storage, drive_id)
    if not ready:
        raise RuntimeError(
            "Production write is blocked until the full-history bootstrap succeeds: "
            f"{reason}. Run 'MobiWork Bootstrap Full History' first."
        )
    return state or {}


def _write_output(name: str, value: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not output:
        return
    with Path(output).open("a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def run() -> dict[str, Any]:
    from sharepoint_semantic import SemanticSharePointClient

    sharepoint = SemanticSharePointClient.from_env()
    drive_id = os.environ.get("SHAREPOINT_DRIVE_ID", "").strip()
    if not drive_id:
        site_id = sharepoint.get_site_id()
        drive_id = sharepoint.get_drive_id(site_id)

    ready, reason, state = read_bootstrap_state(sharepoint, drive_id)
    payload = {
        "ready": ready,
        "reason": reason,
        "state": state or {},
        "state_path": BOOTSTRAP_STATE_PATH,
    }
    _write_output("ready", "true" if ready else "false")
    _write_output("reason", reason.replace("\n", " "))
    LOG.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return payload


if __name__ == "__main__":
    run()

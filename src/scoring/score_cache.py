from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable, Mapping

from project_paths import SCORE_CACHE_DB


class ScoreCache:
    """SQLite cache keyed by immutable image bytes + model signature."""

    def __init__(self, path: str | Path = SCORE_CACHE_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS image_scores (
                model_signature TEXT NOT NULL,
                image_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (model_signature, image_sha256)
            )
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ScoreCache":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def get(self, model_signature: str, image_sha256: str) -> dict | None:
        row = self.connection.execute(
            "SELECT payload_json FROM image_scores WHERE model_signature=? AND image_sha256=?",
            (model_signature, image_sha256),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, model_signature: str, image_sha256: str, payload: Mapping) -> None:
        encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.connection.execute(
            """
            INSERT INTO image_scores(model_signature, image_sha256, payload_json)
            VALUES (?, ?, ?)
            ON CONFLICT(model_signature, image_sha256)
            DO UPDATE SET payload_json=excluded.payload_json, updated_at=CURRENT_TIMESTAMP
            """,
            (model_signature, image_sha256, encoded),
        )
        self.connection.commit()

    def seed(self, rows: Iterable[Mapping], model_signature: str) -> int:
        inserted = 0
        for row in rows:
            sha = str(row.get("image_sha256", "")).strip()
            signature = str(row.get("pipeline_signature", "")).strip()
            payload_text = row.get("score_payload_json")
            if not sha or signature != model_signature or not payload_text:
                continue
            try:
                payload = json.loads(str(payload_text))
            except (TypeError, ValueError):
                continue
            self.put(model_signature, sha, payload)
            inserted += 1
        return inserted

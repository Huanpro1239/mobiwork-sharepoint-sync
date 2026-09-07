from __future__ import annotations

import logging
import os


def configure() -> None:
    """Central logging configuration for CLI entrypoints.

    Honors the LOG_LEVEL env var and sets a consistent format including logger name.
    """
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

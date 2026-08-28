"""Vikoda planning calculation engine.

Business rules are separated into source-refresh adapters, normalized legacy VBA
ports, domain calculations, production schedulers, and the top-level engine.
Excel is an input/output surface rather than the execution runtime.
"""

__all__ = [
    "config",
    "domain",
    "engine",
    "excel_io",
    "formula_port",
    "normalize",
    "rgb_scheduler",
    "source_refresh",
    "vba_port",
]

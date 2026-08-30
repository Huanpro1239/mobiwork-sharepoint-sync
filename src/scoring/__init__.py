"""Auditable image-scoring V2.3 package.

The package root intentionally avoids importing Torch/Transformers so lightweight
sync/KPI tooling can import cache helpers without loading the AI runtime.
"""

PIPELINE_VERSION = "2.3.0"

__all__ = ["PIPELINE_VERSION"]

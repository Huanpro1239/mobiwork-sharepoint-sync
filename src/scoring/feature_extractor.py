from __future__ import annotations

from typing import Sequence

import numpy as np
import torch
from PIL import Image
from transformers import CLIPModel, CLIPProcessor

from scoring.config import CLIP_MODEL_ID, CLIP_MODEL_REVISION, DEVICE


def resolve_torch_device(configured: str = DEVICE) -> str:
    """Resolve the runtime device only when the heavy CLIP runtime is imported."""

    if configured == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if configured == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("AI_DEVICE=cuda was requested but CUDA is unavailable")
    if configured not in {"cpu", "cuda"}:
        raise ValueError("configured device must be auto, cpu, or cuda")
    return configured


class FeatureExtractor:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_model()
        return cls._instance

    def _init_model(self):
        self.device = resolve_torch_device()
        self.model = CLIPModel.from_pretrained(
            CLIP_MODEL_ID,
            revision=CLIP_MODEL_REVISION,
        ).to(self.device).eval()
        self.processor = CLIPProcessor.from_pretrained(
            CLIP_MODEL_ID,
            revision=CLIP_MODEL_REVISION,
        )

    @staticmethod
    def _as_pil_rgb(image) -> Image.Image:
        if isinstance(image, Image.Image):
            return image.convert("RGB")
        if isinstance(image, np.ndarray):
            if image.ndim != 3 or image.shape[2] != 3 or image.size == 0:
                raise ValueError("CLIP image must be a non-empty HxWx3 array")
            return Image.fromarray(np.asarray(image, dtype=np.uint8), mode="RGB")
        raise TypeError("CLIP image must be a PIL image or RGB numpy array")

    def extract_batch(self, images: Sequence[object], batch_size: int = 32) -> np.ndarray:
        """Extract normalized CLIP embeddings in bounded batches."""

        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if not images:
            projection_dim = int(getattr(self.model.config, "projection_dim", 512))
            return np.empty((0, projection_dim), dtype=np.float32)

        all_embeddings: list[np.ndarray] = []
        for start in range(0, len(images), batch_size):
            pil_batch = [
                self._as_pil_rgb(image)
                for image in images[start : start + batch_size]
            ]
            inputs = self.processor(
                images=pil_batch,
                return_tensors="pt",
                padding=True,
            ).to(self.device)
            with torch.inference_mode():
                embeds = self.model.get_image_features(**inputs)
                if hasattr(embeds, "image_embeds"):
                    embeds = embeds.image_embeds
                elif hasattr(embeds, "pooler_output"):
                    embeds = embeds.pooler_output
                embeds = embeds / embeds.norm(
                    p=2,
                    dim=-1,
                    keepdim=True,
                ).clamp_min(1e-12)
                all_embeddings.append(embeds.detach().cpu().numpy())

        return np.vstack(all_embeddings).astype(np.float32, copy=False)

    def extract_single(self, image_rgb) -> np.ndarray:
        """Backward-compatible single-image path implemented through the batch path."""

        return self.extract_batch([image_rgb], batch_size=1)[0]

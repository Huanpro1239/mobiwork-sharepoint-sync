# Windows self-hosted runner for DMS Image AI + KPI

Heavy CLIP + YOLO + EasyOCR + MediaPipe inference runs on a persistent Windows runner instead of the GitHub-hosted sync worker.

## Required runner labels

`self-hosted`, `windows`, `x64`, `dms-ai`

## Prerequisites

- Windows 11 / Windows Server
- Python 3.12
- Git and Azure CLI
- enough disk for Hugging Face/EasyOCR caches, reference images and SQLite score cache
- optional NVIDIA GPU and the matching PyTorch/CUDA build

## One-time private asset bootstrap

From the existing DMS project directory:

```powershell
python src\bootstrap_model_assets.py --source "D:\DMS cham anh" --dry-run
python src\bootstrap_model_assets.py --source "D:\DMS cham anh"
```

The command uploads only runtime assets to SharePoint:

```text
Model Assets/
├─ reference/...
├─ reference_overrides.csv
├─ weights/yolov8s-world.pt
└─ template/KPI_template.xlsx
```

No model weights, customer images or reference photos are committed to GitHub.

## Manual validation

```powershell
pip install -r requirements-ai.txt
python -m unittest discover -s tests_ai -v
python src\run_score_kpi.py --dry-run
```

When the dry run is correct:

```powershell
python src\run_score_kpi.py
```

Historical month:

```powershell
python src\run_score_kpi.py --period 2026-08
```

"""Paths and settings for the training pipeline.

Everything can be overridden with environment variables so the same code runs on a
laptop, inside the Airflow container (project mounted at /opt/project) and in CI.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(os.getenv("PROJECT_ROOT", Path(__file__).resolve().parents[1]))

DATA_FILE = PROJECT_ROOT / os.getenv("DATA_FILE", "data/raw/breast_cancer.csv")
DVC_FILE = DATA_FILE.with_name(DATA_FILE.name + ".dvc")
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
MODELS_DIR = PROJECT_ROOT / "models"
MODEL_FILE = MODELS_DIR / "model.joblib"          # promoted model, baked into the API image
CANDIDATE_FILE = MODELS_DIR / "candidate.joblib"  # output of the latest training run
METADATA_FILE = MODELS_DIR / "model_metadata.json"
METRICS_FILE = PROJECT_ROOT / "metrics.json"

TARGET_COLUMN = "target"
EXPECTED_FEATURES = 30
MIN_ROWS = 100

MLFLOW_TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000")
EXPERIMENT_NAME = os.getenv("MLFLOW_EXPERIMENT_NAME", "breast-cancer-classification")
REGISTERED_MODEL_NAME = os.getenv("MLFLOW_MODEL_NAME", "breast-cancer-classifier")

# Quality gate: a model is only promoted when it clears these thresholds.
DEFAULT_MIN_F1 = float(os.getenv("QUALITY_GATE_MIN_F1", "0.90"))
DEFAULT_MIN_RECALL = float(os.getenv("QUALITY_GATE_MIN_RECALL", "0.90"))

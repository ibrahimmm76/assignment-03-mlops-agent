"""The five pipeline stages: validate, preprocess, train, evaluate/log, quality gate.

Each stage is a plain function so it can be called from the Airflow DAG, from the
DVC pipeline (``python -m training.train``) and from unit tests. Stages hand data to
each other through small files, which keeps Airflow XComs tiny.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from training import config
from training.provenance import collect

log = logging.getLogger("training")


TRUSTED_SKOPS_TYPES = {"random_forest": ["sklearn.tree._tree.Tree"]}


class ValidationError(RuntimeError):
    """The dataset failed validation; nothing downstream should run."""


class QualityGateError(RuntimeError):
    """The model did not meet the quality thresholds and must not be promoted."""


# --------------------------------------------------------------------------- #
# 1. validation
# --------------------------------------------------------------------------- #
def validate(data_file: Path = config.DATA_FILE, inject_failure: bool = False) -> dict:
    """Schema, size and quality checks. Raises ValidationError on failure."""
    if not data_file.exists():
        raise ValidationError(f"dataset not found at {data_file}; run `dvc pull` or `dvc checkout`")
    frame = pd.read_csv(data_file)
    if inject_failure:
        # Deliberate failure for the demo: simulate a corrupted upstream extract.
        log.warning("inject_failure=bad_data: dropping the target column to simulate a corrupted extract")
        frame = frame.drop(columns=[config.TARGET_COLUMN])

    problems = []
    if config.TARGET_COLUMN not in frame.columns:
        problems.append(f"missing target column '{config.TARGET_COLUMN}'")
    features = [c for c in frame.columns if c != config.TARGET_COLUMN]
    if len(features) != config.EXPECTED_FEATURES:
        problems.append(f"expected {config.EXPECTED_FEATURES} feature columns, found {len(features)}")
    if len(frame) < config.MIN_ROWS:
        problems.append(f"only {len(frame)} rows, need at least {config.MIN_ROWS}")
    nulls = int(frame.isna().sum().sum())
    if nulls:
        problems.append(f"{nulls} missing values")
    if config.TARGET_COLUMN in frame.columns and not set(frame[config.TARGET_COLUMN].unique()) <= {0, 1}:
        problems.append("target column must be binary (0/1)")
    if problems:
        raise ValidationError("data validation failed: " + "; ".join(problems))

    summary = {"rows": len(frame), "features": len(features),
               "positive_rate": round(float(frame[config.TARGET_COLUMN].mean()), 4)}
    log.info("validation passed: %s", summary)
    return summary


# --------------------------------------------------------------------------- #
# 2. preprocessing
# --------------------------------------------------------------------------- #
def preprocess(data_file: Path = config.DATA_FILE, out_dir: Path = config.PROCESSED_DIR,
               test_size: float = 0.2, seed: int = 42) -> dict:
    """Stratified train/test split written to data/processed/."""
    from sklearn.model_selection import train_test_split

    frame = pd.read_csv(data_file)
    train, test = train_test_split(frame, test_size=test_size, random_state=seed,
                                   stratify=frame[config.TARGET_COLUMN])
    out_dir.mkdir(parents=True, exist_ok=True)
    train.to_csv(out_dir / "train.csv", index=False)
    test.to_csv(out_dir / "test.csv", index=False)
    result = {"train_rows": len(train), "test_rows": len(test), "out_dir": str(out_dir)}
    log.info("preprocessing done: %s", result)
    return result


# --------------------------------------------------------------------------- #
# 3. training
# --------------------------------------------------------------------------- #
def build_model(model_type: str, params: dict):
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    if model_type == "logreg":
        return make_pipeline(StandardScaler(), LogisticRegression(
            C=float(params.get("C", 1.0)), max_iter=int(params.get("max_iter", 1000))))
    if model_type == "random_forest":
        return RandomForestClassifier(n_estimators=int(params.get("n_estimators", 200)),
                                      max_depth=params.get("max_depth"), random_state=42)
    raise ValueError(f"unknown model_type {model_type!r}; use 'logreg' or 'random_forest'")


def train(model_type: str = "logreg", params: dict | None = None,
          processed_dir: Path = config.PROCESSED_DIR, model_file: Path = config.CANDIDATE_FILE) -> dict:
    import joblib

    params = params or {}
    frame = pd.read_csv(processed_dir / "train.csv")
    x, y = frame.drop(columns=[config.TARGET_COLUMN]), frame[config.TARGET_COLUMN]
    model = build_model(model_type, params)
    model.fit(x, y)
    model_file.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, model_file)
    log.info("trained %s with %s -> %s", model_type, params, model_file)
    return {"model_type": model_type, "params": params, "model_file": str(model_file)}


# --------------------------------------------------------------------------- #
# 4. evaluation + MLflow logging
# --------------------------------------------------------------------------- #
def evaluate(model_file: Path = config.CANDIDATE_FILE, processed_dir: Path = config.PROCESSED_DIR) -> dict:
    import joblib
    from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score, roc_auc_score

    model = joblib.load(model_file)
    frame = pd.read_csv(processed_dir / "test.csv")
    x, y = frame.drop(columns=[config.TARGET_COLUMN]), frame[config.TARGET_COLUMN]
    pred = model.predict(x)
    proba = model.predict_proba(x)[:, 1]
    metrics = {
        "accuracy": accuracy_score(y, pred),
        "precision": precision_score(y, pred),
        "recall": recall_score(y, pred),
        "f1": f1_score(y, pred),
        "roc_auc": roc_auc_score(y, proba),
    }
    return {k: round(float(v), 4) for k, v in metrics.items()}


def log_to_mlflow(train_info: dict, metrics: dict, validation: dict, source: str,
                  extra_tags: dict | None = None) -> dict:
    """Log params, metrics, dataset provenance and the model; register a new version."""
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    import joblib
    import mlflow
    import mlflow.sklearn

    mlflow.set_tracking_uri(config.MLFLOW_TRACKING_URI)
    mlflow.set_experiment(config.EXPERIMENT_NAME)
    provenance = collect(config.DATA_FILE, config.DVC_FILE, config.PROJECT_ROOT)
    model = joblib.load(train_info["model_file"])

    with mlflow.start_run(run_name=f"{train_info['model_type']}-{datetime.now(UTC):%Y%m%d-%H%M%S}") as run:
        mlflow.log_params({"model_type": train_info["model_type"], **train_info["params"]})
        mlflow.log_params({f"data.{k}": v for k, v in validation.items()})
        mlflow.log_metrics(metrics)
        tags = {**{k: str(v) for k, v in provenance.items() if v is not None},
                "pipeline.source": source, **(extra_tags or {})}
        mlflow.set_tags(tags)
        info = mlflow.sklearn.log_model(
            model, name="model", registered_model_name=config.REGISTERED_MODEL_NAME,
            input_example=pd.read_csv(config.PROCESSED_DIR / "test.csv").drop(columns=[config.TARGET_COLUMN]).head(2),
            # MLflow 3 serialises with skops and only trusts listed types. Tree models are
            # built by this pipeline, so their node storage type is safe to trust.
            skops_trusted_types=TRUSTED_SKOPS_TYPES.get(train_info["model_type"]),
        )
        version = getattr(info, "registered_model_version", None)

    result = {"run_id": run.info.run_id, "experiment": config.EXPERIMENT_NAME, "model_version": version,
              "metrics": metrics, "provenance": provenance}
    log.info("logged run %s (model version %s)", run.info.run_id, version)
    return result


# --------------------------------------------------------------------------- #
# 5. quality gate + promotion
# --------------------------------------------------------------------------- #
def quality_gate(metrics: dict, min_f1: float = config.DEFAULT_MIN_F1,
                 min_recall: float = config.DEFAULT_MIN_RECALL) -> dict:
    failures = []
    if metrics["f1"] < min_f1:
        failures.append(f"f1 {metrics['f1']} < {min_f1}")
    if metrics["recall"] < min_recall:
        failures.append(f"recall {metrics['recall']} < {min_recall}")
    if failures:
        raise QualityGateError("quality gate failed: " + "; ".join(failures))
    log.info("quality gate passed (f1=%s, recall=%s)", metrics["f1"], metrics["recall"])
    return {"passed": True, "min_f1": min_f1, "min_recall": min_recall}


def promote(run_result: dict, alias: str = "champion") -> dict:
    """Point the registry alias at the new version and export the model for the API image.

    Only called after the quality gate passed, so models/model.joblib is always a model
    that cleared the gate.
    """
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    import shutil

    from mlflow import MlflowClient

    client = MlflowClient(tracking_uri=config.MLFLOW_TRACKING_URI)
    version = run_result.get("model_version")
    if version:
        client.set_registered_model_alias(config.REGISTERED_MODEL_NAME, alias, str(version))
        client.set_model_version_tag(config.REGISTERED_MODEL_NAME, str(version), "quality_gate", "passed")
    client.set_tag(run_result["run_id"], "quality_gate", "passed")
    shutil.copyfile(config.CANDIDATE_FILE, config.MODEL_FILE)
    write_metadata(run_result, promoted_alias=alias if version else None)
    return {"model": config.REGISTERED_MODEL_NAME, "version": version, "alias": alias}


def mark_gate_failed(run_result: dict, reason: str) -> None:
    """Record on the registered version that it was blocked, so nobody promotes it by mistake."""
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    from mlflow import MlflowClient

    version = run_result.get("model_version")
    client = MlflowClient(tracking_uri=config.MLFLOW_TRACKING_URI)
    client.set_tag(run_result["run_id"], "quality_gate", "failed")
    client.set_tag(run_result["run_id"], "quality_gate.reason", reason)
    if version:
        client.set_model_version_tag(config.REGISTERED_MODEL_NAME, str(version), "quality_gate", "failed")


def write_metadata(run_result: dict, promoted_alias: str | None = None) -> Path:
    """models/model_metadata.json is baked into the API image so it can report its version."""
    meta = {
        "model_name": config.REGISTERED_MODEL_NAME,
        "model_version": run_result.get("model_version"),
        "alias": promoted_alias,
        "run_id": run_result.get("run_id"),
        "metrics": run_result.get("metrics"),
        "dataset_md5": (run_result.get("provenance") or {}).get("dataset.dvc_md5"),
        "git_commit": (run_result.get("provenance") or {}).get("git.commit"),
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    config.METADATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.METADATA_FILE.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return config.METADATA_FILE

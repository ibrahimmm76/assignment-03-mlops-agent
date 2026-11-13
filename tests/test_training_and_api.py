"""Training-pipeline guards and the inference API."""

from __future__ import annotations

import importlib

import joblib
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from data.generate_dataset import build
from training import steps
from training.provenance import dvc_info, file_md5, git_commit


@pytest.fixture
def dataset(tmp_path):
    path = tmp_path / "breast_cancer.csv"
    build("v2").to_csv(path, index=False)
    return path


def test_validation_passes_on_good_data_and_blocks_bad_data(dataset):
    assert steps.validate(dataset)["rows"] == 569
    with pytest.raises(steps.ValidationError, match="missing target column"):
        steps.validate(dataset, inject_failure=True)


def test_quality_gate_blocks_weak_models():
    assert steps.quality_gate({"f1": 0.95, "recall": 0.95}, min_f1=0.9)["passed"]
    with pytest.raises(steps.QualityGateError, match="f1 0.85 < 0.9"):
        steps.quality_gate({"f1": 0.85, "recall": 0.95}, min_f1=0.9)


def test_train_and_evaluate_offline(dataset, tmp_path):
    steps.preprocess(dataset, tmp_path / "processed")
    info = steps.train("logreg", {"C": 1.0}, tmp_path / "processed", tmp_path / "candidate.joblib")
    metrics = steps.evaluate(tmp_path / "candidate.joblib", tmp_path / "processed")
    assert info["model_type"] == "logreg" and metrics["f1"] > 0.9


def test_provenance_reads_dvc_hash_and_git_commit(tmp_path, dataset):
    (tmp_path / "x.dvc").write_text(f"outs:\n- md5: {file_md5(dataset)}\n  size: 1\n  path: breast_cancer.csv\n")
    assert dvc_info(tmp_path / "x.dvc")["md5"] == file_md5(dataset)
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text("8cd0a69b05ec37cd02869fbd6e53f008f457aec5\n")
    assert git_commit(tmp_path) == "8cd0a69b05ec37cd02869fbd6e53f008f457aec5"


def _api(monkeypatch, model_path):
    monkeypatch.setenv("MODEL_PATH", str(model_path))
    monkeypatch.setenv("APP_VERSION", "9.9.9")
    import api.main

    return importlib.reload(api.main)


def test_api_serves_predictions_and_reports_version(monkeypatch, dataset, tmp_path):
    steps.preprocess(dataset, tmp_path / "processed")
    steps.train("logreg", {}, tmp_path / "processed", tmp_path / "model.joblib")
    module = _api(monkeypatch, tmp_path / "model.joblib")
    with TestClient(module.app) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get("/version").json()["app_version"] == "9.9.9"
        row = pd.read_csv(dataset).drop(columns=["target"]).head(1).to_dict("records")
        body = client.post("/predict", json={"rows": row}).json()
        assert body["predictions"][0]["label"] in (0, 1)
        assert client.post("/predict", json={"rows": [{"x": 1.0}]}).status_code == 422
    assert joblib.load(tmp_path / "model.joblib") is not None


def test_api_reports_not_ready_when_model_is_missing(monkeypatch, tmp_path):
    module = _api(monkeypatch, tmp_path / "missing.joblib")
    with TestClient(module.app) as client:
        assert client.get("/health/live").status_code == 200   # process alive: no restart loop
        ready = client.get("/health/ready")
        assert ready.status_code == 503 and "model file not found" in ready.json()["detail"]

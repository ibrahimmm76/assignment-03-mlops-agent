"""Agent settings, read from environment variables (and .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _csv(value: str | None, default: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value if value is not None else default).split(",") if v.strip())


@dataclass(frozen=True)
class Settings:
    project_root: Path = PROJECT_ROOT

    # MLflow
    mlflow_tracking_uri: str = "http://localhost:5000"
    mlflow_experiment: str = "breast-cancer-classification"
    mlflow_model_name: str = "breast-cancer-classifier"

    # Airflow 3 REST API
    airflow_base_url: str = "http://localhost:8080"
    airflow_username: str = "admin"
    airflow_password: str = field(default="", repr=False)
    approved_dags: tuple[str, ...] = ("training_pipeline",)

    # Kubernetes
    k8s_context: str | None = None
    k8s_allowed_namespaces: tuple[str, ...] = ("mlops-demo",)
    k8s_default_namespace: str = "mlops-demo"

    # DVC
    dvc_reproduce_dir: Path = PROJECT_ROOT / "reproductions"

    # Retraining policy (used as evidence when recommending retraining)
    retrain_max_model_age_days: int = 30
    quality_min_f1: float = 0.90

    # Agent behaviour
    tool_timeout_s: float = 30.0
    max_steps: int = 25
    log_file: Path = PROJECT_ROOT / "logs" / "agent.jsonl"

    @classmethod
    def from_env(cls) -> Settings:
        e = os.environ.get
        return cls(
            mlflow_tracking_uri=e("MLFLOW_TRACKING_URI", cls.mlflow_tracking_uri),
            mlflow_experiment=e("MLFLOW_EXPERIMENT_NAME", cls.mlflow_experiment),
            mlflow_model_name=e("MLFLOW_MODEL_NAME", cls.mlflow_model_name),
            airflow_base_url=e("AIRFLOW_BASE_URL", cls.airflow_base_url).rstrip("/"),
            airflow_username=e("AIRFLOW_USERNAME", cls.airflow_username),
            airflow_password=e("AIRFLOW_PASSWORD", ""),
            approved_dags=_csv(e("AIRFLOW_APPROVED_DAGS"), "training_pipeline"),
            k8s_context=e("K8S_CONTEXT") or None,
            k8s_allowed_namespaces=_csv(e("K8S_ALLOWED_NAMESPACES"), "mlops-demo"),
            k8s_default_namespace=e("K8S_NAMESPACE", "mlops-demo"),
            retrain_max_model_age_days=int(e("RETRAIN_MAX_MODEL_AGE_DAYS", "30")),
            quality_min_f1=float(e("QUALITY_GATE_MIN_F1", "0.90")),
            tool_timeout_s=float(e("AGENT_TOOL_TIMEOUT", "30")),
            max_steps=int(e("AGENT_MAX_STEPS", "25")),
            log_file=Path(e("AGENT_LOG_FILE", str(PROJECT_ROOT / "logs" / "agent.jsonl"))),
        )

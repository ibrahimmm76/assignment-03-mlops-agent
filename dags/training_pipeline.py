"""Training pipeline DAG (Airflow 3).

    validate_data -> preprocess -> train_model -> evaluate_and_log -> quality_gate -> promote_model

* Every task retries on transient errors (2 retries, 30 s apart).
* A failed validation stops the run: nothing downstream runs (AirflowFailException, no retries).
* A failed quality gate also fails fast, so ``promote_model`` never runs and the model that
  is currently deployed stays in place.
* Run parameters (set when triggering, e.g. from the agent or the UI):
    model_type      "logreg" or "random_forest"
    C, n_estimators model hyper-parameters
    min_f1          quality-gate threshold
    inject_failure  "none" or "bad_data" (deliberate failure for the demo)

The project folder is mounted into the Airflow container at /opt/project, so the DAG
uses the same training code as the CLI and the DVC pipeline.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta

from airflow.sdk import Param, dag, get_current_context, task
from airflow.sdk.exceptions import AirflowFailException

PROJECT_ROOT = os.getenv("PROJECT_ROOT", "/opt/project")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

log = logging.getLogger("airflow.task")

DEFAULT_ARGS = {
    "owner": "mlops",
    "retries": 2,
    "retry_delay": timedelta(seconds=30),
    "retry_exponential_backoff": True,
}


@dag(
    dag_id="training_pipeline",
    description="Validate data, train, evaluate, log to MLflow and promote only if the quality gate passes",
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args=DEFAULT_ARGS,
    tags=["mlops", "training"],
    params={
        "model_type": Param("logreg", enum=["logreg", "random_forest"]),
        "C": Param(1.0, type="number", minimum=0.0001),
        "n_estimators": Param(200, type="integer", minimum=10, maximum=2000),
        "min_f1": Param(0.90, type="number", minimum=0, maximum=1),
        "inject_failure": Param("none", enum=["none", "bad_data"]),
    },
)
def training_pipeline():
    @task
    def validate_data() -> dict:
        from training import steps

        params = get_current_context()["params"]
        try:
            return steps.validate(inject_failure=params["inject_failure"] == "bad_data")
        except steps.ValidationError as exc:
            # Bad data will not fix itself on retry: fail immediately.
            raise AirflowFailException(str(exc)) from exc

    @task
    def preprocess(_validation: dict) -> dict:
        from training import steps

        return steps.preprocess()

    @task
    def train_model(_split: dict) -> dict:
        from training import steps

        params = get_current_context()["params"]
        model_type = params["model_type"]
        hyper = {"C": params["C"]} if model_type == "logreg" else {"n_estimators": params["n_estimators"]}
        return steps.train(model_type, hyper)

    @task
    def evaluate_and_log(train_info: dict, validation: dict) -> dict:
        from training import steps

        ctx = get_current_context()
        metrics = steps.evaluate()
        run = steps.log_to_mlflow(train_info, metrics, validation, source="airflow",
                                  extra_tags={"airflow.dag_id": ctx["dag"].dag_id,
                                              "airflow.run_id": ctx["run_id"]})
        log.info("evaluation metrics: %s", metrics)
        return run

    @task
    def quality_gate(run: dict) -> dict:
        from training import steps

        params = get_current_context()["params"]
        try:
            return steps.quality_gate(run["metrics"], min_f1=float(params["min_f1"]))
        except steps.QualityGateError as exc:
            steps.mark_gate_failed(run, str(exc))
            # The model is not good enough: retrying cannot help, and promotion must not happen.
            raise AirflowFailException(str(exc)) from exc

    @task
    def promote_model(run: dict, _gate: dict) -> dict:
        from training import steps

        return steps.promote(run)

    validation = validate_data()
    split = preprocess(validation)
    trained = train_model(split)
    run = evaluate_and_log(trained, validation)
    gate = quality_gate(run)
    promote_model(run, gate)


training_pipeline()

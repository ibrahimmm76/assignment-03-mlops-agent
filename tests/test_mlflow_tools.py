"""MLflow tools with a fake MlflowClient."""

from __future__ import annotations

import time
from types import SimpleNamespace as NS

import pytest

from agent.errors import InvalidRequest, ServiceUnavailable
from agent.tools.mlflow_tools import MlflowTools


def run(run_id, f1, gate="passed", md5="597f0797", recall=None):
    return NS(info=NS(run_id=run_id, run_name=f"run-{run_id}", status="FINISHED", start_time=1_700_000_000_000,
                      end_time=None),
              data=NS(metrics={"f1": f1, "recall": recall if recall is not None else f1},
                      params={"model_type": "logreg"},
                      tags={"quality_gate": gate, "dataset.dvc_md5": md5}))


class FakeClient:
    def __init__(self, runs):
        self.runs = runs

    def get_experiment_by_name(self, name):
        return NS(experiment_id="1", name=name) if name == "breast-cancer-classification" else None

    def search_experiments(self):
        return [NS(experiment_id="1", name="breast-cancer-classification", lifecycle_stage="active",
                   creation_time=None)]

    def search_runs(self, ids, max_results=10, order_by=None):
        return self.runs[:max_results]

    def get_run(self, run_id):
        return next(r for r in self.runs if r.info.run_id == run_id)

    def get_model_version_by_alias(self, name, alias):
        return NS(version="5", run_id="a", creation_timestamp=(time.time() - 40 * 86400) * 1000)


def test_best_run_prefers_runs_that_passed_the_quality_gate(settings):
    client = FakeClient([run("a", 0.97), run("b", 0.99, gate="failed"), run("c", 0.95)])
    result = MlflowTools(settings, client=client).find_best_run("f1")
    assert result["best"]["run_id"] == "a"
    assert result["ranking"][0]["run_id"] == "b"
    assert any("did not pass the quality gate" in c for c in result["caveats"])
    assert any("production-ready" in c for c in result["caveats"])


def test_compare_runs_reports_best_per_metric(settings):
    client = FakeClient([run("a", 0.97, recall=0.99), run("b", 0.98, recall=0.95)])
    result = MlflowTools(settings, client=client).compare_runs(last_n=2)
    assert result["best_per_metric"]["f1"]["run_id"] == "b"
    assert result["best_per_metric"]["recall"]["run_id"] == "a"


def test_freshness_policy_flags_old_model(settings):
    result = MlflowTools(settings, client=FakeClient([run("a", 0.97)])).check_model_freshness()
    assert result["retraining_indicated_by_policy"] is True
    assert "days old" in result["policy_reasons"][0]


def test_unknown_metric_and_unreachable_server(settings):
    tools = MlflowTools(settings, client=FakeClient([]))
    with pytest.raises(InvalidRequest):
        tools.find_best_run("made_up_metric")

    class Down(FakeClient):
        def get_experiment_by_name(self, name):
            raise Exception("API request to http://localhost:5000 failed: Max retries exceeded")

    with pytest.raises(ServiceUnavailable):
        MlflowTools(settings, client=Down([])).list_runs()

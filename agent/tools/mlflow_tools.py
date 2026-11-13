"""MLflow experiment-intelligence tools, built on ``MlflowClient``."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from agent.config import Settings
from agent.errors import InvalidRequest, ServiceUnavailable
from agent.schemas import ToolSpec, array, boolean, integer, obj, string

# Fail fast instead of MLflow's default of many retries when the server is down.
os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "1")
os.environ.setdefault("MLFLOW_HTTP_REQUEST_BACKOFF_FACTOR", "0")
os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "10")
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

KEY_TAGS = ("dataset.dvc_md5", "dataset.matches_dvc", "git.commit", "quality_gate", "quality_gate.reason",
            "pipeline.source", "airflow.run_id")
HIGHER_IS_BETTER = {"accuracy", "precision", "recall", "f1", "roc_auc"}


def _ts(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat(timespec="seconds") if ms else None


class MlflowTools:
    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.settings = settings
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from mlflow import MlflowClient

            self._client = MlflowClient(tracking_uri=self.settings.mlflow_tracking_uri)
        return self._client

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            text = str(exc)
            if any(s in text for s in ("Max retries exceeded", "Connection refused", "Failed to establish",
                                        "NewConnectionError", "ConnectionError", "timed out", "API request to")):
                raise ServiceUnavailable(f"MLflow server at {self.settings.mlflow_tracking_uri} is unreachable: "
                                         f"{text[:200]}") from exc
            if "RESOURCE_DOES_NOT_EXIST" in text or "not found" in text.lower():
                raise InvalidRequest(text[:300]) from exc
            raise

    def _experiment(self, name: str | None):
        name = name or self.settings.mlflow_experiment
        exp = self._call(self.client.get_experiment_by_name, name)
        if exp is None:
            names = [e.name for e in self._call(self.client.search_experiments)]
            raise InvalidRequest(f"experiment {name!r} not found; available: {names}")
        return exp

    @staticmethod
    def _summary(run) -> dict[str, Any]:
        tags = run.data.tags
        return {
            "run_id": run.info.run_id,
            "run_name": run.info.run_name,
            "status": run.info.status,
            "start_time": _ts(run.info.start_time),
            "metrics": {k: round(v, 4) for k, v in run.data.metrics.items()},
            "params": run.data.params,
            "tags": {k: tags[k] for k in KEY_TAGS if k in tags},
        }

    # ------------------------------------------------------------------ tools
    def list_experiments(self) -> dict[str, Any]:
        exps = self._call(self.client.search_experiments)
        return {"tracking_uri": self.settings.mlflow_tracking_uri,
                "experiments": [{"experiment_id": e.experiment_id, "name": e.name, "lifecycle": e.lifecycle_stage,
                                 "created": _ts(e.creation_time)} for e in exps]}

    def list_runs(self, experiment: str | None = None, max_results: int = 10,
                  order_by: str = "start_time DESC") -> dict[str, Any]:
        exp = self._experiment(experiment)
        max_results = max(1, min(int(max_results), 50))
        if order_by.split()[0] not in {"start_time", "end_time"} and not order_by.startswith("metrics."):
            raise InvalidRequest("order_by must be 'start_time DESC' or like 'metrics.f1 DESC'")
        runs = self._call(self.client.search_runs, [exp.experiment_id], max_results=max_results, order_by=[order_by])
        return {"experiment": exp.name, "count": len(runs), "runs": [self._summary(r) for r in runs]}

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self._call(self.client.get_run, run_id)
        artifacts = [a.path for a in self._call(self.client.list_artifacts, run_id)]
        out = self._summary(run)
        out["all_tags"] = {k: v for k, v in run.data.tags.items() if not k.startswith("mlflow.")}
        out["artifacts"] = artifacts
        out["end_time"] = _ts(run.info.end_time)
        versions = self._call(self.client.search_model_versions, f"run_id='{run_id}'")
        out["model_versions"] = []
        for v in versions:
            aliases = [a for a, ver in (self._call(self.client.get_registered_model, v.name).aliases or {}).items()
                       if str(ver) == str(v.version)]
            out["model_versions"].append({"name": v.name, "version": v.version, "aliases": aliases,
                                          "tags": dict(v.tags or {})})
        return out

    def find_best_run(self, metric: str = "f1", experiment: str | None = None, last_n: int = 20,
                      require_quality_gate: bool = True) -> dict[str, Any]:
        if metric not in HIGHER_IS_BETTER:
            raise InvalidRequest(f"unsupported metric {metric!r}; use one of {sorted(HIGHER_IS_BETTER)}")
        exp = self._experiment(experiment)
        runs = self._call(self.client.search_runs, [exp.experiment_id], max_results=max(1, min(int(last_n), 100)),
                          order_by=["start_time DESC"])
        scored = [r for r in runs if metric in r.data.metrics and r.info.status == "FINISHED"]
        if not scored:
            return {"experiment": exp.name, "metric": metric, "best": None,
                    "note": f"no finished runs with metric '{metric}' among the last {len(runs)} runs"}
        ranked = sorted(scored, key=lambda r: r.data.metrics[metric], reverse=True)
        if require_quality_gate:
            eligible = [r for r in ranked if r.data.tags.get("quality_gate") == "passed"]
        else:
            eligible = ranked
        best = eligible[0] if eligible else None
        top = ranked[0]
        caveats = []
        if best is None:
            caveats.append("no run in the window passed the quality gate")
        elif best.info.run_id != top.info.run_id:
            caveats.append(f"run {top.info.run_id} has the highest {metric} but did not pass the quality gate")
        if best is not None:
            md5s = {r.data.tags.get("dataset.dvc_md5") for r in ranked[:5]}
            if len(md5s - {None}) > 1:
                caveats.append("the top runs were trained on different dataset versions, so scores are not "
                               "directly comparable")
            if len([r for r in ranked if abs(r.data.metrics[metric] - best.data.metrics[metric]) < 0.005]) > 1:
                caveats.append(f"several runs are within 0.005 {metric} of each other; the difference may not be "
                               "meaningful on this test set")
        caveats.append(f"a high {metric} alone does not make a model production-ready: check recall/precision, "
                       "dataset version, quality-gate status and deployment health")
        return {"experiment": exp.name, "metric": metric, "runs_considered": len(scored),
                "best": self._summary(best) if best else None,
                "ranking": [{"run_id": r.info.run_id, "run_name": r.info.run_name,
                             metric: round(r.data.metrics[metric], 4),
                             "quality_gate": r.data.tags.get("quality_gate", "unknown")} for r in ranked[:10]],
                "caveats": caveats}

    def compare_runs(self, run_ids: list[str] | None = None, experiment: str | None = None,
                     last_n: int = 5) -> dict[str, Any]:
        if run_ids:
            runs = [self._call(self.client.get_run, rid) for rid in run_ids[:10]]
        else:
            exp = self._experiment(experiment)
            runs = self._call(self.client.search_runs, [exp.experiment_id],
                              max_results=max(2, min(int(last_n), 20)), order_by=["start_time DESC"])
        if len(runs) < 2:
            raise InvalidRequest("need at least two runs to compare")
        metrics = sorted({m for r in runs for m in r.data.metrics})
        best_per_metric = {}
        for m in metrics:
            vals = [(r.data.metrics[m], r.info.run_id) for r in runs if m in r.data.metrics]
            if vals:
                v, rid = max(vals) if m in HIGHER_IS_BETTER else min(vals)
                best_per_metric[m] = {"run_id": rid, "value": round(v, 4)}
        return {"runs": [self._summary(r) for r in runs], "best_per_metric": best_per_metric}

    def compare_experiments(self, experiments: list[str], metric: str = "f1") -> dict[str, Any]:
        if not experiments or len(experiments) < 2:
            raise InvalidRequest("give at least two experiment names")
        out = []
        for name in experiments[:5]:
            exp = self._experiment(name)
            runs = self._call(self.client.search_runs, [exp.experiment_id], max_results=100)
            vals = [r.data.metrics[metric] for r in runs if metric in r.data.metrics]
            out.append({"experiment": exp.name, "runs": len(runs), "runs_with_metric": len(vals),
                        f"best_{metric}": round(max(vals), 4) if vals else None,
                        f"mean_{metric}": round(sum(vals) / len(vals), 4) if vals else None})
        return {"metric": metric, "experiments": out}

    def get_model_registry(self, model_name: str | None = None) -> dict[str, Any]:
        name = model_name or self.settings.mlflow_model_name
        try:
            model = self._call(self.client.get_registered_model, name)
        except InvalidRequest as exc:
            names = [m.name for m in self._call(self.client.search_registered_models)]
            raise InvalidRequest(f"registered model {name!r} not found; available: {names}") from exc
        versions = self._call(self.client.search_model_versions, f"name='{name}'")
        aliases = dict(model.aliases or {})  # search results do not include aliases; map them here
        by_version: dict[str, list[str]] = {}
        for alias, ver in aliases.items():
            by_version.setdefault(str(ver), []).append(alias)
        return {"model": name, "aliases": aliases,
                "versions": [{"version": v.version, "run_id": v.run_id, "status": v.status,
                              "aliases": by_version.get(str(v.version), []), "created": _ts(v.creation_timestamp),
                              "tags": dict(v.tags or {})}
                             for v in sorted(versions, key=lambda v: int(v.version), reverse=True)[:20]]}

    def check_model_freshness(self, model_name: str | None = None, alias: str = "champion") -> dict[str, Any]:
        """Evidence for retraining decisions: age and gate status of the promoted model vs. policy."""
        name = model_name or self.settings.mlflow_model_name
        try:
            version = self._call(self.client.get_model_version_by_alias, name, alias)
        except InvalidRequest:
            return {"model": name, "alias": alias, "found": False,
                    "note": f"no version of {name!r} has alias '{alias}'"}
        run = self._call(self.client.get_run, version.run_id)
        created = version.creation_timestamp / 1000
        age_days = round((datetime.now(UTC).timestamp() - created) / 86400, 1)
        f1 = run.data.metrics.get("f1")
        policy = {"max_model_age_days": self.settings.retrain_max_model_age_days,
                  "min_f1": self.settings.quality_min_f1}
        reasons = []
        if age_days > policy["max_model_age_days"]:
            reasons.append(f"model is {age_days} days old, policy maximum is {policy['max_model_age_days']}")
        if f1 is not None and f1 < policy["min_f1"]:
            reasons.append(f"recorded f1 {round(f1, 4)} is below the policy minimum {policy['min_f1']}")
        return {"model": name, "alias": alias, "found": True, "version": version.version, "run_id": version.run_id,
                "created": _ts(version.creation_timestamp), "age_days": age_days,
                "metrics": {k: round(v, 4) for k, v in run.data.metrics.items()},
                "dataset_md5": run.data.tags.get("dataset.dvc_md5"), "policy": policy,
                "retraining_indicated_by_policy": bool(reasons), "policy_reasons": reasons,
                "note": "no model-degradation data is recorded in MLflow; production performance is unknown"}

    # ------------------------------------------------------------------ registry
    def specs(self) -> list[ToolSpec]:
        exp = {"experiment": string("Experiment name (default: the project experiment)")}
        return [
            ToolSpec("mlflow_list_experiments", "mlflow", "List MLflow experiments.", obj(), self.list_experiments),
            ToolSpec("mlflow_list_runs", "mlflow",
                     "List recent training runs with metrics, parameters and provenance tags (dataset md5, git "
                     "commit, quality-gate result, Airflow run).",
                     obj({**exp, "max_results": integer("Number of runs (1-50, default 10)"),
                          "order_by": string("'start_time DESC' (default) or e.g. 'metrics.f1 DESC'")}),
                     self.list_runs),
            ToolSpec("mlflow_get_run", "mlflow",
                     "Full details of one run: metrics, parameters, all tags (including dataset provenance), "
                     "artifacts and registered model versions.", obj({"run_id": string("MLflow run id")}, ["run_id"]),
                     self.get_run),
            ToolSpec("mlflow_find_best_run", "mlflow",
                     "Rank recent runs by a metric and pick the best candidate, preferring runs that passed the "
                     "quality gate. Returns the ranking and caveats.",
                     obj({"metric": string("Metric to rank by", sorted(HIGHER_IS_BETTER)), **exp,
                          "last_n": integer("How many recent runs to consider (default 20)"),
                          "require_quality_gate": boolean("Only pick runs that passed the quality gate "
                                                          "(default true)")}),
                     self.find_best_run),
            ToolSpec("mlflow_compare_runs", "mlflow",
                     "Compare specific runs (run_ids) or the last N runs of an experiment, with the best run per "
                     "metric.", obj({"run_ids": array("Run ids to compare"), **exp,
                                     "last_n": integer("Number of recent runs if run_ids is empty (default 5)")}),
                     self.compare_runs),
            ToolSpec("mlflow_compare_experiments", "mlflow",
                     "Compare two or more experiments by the best and mean value of a metric.",
                     obj({"experiments": array("Experiment names"),
                          "metric": string("Metric (default f1)")}, ["experiments"]), self.compare_experiments),
            ToolSpec("mlflow_get_model_registry", "mlflow",
                     "Registered model versions, aliases (e.g. champion) and quality-gate tags.",
                     obj({"model_name": string("Registered model name (default: project model)")}),
                     self.get_model_registry),
            ToolSpec("mlflow_check_model_freshness", "mlflow",
                     "Age, metrics and dataset hash of the promoted model compared with the retraining policy. "
                     "Use this as evidence before recommending retraining.",
                     obj({"model_name": string("Registered model name"),
                          "alias": string("Alias of the promoted model (default champion)")}),
                     self.check_model_freshness),
        ]

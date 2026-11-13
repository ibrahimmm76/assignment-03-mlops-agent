"""Airflow 3 API client with a fake HTTP session."""

from __future__ import annotations

import pytest
import requests

from agent.errors import ServiceUnavailable
from agent.tools.airflow_tools import AirflowTools


class FakeResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code, self._payload = status, payload
        self.ok = status < 400
        self.content = b"x"
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, routes: dict, expire_first_token: bool = False) -> None:
        self.routes, self.calls, self.tokens = routes, [], 0
        self.expire_first_token = expire_first_token

    def post(self, url, json=None, timeout=None):
        self.tokens += 1
        return FakeResponse(200, {"access_token": f"token-{self.tokens}"})

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append((method, url, headers["Authorization"]))
        if self.expire_first_token and headers["Authorization"] == "Bearer token-1":
            return FakeResponse(401, {"detail": "expired"})
        for (m, suffix), resp in self.routes.items():
            if m == method and url.endswith(suffix):
                return FakeResponse(*resp)
        return FakeResponse(404, {"detail": "not found"})


FAILED_LOG = {"content": [
    {"event": "::group::Pre Execute"},
    {"event": "Task failed with exception", "level": "error", "error_detail": [{
        "exc_type": "AirflowFailException", "exc_value": "data validation failed: missing target column 'target'",
        "frames": [{"filename": "/opt/project/dags/training_pipeline.py", "lineno": 69, "name": "validate_data"}]}]},
]}
RUN = "manual__2026-11-11T17:50:00"
TASKS = {"task_instances": [
    {"task_id": "validate_data", "state": "failed", "try_number": 1, "duration": 0.4, "start_date": "1"},
    {"task_id": "preprocess", "state": "upstream_failed", "try_number": 0, "duration": None, "start_date": None},
]}


def tools_with(settings, policy, routes, **kw) -> tuple[AirflowTools, FakeSession]:
    session = FakeSession(routes, **kw)
    return AirflowTools(settings, policy, session=session), session


def test_failed_task_diagnosis_extracts_root_cause(settings, policy):
    tools, _ = tools_with(settings, policy, {
        ("GET", f"/dagRuns/{RUN.replace(':', '%3A')}/taskInstances"): (200, TASKS),
        ("GET", "/taskInstances/validate_data/logs/1"): (200, FAILED_LOG),
    })
    result = tools.get_failed_tasks("training_pipeline", RUN)
    failure = result["failed_tasks"][0]
    assert failure["task_id"] == "validate_data"
    assert failure["errors"][0]["message"] == "data validation failed: missing target column 'target'"
    assert failure["errors"][0]["location"] == "dags/training_pipeline.py:69 in validate_data"
    assert result["blocked_downstream"] == ["preprocess"]


def test_expired_token_is_refreshed_once(settings, policy):
    tools, session = tools_with(settings, policy, {("GET", "/dags"): (200, {"dags": [], "total_entries": 0})},
                                expire_first_token=True)
    assert tools.list_dags()["dags"] == []
    assert session.tokens == 2


def test_unreachable_airflow_is_unavailable(settings, policy):
    class DownSession(FakeSession):
        def post(self, *a, **k):
            raise requests.ConnectionError("refused")

    tools = AirflowTools(settings, policy, session=DownSession({}))
    with pytest.raises(ServiceUnavailable):
        tools.list_dags()


def test_trigger_unpauses_and_starts_an_approved_dag(settings, policy):
    tools, session = tools_with(settings, policy, {
        ("GET", "/dags/training_pipeline"): (200, {"dag_id": "training_pipeline", "is_paused": True}),
        ("PATCH", "/dags/training_pipeline"): (200, {"is_paused": False}),
        ("POST", "/dags/training_pipeline/dagRuns"): (200, {"dag_run_id": RUN, "state": "queued", "conf": {}}),
    })
    result = tools.trigger_dag("training_pipeline", {"model_type": "logreg"})
    assert result["triggered"] and result["unpaused"] and result["state"] == "queued"
    assert [c[0] for c in session.calls] == ["GET", "PATCH", "POST"]

"""Apache Airflow 3 workflow tools (REST API v2 with JWT authentication)."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

import requests

from agent.config import Settings
from agent.errors import InvalidRequest, ServiceUnavailable, ToolError
from agent.safety import SafetyPolicy
from agent.schemas import ToolSpec, integer, obj, string

TERMINAL_STATES = {"success", "failed"}
LOG_TAIL = 25


class AirflowTools:
    def __init__(self, settings: Settings, policy: SafetyPolicy, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.policy = policy
        self.base = settings.airflow_base_url
        self.session = session or requests.Session()
        self._token: str | None = None

    # ------------------------------------------------------------------ http
    def _authenticate(self) -> str:
        if not self.settings.airflow_password:
            raise ServiceUnavailable("AIRFLOW_PASSWORD is not set; cannot authenticate to the Airflow API")
        try:
            resp = self.session.post(f"{self.base}/auth/token", timeout=10,
                                     json={"username": self.settings.airflow_username,
                                           "password": self.settings.airflow_password})
        except requests.RequestException as exc:
            raise ServiceUnavailable(f"Airflow at {self.base} is unreachable: {type(exc).__name__}") from exc
        if resp.status_code in (401, 403):
            raise ServiceUnavailable("Airflow rejected the credentials (check AIRFLOW_USERNAME / AIRFLOW_PASSWORD)")
        if not resp.ok:
            raise ServiceUnavailable(f"Airflow auth endpoint returned HTTP {resp.status_code}")
        token = resp.json().get("access_token")
        if not token:
            raise ServiceUnavailable("Airflow auth response did not contain an access token")
        return token

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._token is None:
            self._token = self._authenticate()
        for attempt in (1, 2):
            try:
                resp = self.session.request(method, f"{self.base}/api/v2{path}", timeout=15,
                                            headers={"Authorization": f"Bearer {self._token}",
                                                     "Accept": "application/json"}, **kwargs)
            except requests.Timeout as exc:
                raise ServiceUnavailable(f"Airflow API timed out on {path}") from exc
            except requests.RequestException as exc:
                raise ServiceUnavailable(f"Airflow at {self.base} is unreachable: {type(exc).__name__}") from exc
            if resp.status_code == 401 and attempt == 1:  # token expired: re-authenticate once
                self._token = self._authenticate()
                continue
            break
        if resp.status_code == 404:
            raise InvalidRequest(f"not found: {path}")
        if resp.status_code in (400, 409, 422):
            raise InvalidRequest(f"Airflow rejected the request ({resp.status_code}): {resp.text[:300]}")
        if resp.status_code >= 500:
            raise ServiceUnavailable(f"Airflow API error {resp.status_code} on {path}")
        if not resp.ok:
            raise ToolError(f"Airflow API returned {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else {}

    @staticmethod
    def _run_summary(r: dict[str, Any]) -> dict[str, Any]:
        return {"dag_run_id": r.get("dag_run_id"), "state": r.get("state"), "run_type": r.get("run_type"),
                "start_date": r.get("start_date"), "end_date": r.get("end_date"), "conf": r.get("conf")}

    def _latest_run_id(self, dag_id: str) -> str:
        runs = self._request("GET", f"/dags/{quote(dag_id)}/dagRuns",
                             params={"order_by": "-start_date", "limit": 1}).get("dag_runs", [])
        if not runs:
            raise InvalidRequest(f"DAG {dag_id!r} has no runs yet")
        return runs[0]["dag_run_id"]

    # ------------------------------------------------------------------ tools
    def list_dags(self) -> dict[str, Any]:
        data = self._request("GET", "/dags", params={"limit": 100})
        return {"dags": [{"dag_id": d["dag_id"], "is_paused": d.get("is_paused"), "tags": [t.get("name") for t in
                          d.get("tags") or []], "description": d.get("description"),
                          "approved_for_trigger": d["dag_id"] in self.settings.approved_dags}
                         for d in data.get("dags", [])],
                "total": data.get("total_entries")}

    def get_dag_runs(self, dag_id: str, limit: int = 5) -> dict[str, Any]:
        data = self._request("GET", f"/dags/{quote(dag_id)}/dagRuns",
                             params={"order_by": "-start_date", "limit": max(1, min(int(limit), 25))})
        runs = [self._run_summary(r) for r in data.get("dag_runs", [])]
        return {"dag_id": dag_id, "runs": runs,
                "state_counts": {s: sum(1 for r in runs if r["state"] == s) for s in {r["state"] for r in runs}}}

    def trigger_dag(self, dag_id: str, conf: dict[str, Any] | None = None) -> dict[str, Any]:
        conf = self.policy.check_dag_trigger(dag_id, conf)
        dag = self._request("GET", f"/dags/{quote(dag_id)}")
        if dag.get("is_paused"):
            self._request("PATCH", f"/dags/{quote(dag_id)}", json={"is_paused": False})
        run = self._request("POST", f"/dags/{quote(dag_id)}/dagRuns", json={"logical_date": None, "conf": conf})
        return {"triggered": True, "dag_id": dag_id, "unpaused": bool(dag.get("is_paused")),
                **self._run_summary(run)}

    def wait_for_dag_run(self, dag_id: str, dag_run_id: str, timeout_s: int = 300) -> dict[str, Any]:
        deadline = time.monotonic() + max(10, min(int(timeout_s), 600))
        state = None
        while time.monotonic() < deadline:
            run = self._request("GET", f"/dags/{quote(dag_id)}/dagRuns/{quote(dag_run_id, safe='')}")
            state = run.get("state")
            if state in TERMINAL_STATES:
                tasks = self.get_task_instances(dag_id, dag_run_id)["tasks"]
                return {"dag_id": dag_id, "dag_run_id": dag_run_id, "state": state, "finished": True, "tasks": tasks}
            time.sleep(5)
        return {"dag_id": dag_id, "dag_run_id": dag_run_id, "state": state, "finished": False,
                "note": f"still {state} after {timeout_s}s; check again later"}

    def get_task_instances(self, dag_id: str, dag_run_id: str | None = None) -> dict[str, Any]:
        run_id = dag_run_id or self._latest_run_id(dag_id)
        data = self._request("GET", f"/dags/{quote(dag_id)}/dagRuns/{quote(run_id, safe='')}/taskInstances")
        tasks = sorted(data.get("task_instances", []), key=lambda t: (t.get("start_date") or "9", t["task_id"]))
        return {"dag_id": dag_id, "dag_run_id": run_id,
                "tasks": [{"task_id": t["task_id"], "state": t.get("state"), "try_number": t.get("try_number"),
                           "duration_s": round(t["duration"], 2) if t.get("duration") is not None else None,
                           "start_date": t.get("start_date"), "end_date": t.get("end_date")} for t in tasks]}

    def get_task_logs(self, dag_id: str, dag_run_id: str, task_id: str, try_number: int | None = None) -> dict:
        if try_number is None:
            tis = self.get_task_instances(dag_id, dag_run_id)["tasks"]
            match = [t for t in tis if t["task_id"] == task_id]
            if not match:
                raise InvalidRequest(f"task {task_id!r} not in run {dag_run_id}")
            try_number = max(int(match[0]["try_number"] or 1), 1)
        data = self._request("GET", f"/dags/{quote(dag_id)}/dagRuns/{quote(dag_run_id, safe='')}/taskInstances/"
                                    f"{quote(task_id)}/logs/{int(try_number)}", params={"full_content": "true"})
        return {"dag_id": dag_id, "dag_run_id": dag_run_id, "task_id": task_id, "try_number": try_number,
                **_condense_log(data.get("content"))}

    def get_failed_tasks(self, dag_id: str, dag_run_id: str | None = None) -> dict[str, Any]:
        """Failed tasks in a run (default: latest run) with the error extracted from their logs."""
        run_id = dag_run_id or self._latest_run_id(dag_id)
        tasks = self.get_task_instances(dag_id, run_id)["tasks"]
        failed = [t for t in tasks if t["state"] == "failed"]
        blocked = [t["task_id"] for t in tasks if t["state"] in ("upstream_failed", "skipped")]
        diagnostics = []
        for t in failed:
            try:
                logs = self.get_task_logs(dag_id, run_id, t["task_id"], t["try_number"])
                diagnostics.append({"task_id": t["task_id"], "try_number": t["try_number"],
                                    "errors": logs["errors"], "log_tail": logs["log_tail"][-8:]})
            except ToolError as exc:
                diagnostics.append({"task_id": t["task_id"], "logs_unavailable": str(exc)})
        return {"dag_id": dag_id, "dag_run_id": run_id, "failed_tasks": diagnostics,
                "blocked_downstream": blocked, "all_tasks": tasks}

    # ------------------------------------------------------------------ registry
    def specs(self) -> list[ToolSpec]:
        dag = {"dag_id": string("DAG id, e.g. training_pipeline")}
        run = {"dag_run_id": string("DAG run id (default: the latest run)")}
        return [
            ToolSpec("airflow_list_dags", "airflow", "List Airflow DAGs, whether they are paused and whether the "
                     "agent may trigger them.", obj(), self.list_dags),
            ToolSpec("airflow_get_dag_runs", "airflow", "Recent runs of a DAG with state, dates and run parameters.",
                     obj({**dag, "limit": integer("Number of runs (default 5)")}, ["dag_id"]), self.get_dag_runs),
            ToolSpec("airflow_trigger_dag", "airflow",
                     "Trigger an approved DAG (state-changing: requires explicit user confirmation). Only call it "
                     "when the user explicitly asked to run the pipeline. conf keys: model_type, C, n_estimators, "
                     "min_f1, inject_failure.",
                     obj({**dag, "conf": {"type": "object", "description": "Run parameters"}}, ["dag_id"]),
                     self.trigger_dag, mutating=True),
            ToolSpec("airflow_wait_for_dag_run", "airflow",
                     "Wait until a DAG run finishes (success/failed) and return final task states.",
                     obj({**dag, "dag_run_id": string("DAG run id"),
                          "timeout_s": integer("Maximum wait in seconds (default 300, max 600)")},
                         ["dag_id", "dag_run_id"]), self.wait_for_dag_run, timeout_s=620),
            ToolSpec("airflow_get_task_instances", "airflow", "Task states, tries and durations for a DAG run.",
                     obj({**dag, **run}, ["dag_id"]), self.get_task_instances),
            ToolSpec("airflow_get_failed_tasks", "airflow",
                     "Failed tasks of a DAG run with the exception and log lines extracted from their logs, plus "
                     "the downstream tasks that were blocked.", obj({**dag, **run}, ["dag_id"]),
                     self.get_failed_tasks),
            ToolSpec("airflow_get_task_logs", "airflow", "Condensed log of one task try: errors and the last lines.",
                     obj({**dag, "dag_run_id": string("DAG run id"), "task_id": string("Task id"),
                          "try_number": integer("Try number (default: latest)")},
                         ["dag_id", "dag_run_id", "task_id"]), self.get_task_logs),
        ]


def _condense_log(content: Any) -> dict[str, Any]:
    """Airflow 3 returns structured log events; keep errors and the tail."""
    if isinstance(content, str):
        lines = content.splitlines()
        errors = [ln for ln in lines if "ERROR" in ln or "Traceback" in ln or "Exception" in ln][-10:]
        return {"errors": errors, "log_tail": lines[-LOG_TAIL:]}
    events = content if isinstance(content, list) else []
    errors, tail = [], []
    for e in events:
        if not isinstance(e, dict):
            continue
        text = str(e.get("event", ""))
        if text.startswith("::") or not text:
            continue
        level = str(e.get("level", "")).lower()
        tail.append(f"[{level or 'log'}] {text}"[:400])
        for detail in e.get("error_detail") or []:
            errors.append({"exception": detail.get("exc_type"), "message": str(detail.get("exc_value"))[:500],
                           "location": _last_project_frame(detail.get("frames") or [])})
        if level in ("error", "critical") and not e.get("error_detail"):
            errors.append({"message": text[:500]})
    return {"errors": errors[-10:], "log_tail": tail[-LOG_TAIL:]}


def _last_project_frame(frames: list[dict]) -> str | None:
    """Most relevant frame: the last one inside the project (dags/ or training/)."""
    for f in reversed(frames):
        name = str(f.get("filename", ""))
        if "/dags/" in name or "/training/" in name:
            return f"{name.split('/opt/project/')[-1]}:{f.get('lineno')} in {f.get('name')}"
    return f"{frames[-1].get('filename')}:{frames[-1].get('lineno')}" if frames else None

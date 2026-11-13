"""Agent behaviour: tool selection, multi-tool requests, failures, timeouts, grounding, logging."""

from __future__ import annotations

import json
import time

from agent.errors import ServiceUnavailable
from agent.orchestrator import Orchestrator
from agent.schemas import ToolSpec, obj, string
from tests.conftest import ScriptedLLM


def make_tools(**funcs) -> dict[str, ToolSpec]:
    specs = {}
    for name, (func, mutating) in funcs.items():
        module = name.split("_")[0].replace("k8s", "kubernetes")
        specs[name] = ToolSpec(name, module, f"test tool {name}",
                               obj({"run_id": string("run"), "namespace": string("ns"),
                                    "deployment": string("deployment")}), func, mutating=mutating)
    return specs


def best_run(**_):
    return {"best": {"run_id": "b40fb22a6e9c4a6c8f73fa0a88d590b8", "metrics": {"f1": 0.9861}},
            "tags": {"dataset.dvc_md5": "597f07978de0eb402b7f22d9f256e744"}}


def dataset_version(**_):
    return {"versions": [{"commit": "8cd0a69", "tags": ["data-v2"], "md5": "597f07978de0eb402b7f22d9f256e744"}]}


def test_selects_and_executes_the_requested_tool(settings, policy, logger):
    llm = ScriptedLLM([[("mlflow_find_best_run", {})],
                       "**Facts**\n- Best run b40fb22a has f1 0.9861 [mlflow_find_best_run]"])
    agent = Orchestrator(llm, make_tools(mlflow_find_best_run=(best_run, False)), settings, policy, logger)
    resp = agent.ask("Which run is best by F1?")
    assert [r.tool for r in resp.tool_results] == ["mlflow_find_best_run"]
    assert resp.tool_results[0].status == "ok"
    assert resp.unsupported_values == []
    # the tool result was sent back to the model as a structured envelope
    tool_msg = [m for m in llm.requests[-1] if m["role"] == "tool"][0]
    assert json.loads(tool_msg["content"])["status"] == "ok"


def test_multi_tool_request_collects_evidence_from_each_system(settings, policy, logger):
    llm = ScriptedLLM([[("mlflow_find_best_run", {})], [("dvc_find_version_by_hash", {})],
                       [("k8s_find_unhealthy_pods", {"namespace": "mlops-demo"})],
                       "Run b40fb22a used data-v2 (commit 8cd0a69); 2 pods healthy."])
    tools = make_tools(mlflow_find_best_run=(best_run, False), dvc_find_version_by_hash=(dataset_version, False),
                       k8s_find_unhealthy_pods=(lambda **_: {"healthy": True, "unhealthy_count": 0}, False))
    resp = Orchestrator(llm, tools, settings, policy, logger).ask("best model, its dataset, deployment health?")
    assert [r.tool for r in resp.tool_results] == ["mlflow_find_best_run", "dvc_find_version_by_hash",
                                                   "k8s_find_unhealthy_pods"]
    assert all(r.ok for r in resp.tool_results)
    assert resp.unsupported_values == []


def test_unavailable_service_is_reported_not_invented(settings, policy, logger):
    def down(**_):
        raise ServiceUnavailable("MLflow server at http://localhost:5000 is unreachable")

    # The scripted model first tries to make up metrics, then corrects itself.
    llm = ScriptedLLM([[("mlflow_find_best_run", {})],
                       "The best model has F1 0.9734 and accuracy 97.1%.",
                       "MLflow is unreachable, so the best model and its metrics are unknown."])
    resp = Orchestrator(llm, make_tools(mlflow_find_best_run=(down, False)), settings, policy, logger).ask("best?")
    assert resp.tool_results[0].status == "unavailable"
    assert resp.tool_results[0].data is None  # no operational data was produced
    assert "unreachable" in resp.answer and "0.9734" not in resp.answer
    revision = llm.requests[-1][-1]["content"]
    assert "0.9734" in revision and "97.1%" in revision  # the fabricated values were caught


def test_fabricated_values_are_flagged_when_model_does_not_correct(settings, policy, logger):
    def down(**_):
        raise ServiceUnavailable("MLflow unreachable")

    llm = ScriptedLLM([[("mlflow_find_best_run", {})], "F1 is 0.9734.", "F1 is still 0.9734."])
    resp = Orchestrator(llm, make_tools(mlflow_find_best_run=(down, False)), settings, policy, logger).ask("best?")
    assert resp.unsupported_values == ["0.9734"]
    assert "unverified" in resp.answer


def test_tool_timeout_is_reported(settings, policy, logger):
    def slow(**_):
        time.sleep(5)

    llm = ScriptedLLM([[("airflow_get_dag_runs", {})], "Airflow did not respond in time."])
    resp = Orchestrator(llm, make_tools(airflow_get_dag_runs=(slow, False)), settings, policy, logger).ask("runs?")
    assert resp.tool_results[0].status == "timeout"
    assert "did not finish within 2" in resp.tool_results[0].error


def test_invalid_requests_are_rejected_without_crashing(settings, policy, logger):
    llm = ScriptedLLM([[("does_not_exist", {}), ("mlflow_find_best_run", {"bogus": 1}),
                        ("mlflow_find_best_run", {"__invalid_arguments__": "{oops"})], "Could not answer."])
    resp = Orchestrator(llm, make_tools(mlflow_find_best_run=(best_run, False)), settings, policy, logger).ask("?")
    assert [r.status for r in resp.tool_results] == ["invalid_request"] * 3
    assert "unknown tool" in resp.tool_results[0].error
    assert "unknown arguments" in resp.tool_results[1].error


def test_unexpected_tool_crash_becomes_error_result(settings, policy, logger):
    def boom(**_):
        raise RuntimeError("database locked")

    llm = ScriptedLLM([[("mlflow_find_best_run", {})], "MLflow returned an error."])
    resp = Orchestrator(llm, make_tools(mlflow_find_best_run=(boom, False)), settings, policy, logger).ask("?")
    assert resp.tool_results[0].status == "error"
    assert "database locked" in resp.tool_results[0].error


def test_structured_logs_have_required_fields_and_no_secrets(settings, policy, logger):
    llm = ScriptedLLM([[("mlflow_find_best_run", {"run_id": "abc", "namespace": "token=supersecret123"})], "ok"])
    tools = make_tools(mlflow_find_best_run=(best_run, False))
    tools["mlflow_find_best_run"].parameters["properties"]["password"] = string("x")
    agent = Orchestrator(llm, tools, settings, policy, logger)
    resp = agent.ask("check")
    records = [json.loads(line) for line in settings.log_file.read_text().splitlines()]
    calls = [r for r in records if r["event"] == "tool_call"]
    assert calls and {"tool", "status", "duration_ms", "request_id", "ts"} <= set(calls[0])
    assert calls[0]["request_id"] == resp.request_id
    assert "supersecret123" not in settings.log_file.read_text()


def test_mutating_tool_runs_only_after_confirmation(settings, policy, logger):
    calls = []
    tools = make_tools(k8s_restart_deployment=(lambda **kw: calls.append(kw) or {"restarted": True}, True))
    asked = []
    agent = Orchestrator(ScriptedLLM([[("k8s_restart_deployment", {"deployment": "inference-api",
                                                                    "namespace": "mlops-demo"})], "Restarted."]),
                         tools, settings, policy, logger, confirm=lambda action: asked.append(action) or True)
    resp = agent.ask("Restart the inference API")
    assert resp.tool_results[0].status == "ok" and calls
    assert "k8s_restart_deployment" in asked[0]

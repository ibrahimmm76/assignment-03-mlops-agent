"""Kubernetes diagnostics against fake API objects (no cluster needed)."""

from __future__ import annotations

import pytest

from agent.errors import ServiceUnavailable
from agent.tools.kubernetes_tools import KubernetesTools
from tests.conftest import FakeApps, FakeCore, container_status, event, pod


def test_healthy_deployment(settings, policy):
    core = FakeCore(pods=[pod("api-1"), pod("api-2")])
    result = KubernetesTools(settings, policy, core=core, apps=FakeApps()).find_unhealthy_pods()
    assert result["healthy"] is True and result["total_pods"] == 2


def test_diagnoses_not_ready_and_crashlooping_pods(settings, policy):
    core = FakeCore(
        pods=[pod("api-1", statuses=[container_status(ready=False)]),
              pod("api-2", statuses=[container_status(ready=False, restarts=4, waiting="CrashLoopBackOff",
                                                      message="back-off restarting", last_reason="Error")])],
        events=[event("Unhealthy", "Readiness probe failed: HTTP probe failed with statuscode: 503")],
        ready_endpoints=0, logs='{"level":"ERROR","msg":"model file not found at /app/models/missing.joblib"}')
    tools = KubernetesTools(settings, policy, core=core, apps=FakeApps())
    result = tools.find_unhealthy_pods()
    assert result["unhealthy_count"] == 2
    problems = " ".join(p for u in result["unhealthy_pods"] for p in u["problems"])
    assert "readiness probe" in problems and "CrashLoopBackOff" in problems and "restarted 4x" in problems
    assert "Readiness probe failed" in result["recent_warning_events"][0]["message"]
    services = tools.list_services()["services"]
    assert services[0]["ready_endpoints"] == 0 and services[0]["serving"] is False
    logs = tools.get_pod_logs("api-1")
    assert "model file not found" in logs["error_lines"][0]


def test_reports_deployed_version(settings, policy):
    version = KubernetesTools(settings, policy, core=FakeCore(), apps=FakeApps()).get_deployed_version()
    assert version["images"] == ["inference-api:1.0.0"]
    assert version["app_version"] == "1.0.0" and version["model_version"] == "5"
    assert version["config_maps"]["inference-api-config"]["APP_VERSION"] == "1.0.0"


def test_unreachable_cluster_is_unavailable(settings, policy):
    class DownCore(FakeCore):
        def list_namespaced_pod(self, ns, label_selector=""):
            raise ConnectionError("MaxRetryError: Connection refused")

    with pytest.raises(ServiceUnavailable):
        KubernetesTools(settings, policy, core=DownCore(), apps=FakeApps()).list_pods()


def test_pod_logs_keep_startup_errors_behind_probe_noise(settings, policy):
    noise = "\n".join('INFO:     10.1.0.1:1 - "GET /health/ready HTTP/1.1" 503 Service Unavailable' for _ in range(300))
    core = FakeCore(logs='ERROR model file not found at /app/models/model-v2.joblib\n' + noise)
    tools = KubernetesTools(settings, policy, core=core, apps=FakeApps())
    logs = tools.get_pod_logs("api-1")
    assert logs["error_lines"] == ["ERROR model file not found at /app/models/model-v2.joblib"]
    assert logs["probe_requests"] == {"/health/ready 503": 300}

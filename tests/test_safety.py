"""Unauthorised changes are rejected: wrong namespace, no confirmation, unapproved DAGs."""

from __future__ import annotations

import pytest

from agent.errors import PolicyDenied
from agent.orchestrator import Orchestrator
from agent.tools.kubernetes_tools import KubernetesTools
from tests.conftest import FakeApps, FakeCore, ScriptedLLM


def k8s_agent(settings, policy, logger, confirm, apps):
    tools = {s.name: s for s in KubernetesTools(settings, policy, core=FakeCore(), apps=apps).specs()}
    return tools


def run(settings, policy, logger, tool, args, confirm):
    apps = FakeApps()
    tools = k8s_agent(settings, policy, logger, confirm, apps)
    llm = ScriptedLLM([[(tool, args)], "done"])
    resp = Orchestrator(llm, tools, settings, policy, logger, confirm=confirm).ask("do it")
    return resp.tool_results[0], apps


def test_restart_outside_approved_namespace_is_rejected(settings, policy, logger):
    asked = []
    result, apps = run(settings, policy, logger, "k8s_restart_deployment",
                       {"deployment": "coredns", "namespace": "kube-system"},
                       confirm=lambda a: asked.append(a) or True)
    assert result.status == "denied"
    assert "not approved" in result.error
    assert apps.patches == []          # nothing was changed
    assert asked == []                 # the user was not even asked to approve it


def test_restart_without_confirmation_is_rejected(settings, policy, logger):
    result, apps = run(settings, policy, logger, "k8s_restart_deployment",
                       {"deployment": "inference-api", "namespace": "mlops-demo"}, confirm=lambda a: False)
    assert result.status == "denied"
    assert "did not confirm" in result.error
    assert apps.patches == []


def test_scale_with_confirmation_in_approved_namespace_is_applied(settings, policy, logger):
    result, apps = run(settings, policy, logger, "k8s_scale_deployment",
                       {"deployment": "inference-api", "replicas": 3, "namespace": "mlops-demo"},
                       confirm=lambda a: True)
    assert result.status == "ok"
    assert apps.patches == [{"name": "inference-api", "namespace": "mlops-demo",
                             "scale": {"spec": {"replicas": 3}}}]


def test_read_tools_are_also_limited_to_approved_namespaces(settings, policy):
    tools = KubernetesTools(settings, policy, core=FakeCore(), apps=FakeApps())
    with pytest.raises(PolicyDenied):
        tools.list_pods(namespace="kube-system")


def test_only_approved_dags_and_parameters_can_be_triggered(policy):
    with pytest.raises(PolicyDenied):
        policy.check_dag_trigger("delete_everything", {})
    with pytest.raises(PolicyDenied):
        policy.check_dag_trigger("training_pipeline", {"bash_command": "rm -rf /"})
    assert policy.check_dag_trigger("training_pipeline", {"min_f1": 0.95}) == {"min_f1": 0.95}


def test_non_interactive_default_denies_every_change(settings, policy, logger):
    apps = FakeApps()
    tools = k8s_agent(settings, policy, logger, None, apps)
    llm = ScriptedLLM([[("k8s_restart_deployment", {"deployment": "inference-api"})], "done"])
    resp = Orchestrator(llm, tools, settings, policy, logger).ask("restart")  # default confirm = deny_all
    assert resp.tool_results[0].status == "denied" and apps.patches == []

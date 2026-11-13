"""Tool registry: builds the tools of all four modules."""

from __future__ import annotations

from agent.config import Settings
from agent.safety import SafetyPolicy
from agent.schemas import ToolSpec
from agent.tools.airflow_tools import AirflowTools
from agent.tools.dvc_tools import DvcTools
from agent.tools.kubernetes_tools import KubernetesTools
from agent.tools.mlflow_tools import MlflowTools


def build_registry(settings: Settings, policy: SafetyPolicy) -> dict[str, ToolSpec]:
    specs = [
        *DvcTools(settings).specs(),
        *MlflowTools(settings).specs(),
        *AirflowTools(settings, policy).specs(),
        *KubernetesTools(settings, policy).specs(),
    ]
    return {s.name: s for s in specs}

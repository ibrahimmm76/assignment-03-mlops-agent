"""Safety policy, enforced in code independently of the language model.

* Kubernetes: every operation must target an allow-listed namespace.
* Airflow: only allow-listed DAGs can be triggered, with validated run parameters.
* Any tool that changes state (``ToolSpec.mutating``) must be approved by the person
  running the agent before it executes. Without an interactive confirmation it is denied.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent.config import Settings
from agent.errors import PolicyDenied

ConfirmFn = Callable[[str], bool]

ALLOWED_DAG_CONF_KEYS = {"model_type", "C", "n_estimators", "min_f1", "inject_failure"}


def deny_all(_: str) -> bool:
    """Default confirmation callback for non-interactive use: nothing is approved."""
    return False


class SafetyPolicy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def check_namespace(self, namespace: str | None) -> str:
        ns = namespace or self.settings.k8s_default_namespace
        if ns not in self.settings.k8s_allowed_namespaces:
            raise PolicyDenied(
                f"namespace '{ns}' is not approved; allowed namespaces: {list(self.settings.k8s_allowed_namespaces)}")
        return ns

    def check_dag_trigger(self, dag_id: str, conf: dict[str, Any] | None) -> dict[str, Any]:
        if dag_id not in self.settings.approved_dags:
            raise PolicyDenied(f"DAG '{dag_id}' is not approved for triggering; approved: "
                               f"{list(self.settings.approved_dags)}")
        conf = dict(conf or {})
        unknown = sorted(set(conf) - ALLOWED_DAG_CONF_KEYS)
        if unknown:
            raise PolicyDenied(f"run parameters {unknown} are not allowed; allowed: {sorted(ALLOWED_DAG_CONF_KEYS)}")
        return conf

    def check_tool(self, tool_name: str, module: str, args: dict[str, Any]) -> None:
        """Pre-checks before asking for confirmation, so the user is never asked to approve
        something the policy would reject anyway."""
        if module == "kubernetes":
            self.check_namespace(args.get("namespace"))
        if tool_name == "airflow_trigger_dag":
            self.check_dag_trigger(args.get("dag_id", ""), args.get("conf"))


def describe_action(tool_name: str, args: dict[str, Any]) -> str:
    """Human-readable summary shown in the confirmation prompt."""
    shown = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return f"{tool_name}({shown})"

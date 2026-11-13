"""Shared fixtures: a scripted language model, fake Kubernetes objects, test settings."""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.config import Settings  # noqa: E402
from agent.llm import LLMResponse, ToolCall  # noqa: E402
from agent.logging_utils import setup_logging  # noqa: E402
from agent.safety import SafetyPolicy  # noqa: E402


class ScriptedLLM:
    """Replays a fixed sequence of turns instead of calling a real model.

    Each turn is a list of (tool_name, arguments) tool calls, or a string (final answer).
    Every request is recorded so tests can inspect what the model was sent.
    """

    provider, model = "scripted", "scripted"

    def __init__(self, turns: list[Any]) -> None:
        self.turns = list(turns)
        self.requests: list[list[dict[str, Any]]] = []
        self._n = 0

    def complete(self, system, messages, tools):  # noqa: ANN001,ANN201
        self.requests.append([dict(m) for m in messages])
        turn = self.turns.pop(0) if self.turns else "done"
        if isinstance(turn, str):
            return LLMResponse(text=turn)
        calls = []
        for name, args in turn:
            self._n += 1
            calls.append(ToolCall(id=f"call_{self._n}", name=name, arguments=args))
        return LLMResponse(text="", tool_calls=calls)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(project_root=tmp_path, airflow_password="pw-for-tests", log_file=tmp_path / "agent.jsonl",
                    tool_timeout_s=2, max_steps=10, dvc_reproduce_dir=tmp_path / "reproductions")


@pytest.fixture
def policy(settings: Settings) -> SafetyPolicy:
    return SafetyPolicy(settings)


@pytest.fixture
def logger(settings: Settings) -> logging.Logger:
    return setup_logging(settings.log_file)


# ---------------------------------------------------------------- fake Kubernetes objects
def _now(minutes_ago: int = 5) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes_ago)


def container_status(name="api", ready=True, restarts=0, waiting=None, message=None, last_reason=None):
    state = NS(running=NS() if waiting is None and ready else None,
               waiting=NS(reason=waiting, message=message) if waiting else None, terminated=None)
    if waiting is None and not ready:
        state = NS(running=NS(), waiting=None, terminated=None)
    last = NS(terminated=NS(reason=last_reason, exit_code=1) if last_reason else None)
    return NS(name=name, image="inference-api:1.0.0", ready=ready, restart_count=restarts, state=state,
              last_state=last)


def pod(name, phase="Running", statuses=None):
    return NS(metadata=NS(name=name, labels={"app": "inference-api"}, creation_timestamp=_now()),
              status=NS(phase=phase, container_statuses=statuses or [container_status()]),
              spec=NS(node_name="docker-desktop"))


def event(reason, message, kind="Pod", name="inference-api-abc", type_="Warning"):
    return NS(type=type_, reason=reason, message=message, count=3, last_timestamp=_now(1), event_time=None,
              involved_object=NS(kind=kind, name=name), metadata=NS(creation_timestamp=_now(1)))


class FakeCore:
    def __init__(self, pods=None, events=None, ready_endpoints=2, logs="INFO started"):
        self.pods, self.events, self.ready_endpoints, self.logs = pods or [], events or [], ready_endpoints, logs

    def list_namespaced_pod(self, ns, label_selector=""):
        return NS(items=self.pods)

    def list_namespaced_event(self, ns):
        return NS(items=self.events)

    def read_namespaced_pod_log(self, name, namespace, tail_lines=50, previous=False):
        return self.logs

    def list_namespaced_service(self, ns):
        return NS(items=[NS(metadata=NS(name="inference-api"),
                            spec=NS(type="ClusterIP", cluster_ip="10.96.0.10", selector={"app": "inference-api"},
                                    ports=[NS(port=80, target_port=8000, protocol="TCP")]))])

    def read_namespaced_endpoints(self, name, namespace):
        addrs = [NS() for _ in range(self.ready_endpoints)]
        return NS(subsets=[NS(addresses=addrs, not_ready_addresses=[NS(), NS()] if not addrs else [])])

    def read_namespaced_config_map(self, name, namespace):
        return NS(data={"APP_VERSION": "1.0.0", "MODEL_PATH": "/app/models/model.joblib"})


class FakeApps:
    def __init__(self):
        self.patches: list[dict] = []

    def list_namespaced_deployment(self, ns):
        return NS(items=[self.read_namespaced_deployment("inference-api", ns)])

    def read_namespaced_deployment(self, name, namespace):
        container = NS(image="inference-api:1.0.0", env_from=[NS(config_map_ref=NS(name="inference-api-config"))])
        return NS(metadata=NS(name=name, labels={"app.kubernetes.io/version": "1.0.0"},
                              annotations={"deployment.kubernetes.io/revision": "3"}),
                  spec=NS(replicas=2, template=NS(metadata=NS(annotations={"mlops/model-version": "5"}),
                                                  spec=NS(containers=[container]))),
                  status=NS(ready_replicas=2, available_replicas=2, updated_replicas=2, conditions=[]))

    def patch_namespaced_deployment(self, name, namespace, body):
        self.patches.append({"name": name, "namespace": namespace, "body": body})

    def patch_namespaced_deployment_scale(self, name, namespace, body):
        self.patches.append({"name": name, "namespace": namespace, "scale": body})

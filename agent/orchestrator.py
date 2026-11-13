"""The agent loop: understand the request, choose tools, execute them, report evidence.

For each user message:

1. the model receives the conversation and the schemas of all tools;
2. it chooses tool calls; each call is validated, checked against the safety policy,
   confirmed by the user when it changes state, and executed with a timeout;
3. every result goes back to the model as a structured ``ToolResult``;
4. when the model answers, the grounding check verifies that operational values in the
   answer appear in tool evidence. If not, the model is asked to correct the answer once,
   and any value still unsupported is flagged to the user.

Every tool call is logged as a structured JSON line with the request id.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import time
import uuid
from typing import Any

from agent.config import Settings
from agent.errors import InvalidRequest, PolicyDenied, ServiceUnavailable, ToolError
from agent.grounding import find_unsupported_values
from agent.llm import LLMClient, LLMError
from agent.logging_utils import log_event, redact
from agent.safety import ConfirmFn, SafetyPolicy, deny_all, describe_action
from agent.schemas import AgentResponse, ToolResult, ToolSpec

SYSTEM_PROMPT = """You are an MLOps operations assistant for a machine-learning team. You answer questions \
about datasets (DVC), experiments and models (MLflow), training pipelines (Apache Airflow) and the deployed \
prediction service (Kubernetes) by calling the tools provided.

Rules:
1. Operational facts (versions, hashes, metrics, run ids, statuses, pod states, log lines) must come from tool \
results in this conversation. Never use general knowledge or guesses for them.
2. If a tool returns status "unavailable", "timeout" or "error", say plainly that the information could not be \
retrieved and why. Do not fill the gap with assumed values.
3. Call several tools when a request needs them. Correlate evidence across systems: e.g. the dataset md5 \
recorded on an MLflow run (tag dataset.dvc_md5) maps to a DVC dataset version through \
dvc_find_version_by_hash.
4. Tools that change state (triggering a DAG, restarting or scaling a deployment) may only be called when the \
user explicitly asked for that action in their latest message. Never trigger retraining on your own initiative; \
recommend it instead. The user will be asked to confirm.
5. A high metric alone does not make a model production-ready. Consider the quality gate, other metrics, the \
dataset version and deployment health.
6. Recommend retraining only with evidence: the freshness policy (mlflow_check_model_freshness), a failed quality \
gate, newer data than the model was trained on, or degradation results the user supplied. Before answering \
whether retraining is necessary, collect evidence from all four systems: MLflow (best run and \
mlflow_check_model_freshness), DVC (dataset version of that run via dvc_find_version_by_hash and the current one \
via dvc_get_current_version), Kubernetes (k8s_get_deployed_version and k8s_find_unhealthy_pods) and Airflow \
(recent training runs via airflow_get_dag_runs).
7. When diagnosing failures, gather evidence first (status, events, logs), then state the root cause and the \
evidence for it.

Final answer format (Markdown):
**Facts**: bullet points, each naming the tool it came from in brackets, e.g. [mlflow_find_best_run].
**Uncertainties**: what could not be verified or is unknown.
**Recommended actions**: concrete next steps (or "None").
Keep it concise."""

REVISION_PROMPT = """Your answer contains values that do not appear in any tool result: {values}. Rewrite the \
answer using only values present in the tool results (call tools again if needed). If a value is unknown, say so."""


class Orchestrator:
    def __init__(self, llm: LLMClient, tools: dict[str, ToolSpec], settings: Settings, policy: SafetyPolicy,
                 logger: logging.Logger | None = None, confirm: ConfirmFn = deny_all) -> None:
        self.llm = llm
        self.tools = tools
        self.settings = settings
        self.policy = policy
        self.logger = logger or logging.getLogger("mlops_agent")
        self.confirm = confirm
        self.messages: list[dict[str, Any]] = []
        self.evidence: list[Any] = []  # tool results and user messages of the whole conversation
        self.conversation_id = uuid.uuid4().hex[:8]
        self._schemas = [t.schema() for t in tools.values()]

    # ------------------------------------------------------------------ public
    def ask(self, question: str) -> AgentResponse:
        request_id = uuid.uuid4().hex[:12]
        log_event(self.logger, "request_received", request_id=request_id, conversation_id=self.conversation_id,
                  question=question[:500])
        self.messages.append({"role": "user", "content": question})
        self.evidence.append(question)
        results: list[ToolResult] = []
        revised = False
        answer = ""
        unsupported: list[str] = []
        step = 0

        for step in range(1, self.settings.max_steps + 1):  # noqa: B007 - step is reported after the loop
            try:
                resp = self.llm.complete(SYSTEM_PROMPT, self.messages, self._schemas)
            except LLMError as exc:
                log_event(self.logger, "llm_error", logging.ERROR, request_id=request_id, error=str(exc)[:300])
                answer = (f"I could not complete this request because the language model is unavailable: {exc}\n\n"
                          + self._evidence_summary(results))
                break
            self.messages.append({"role": "assistant", "content": resp.text or None,
                                  "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments,
                                                  "provider_data": c.provider_data} for c in resp.tool_calls]})
            if resp.tool_calls:
                for call in resp.tool_calls:
                    result = self.execute(call.name, call.arguments, request_id)
                    results.append(result)
                    self.evidence.append(result.to_dict())
                    self.messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                                          "content": result.for_model()})
                continue

            answer = resp.text or ""
            unsupported = find_unsupported_values(answer, self.evidence)
            if unsupported and not revised:
                revised = True
                log_event(self.logger, "grounding_revision", logging.WARNING, request_id=request_id,
                          unsupported=unsupported)
                self.messages.append({"role": "user", "content": REVISION_PROMPT.format(values=unsupported)})
                continue
            break
        else:
            answer = answer or ("I reached the step limit before finishing. " + self._evidence_summary(results))

        if unsupported:
            answer += ("\n\n> **Note:** these values could not be matched to any tool result and should be "
                       f"treated as unverified: {', '.join(unsupported)}")
        log_event(self.logger, "request_completed", request_id=request_id, conversation_id=self.conversation_id,
                  steps=step, tool_calls=len(results), unsupported_values=unsupported)
        return AgentResponse(request_id=request_id, answer=answer, tool_results=results,
                             unsupported_values=unsupported, steps=step)

    # ------------------------------------------------------------------ tool execution
    def execute(self, name: str, args: dict[str, Any], request_id: str) -> ToolResult:
        started = time.monotonic()
        args = args if isinstance(args, dict) else {}
        spec = self.tools.get(name)
        if spec is None:
            result = ToolResult(name, "invalid_request", error=f"unknown tool {name!r}")
        elif "__invalid_arguments__" in args:
            result = ToolResult(name, "invalid_request", error="tool arguments were not valid JSON")
        else:
            result = self._run(spec, args, request_id)
        result.duration_ms = int((time.monotonic() - started) * 1000)
        result.request_id = request_id
        result.arguments = args
        log_event(self.logger, "tool_call", logging.INFO if result.ok else logging.WARNING,
                  request_id=request_id, conversation_id=self.conversation_id, tool=name, status=result.status,
                  duration_ms=result.duration_ms, args=redact(args), error=result.error)
        return result

    def _run(self, spec: ToolSpec, args: dict[str, Any], request_id: str) -> ToolResult:
        allowed = set(spec.parameters.get("properties", {}))
        unknown = sorted(set(args) - allowed)
        missing = [r for r in spec.parameters.get("required", []) if r not in args]
        if missing or unknown:
            return ToolResult(spec.name, "invalid_request",
                              error=f"missing arguments {missing}" if missing else f"unknown arguments {unknown}")
        try:
            if spec.mutating:
                self.policy.check_tool(spec.name, spec.module, args)
                action = describe_action(spec.name, args)
                approved = self.confirm(action)
                log_event(self.logger, "confirmation", request_id=request_id, tool=spec.name, approved=approved)
                if not approved:
                    raise PolicyDenied(f"the user did not confirm {action}; no change was made")
            data = self._with_timeout(spec, args)
            return ToolResult(spec.name, "ok", data=data)
        except PolicyDenied as exc:
            return ToolResult(spec.name, "denied", error=str(exc))
        except ServiceUnavailable as exc:
            return ToolResult(spec.name, "unavailable", error=str(exc))
        except InvalidRequest as exc:
            return ToolResult(spec.name, "invalid_request", error=str(exc))
        except concurrent.futures.TimeoutError:
            return ToolResult(spec.name, "timeout",
                              error=f"{spec.name} did not finish within {self._timeout(spec)}s")
        except (ToolError, TypeError, ValueError) as exc:
            return ToolResult(spec.name, "error", error=str(redact(str(exc)))[:500])
        except Exception as exc:  # a bug in one tool must not crash the conversation
            self.logger.exception("tool crashed", extra={"fields": {"tool": spec.name, "request_id": request_id}})
            return ToolResult(spec.name, "error", error=f"{type(exc).__name__}: {str(redact(str(exc)))[:300]}")

    def _timeout(self, spec: ToolSpec) -> float:
        return spec.timeout_s or self.settings.tool_timeout_s

    def _with_timeout(self, spec: ToolSpec, args: dict[str, Any]) -> Any:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            return pool.submit(spec.func, **args).result(timeout=self._timeout(spec))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _evidence_summary(results: list[ToolResult]) -> str:
        if not results:
            return "No tool results were collected."
        rows = [f"- {r.tool}: {r.status}" + (f" ({r.error})" if r.error else "") for r in results]
        return "Tool calls made:\n" + "\n".join(rows)

    def transcript(self) -> str:
        """JSON transcript of the conversation (messages and tool evidence), secrets redacted."""
        return json.dumps(redact({"conversation_id": self.conversation_id, "messages": self.messages}),
                          indent=2, default=str)

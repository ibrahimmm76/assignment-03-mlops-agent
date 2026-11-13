"""Command-line interface for the MLOps agent.

    python -m agent.main                         interactive chat
    python -m agent.main --ask "question"        one question, then exit
    python -m agent.main --list-tools            show the tools and which ones change state
    python -m agent.main --ask "..." --evidence evidence/scenario1.md
                                                 also save the answer with full tool evidence

State-changing tools always ask for confirmation in the terminal. When the agent is not
running in an interactive terminal, every state change is refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from agent.config import PROJECT_ROOT, Settings
from agent.llm import LLMError, create_client
from agent.logging_utils import redact, setup_logging
from agent.orchestrator import Orchestrator
from agent.safety import SafetyPolicy, deny_all
from agent.schemas import AgentResponse
from agent.tools import build_registry


def terminal_confirm(action: str) -> bool:
    print(f"\n  The agent wants to run a state-changing operation:\n    {action}", file=sys.stderr)
    try:
        reply = input("  Allow it? [y/N] ").strip().lower()
    except EOFError:
        return False
    return reply in ("y", "yes")


def build_agent(verbose: bool = False, interactive: bool = True) -> Orchestrator:
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env")
    except ImportError:
        pass
    settings = Settings.from_env()
    logger = setup_logging(settings.log_file, verbose=verbose)
    policy = SafetyPolicy(settings)
    tools = build_registry(settings, policy)
    llm = create_client()
    confirm = terminal_confirm if interactive and sys.stdin.isatty() else deny_all
    return Orchestrator(llm, tools, settings, policy, logger=logger, confirm=confirm)


def write_evidence(path: Path, question: str, response: AgentResponse, model: str) -> None:
    """Markdown record of one conversation turn with the raw tool evidence."""
    lines = [f"# Agent conversation: {question}", "",
             f"- Time: {datetime.now(UTC).isoformat(timespec='seconds')}",
             f"- Request id: `{response.request_id}`", f"- Model: `{model}`",
             f"- Tool calls: {len(response.tool_results)}", "", "## Question", "", question, "",
             "## Answer", "", response.answer, "", "## Tool evidence", ""]
    for i, r in enumerate(response.tool_results, 1):
        lines += [f"### {i}. `{r.tool}` ({r.status}, {r.duration_ms} ms)", "",
                  f"Arguments: `{json.dumps(redact(r.arguments), default=str)}`", ""]
        body = r.error if r.error else json.dumps(redact(r.data), indent=2, default=str)
        if len(body) > 6000:
            body = body[:6000] + "\n... (truncated)"
        lines += ["```json" if not r.error else "```", body, "```", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def print_response(response: AgentResponse) -> None:
    calls = ", ".join(f"{r.tool}:{r.status}" for r in response.tool_results) or "none"
    print(f"\n{response.answer}\n")
    print(f"[request {response.request_id} | tools: {calls}]", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MLOps agent for DVC, MLflow, Airflow and Kubernetes")
    parser.add_argument("--ask", help="ask one question and exit")
    parser.add_argument("--evidence", type=Path, help="save the answer and tool evidence to this Markdown file")
    parser.add_argument("--transcript", type=Path, help="save the full JSON transcript at exit")
    parser.add_argument("--list-tools", action="store_true", help="list available tools")
    parser.add_argument("-v", "--verbose", action="store_true", help="also print structured logs to stderr")
    args = parser.parse_args(argv)

    if args.list_tools:
        settings = Settings.from_env()
        policy = SafetyPolicy(settings)
        for spec in build_registry(settings, policy).values():
            flag = " [changes state: confirmation required]" if spec.mutating else ""
            print(f"{spec.module:<11} {spec.name:<30} {spec.description.split('. ')[0].rstrip('.')}{flag}")
        return 0

    try:
        agent = build_agent(verbose=args.verbose)
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.ask:
            response = agent.ask(args.ask)
            print_response(response)
            if args.evidence:
                write_evidence(args.evidence, args.ask, response, getattr(agent.llm, "model", "?"))
            return 0

        print("MLOps agent ready. Ask about datasets, experiments, pipelines or the deployment. "
              "Type 'exit' to quit.")
        turn = 0
        while True:
            try:
                question = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if question.lower() in ("exit", "quit"):
                break
            if not question:
                continue
            turn += 1
            response = agent.ask(question)
            print_response(response)
            if args.evidence:
                stem = args.evidence.with_suffix("")
                write_evidence(stem.parent / f"{stem.name}_{turn}.md", question, response,
                               getattr(agent.llm, "model", "?"))
        return 0
    finally:
        if args.transcript:
            args.transcript.parent.mkdir(parents=True, exist_ok=True)
            args.transcript.write_text(agent.transcript(), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())

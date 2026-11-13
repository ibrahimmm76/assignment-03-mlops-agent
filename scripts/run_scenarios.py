"""Run the demonstration scenarios and save each conversation with its tool evidence.

    python scripts/run_scenarios.py                 run all scenarios
    python scripts/run_scenarios.py --only 3        run one scenario
    python scripts/run_scenarios.py --list          show the scenarios

Output goes to evidence/<number>_<name>.md: the question, the agent's answer and the
raw result of every tool call. These files are the conversation evidence for the report.
Scenario 3 triggers the training DAG, so the agent asks you to confirm it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.main import build_agent, print_response, write_evidence  # noqa: E402

SCENARIOS = [
    ("dataset_provenance",
     "Which dataset version was used to train the current champion model? Give the DVC hash, the git commit and "
     "version tag, compare it with the previous dataset version, and show how to reproduce it."),
    ("model_selection",
     "Compare the last five training runs and recommend the best candidate based on F1 score."),
    ("pipeline_execution",
     "Trigger the model training pipeline with model_type logreg and report whether execution succeeded."),
    ("deployment_diagnosis",
     "Why is my prediction API unavailable?"),
    ("multi_tool",
     "Find the best recent model, identify the dataset version used to train it, check the deployment status, "
     "and explain whether retraining is necessary."),
    ("failure_airflow",
     "The latest training pipeline run failed. Find which task failed, the root cause, and what was blocked."),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", type=int, help="scenario number (1-based)")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for i, (name, q) in enumerate(SCENARIOS, 1):
            print(f"{i}. {name}: {q}")
        return 0

    out_dir = ROOT / "evidence"
    selected = [(i, *SCENARIOS[i - 1]) for i in ([args.only] if args.only else range(1, len(SCENARIOS) + 1))]
    for i, name, question in selected:
        print(f"\n=== Scenario {i}: {name}\n{question}")
        agent = build_agent()  # fresh conversation per scenario
        response = agent.ask(question)
        print_response(response)
        path = out_dir / f"{i:02d}_{name}.md"
        write_evidence(path, question, response, getattr(agent.llm, "model", "?"))
        print(f"saved {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Run the full training pipeline from the command line (also used by `dvc repro`).

    python -m training.train --model-type logreg --C 1.0
    python -m training.train --model-type random_forest --n-estimators 300

Stages: validate -> preprocess -> train -> evaluate + log to MLflow -> quality gate -> promote.
A failed validation or quality gate stops the run before anything is promoted.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from training import config, steps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train and register the breast-cancer classifier.")
    parser.add_argument("--model-type", choices=["logreg", "random_forest"], default="logreg")
    parser.add_argument("--C", type=float, default=1.0, help="logreg regularisation strength")
    parser.add_argument("--n-estimators", type=int, default=200, help="random forest size")
    parser.add_argument("--max-depth", type=int, default=None, help="random forest depth")
    parser.add_argument("--min-f1", type=float, default=config.DEFAULT_MIN_F1)
    parser.add_argument("--no-mlflow", action="store_true", help="skip MLflow logging (offline check)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    params = {"C": args.C} if args.model_type == "logreg" else {
        "n_estimators": args.n_estimators, **({"max_depth": args.max_depth} if args.max_depth else {})}
    try:
        validation = steps.validate()
        steps.preprocess()
        train_info = steps.train(args.model_type, params)
        metrics = steps.evaluate()
        config.METRICS_FILE.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
        if args.no_mlflow:
            print(json.dumps({"metrics": metrics}, indent=2))
            return 0
        run = steps.log_to_mlflow(train_info, metrics, validation, source="cli")
        try:
            steps.quality_gate(metrics, min_f1=args.min_f1)
        except steps.QualityGateError as exc:
            steps.mark_gate_failed(run, str(exc))
            raise
        promoted = steps.promote(run)
    except (steps.ValidationError, steps.QualityGateError) as exc:
        logging.error("%s", exc)
        return 1
    print(json.dumps({"run_id": run["run_id"], "metrics": metrics, "promoted": promoted}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

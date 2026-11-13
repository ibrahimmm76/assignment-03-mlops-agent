"""Create a version of the breast-cancer dataset used by this project.

The dataset is versioned with DVC, not git. This script only exists so anyone can
recreate the exact raw file for each version:

    python data/generate_dataset.py --version v1   # 400 rows
    python data/generate_dataset.py --version v2   # all 569 rows

Both versions are deterministic, so the DVC md5 hashes are reproducible.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from sklearn.datasets import load_breast_cancer

OUTPUT = Path(__file__).resolve().parent / "raw" / "breast_cancer.csv"
ROWS = {"v1": 400, "v2": None}


def build(version: str) -> pd.DataFrame:
    bunch = load_breast_cancer(as_frame=True)
    frame = bunch.frame.copy()
    frame.columns = [c.replace(" ", "_") for c in frame.columns]
    limit = ROWS[version]
    return frame if limit is None else frame.iloc[:limit].reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", choices=sorted(ROWS), required=True)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()

    frame = build(args.version)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(args.output, index=False, float_format="%.6g", lineterminator="\n")
    print(f"wrote {len(frame)} rows ({args.version}) to {args.output}")


if __name__ == "__main__":
    main()

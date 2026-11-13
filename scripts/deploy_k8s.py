"""Build the inference image and deploy it to the local Kubernetes cluster.

    python scripts/deploy_k8s.py                          build image + deploy (or update)
    python scripts/deploy_k8s.py --inject missing-model   failure 1: ConfigMap points to a missing model file
    python scripts/deploy_k8s.py --inject bad-image       failure 2: Deployment uses an image that does not exist
    python scripts/deploy_k8s.py --restore                undo an injected failure
    python scripts/deploy_k8s.py --status                 show pods and services

The model version shown by the API and by the agent is read from models/model_metadata.json,
which the training pipeline writes when a model passes the quality gate.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "kubernetes"
NAMESPACE = "mlops-demo"


def run(cmd: list[str], stdin: str | None = None) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, input=stdin, text=True, check=True, cwd=ROOT)


def load(name: str) -> dict:
    return yaml.safe_load((K8S / name).read_text(encoding="utf-8"))


def apply(doc: dict) -> None:
    run(["kubectl", "apply", "-f", "-"], stdin=yaml.safe_dump(doc, sort_keys=False))


def model_version() -> str:
    meta = ROOT / "models" / "model_metadata.json"
    if not meta.exists():
        sys.exit("models/model_metadata.json not found: train a model that passes the quality gate first "
                 "(trigger the Airflow DAG or run `python -m training.train`).")
    return str(json.loads(meta.read_text(encoding="utf-8")).get("model_version") or "unknown")


def manifests(version: str) -> tuple[dict, dict, dict]:
    config = load("configmap.yaml")
    config["data"]["MODEL_VERSION"] = version
    deployment = load("deployment.yaml")
    app_version = config["data"]["APP_VERSION"]
    deployment["spec"]["template"]["metadata"]["annotations"]["mlops/model-version"] = version
    deployment["spec"]["template"]["spec"]["containers"][0]["image"] = f"inference-api:{app_version}"
    return config, deployment, load("service.yaml")


def deploy(build: bool = True) -> None:
    version = model_version()
    config, deployment, service = manifests(version)
    if build:
        run(["docker", "build", "-t", f"inference-api:{config['data']['APP_VERSION']}", "."])
    run(["kubectl", "apply", "-f", str(K8S / "namespace.yaml")])
    apply(config)
    apply(service)
    apply(deployment)
    # A ConfigMap change does not restart pods by itself.
    run(["kubectl", "-n", NAMESPACE, "rollout", "restart", "deployment/inference-api"])
    run(["kubectl", "-n", NAMESPACE, "rollout", "status", "deployment/inference-api", "--timeout=120s"])
    print(f"\nDeployed inference-api with model version {version}.")


def inject(kind: str) -> None:
    config, deployment, _ = manifests(model_version())
    if kind == "missing-model":
        broken = copy.deepcopy(config)
        broken["data"]["MODEL_PATH"] = "/app/models/model-v2.joblib"  # file does not exist in the image
        apply(broken)
        run(["kubectl", "-n", NAMESPACE, "rollout", "restart", "deployment/inference-api"])
    elif kind == "bad-image":
        broken = copy.deepcopy(deployment)
        broken["spec"]["template"]["spec"]["containers"][0]["image"] = "inference-api:9.9.9-missing"
        apply(broken)
    print(f"\nInjected failure '{kind}'. Give it ~30s, then ask the agent: \"Why is my prediction API unavailable?\"")


def status() -> None:
    for kind in ("deployments", "pods", "services", "endpoints"):
        run(["kubectl", "-n", NAMESPACE, "get", kind, "-o", "wide"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--inject", choices=["missing-model", "bad-image"])
    group.add_argument("--restore", action="store_true")
    group.add_argument("--status", action="store_true")
    parser.add_argument("--no-build", action="store_true", help="skip docker build")
    args = parser.parse_args()
    for tool in ("kubectl", "docker"):
        if shutil.which(tool) is None:
            sys.exit(f"'{tool}' not found on PATH. Enable Kubernetes in Docker Desktop (Settings > Kubernetes).")
    if args.status:
        status()
    elif args.inject:
        inject(args.inject)
    else:
        deploy(build=not (args.no_build or args.restore))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# MLOps Operations Agent

A conversational agent for day-to-day ML operations. You ask a question in plain English and the
agent works out which systems to query, calls them through structured tools, and answers using only
what those tools returned:

* **DVC**: which dataset version a model was trained on, how versions differ, how to reproduce one
* **MLflow**: experiments, runs, metrics, the best candidate, the model registry
* **Apache Airflow 3**: the training pipeline, its runs, failed tasks and their root cause
* **Kubernetes**: the deployed prediction API, its pods, services, events and logs

```
you> Find the best recent model, identify the dataset version used to train it,
     check the deployment status, and explain whether retraining is necessary.
```

The agent answers in three parts, **Facts** (each one citing the tool it came from),
**Uncertainties** and **Recommended actions**. Anything that changes state, such as triggering
training or restarting or scaling the deployment, needs your explicit confirmation.

See [architecture.md](architecture.md) for the design and [docs/technical_report.md](docs/technical_report.md)
for the report.

---

## Contents

1. [Prerequisites](#1-prerequisites)
2. [Installation](#2-installation)
3. [Environment configuration](#3-environment-configuration)
4. [Dataset versioning with DVC](#4-dataset-versioning-with-dvc)
5. [Launching MLflow and Airflow](#5-launching-mlflow-and-airflow)
6. [Training a model](#6-training-a-model)
7. [Deploying to Kubernetes](#7-deploying-to-kubernetes)
8. [Talking to the agent](#8-talking-to-the-agent)
9. [Running the tests](#9-running-the-tests)
10. [Failure injection and diagnosis](#10-failure-injection-and-diagnosis)
11. [Demonstration scenarios](#11-demonstration-scenarios)
12. [Repository layout](#12-repository-layout)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Prerequisites

| Tool | Version | Used for |
|---|---|---|
| Python | 3.11, 3.12 or 3.13 | agent, training, tests |
| Git | any recent | code and dataset version history |
| Docker Desktop | with **Kubernetes enabled** | MLflow, Airflow, the API image and the local cluster |
| A Gemini API key | free tier works | the agent's language model ([get one](https://aistudio.google.com/apikey)) |

To enable Kubernetes in Docker Desktop: **Settings → Kubernetes → Enable Kubernetes → Apply & restart**.
Then check that `kubectl config current-context` prints `docker-desktop`.

## 2. Installation

```bash
git clone https://github.com/ibrahimmm76/assignment-03-mlops-agent.git
cd assignment-03-mlops-agent

python -m venv .venv
# Windows (Command Prompt)
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements-dev.txt
```

`requirements.txt` holds the runtime dependencies (the same versions as the Docker images), and
`requirements-dev.txt` adds pytest and ruff.

## 3. Environment configuration

```bash
copy .env.example .env        # macOS / Linux: cp .env.example .env
```

Open `.env` and set at least:

| Variable | Value |
|---|---|
| `GEMINI_API_KEY` | your Gemini key |
| `AIRFLOW_PASSWORD` | any password you choose; Docker Compose uses it for the Airflow `admin` user |

Everything else has working defaults for a local setup:

| Variable | Default | Meaning |
|---|---|---|
| `LLM_MODEL` / `LLM_FALLBACK_MODEL` | `gemini-3.5-flash` / flash-lite models | model and backups if one is overloaded or out of quota |
| `MLFLOW_TRACKING_URI` | `http://localhost:5000` | MLflow server |
| `AIRFLOW_BASE_URL` | `http://localhost:8080` | Airflow API |
| `AIRFLOW_APPROVED_DAGS` | `training_pipeline` | the only DAGs the agent may trigger |
| `K8S_CONTEXT` | `docker-desktop` | kubeconfig context |
| `K8S_ALLOWED_NAMESPACES` | `mlops-demo` | the only namespaces the agent may touch |
| `QUALITY_GATE_MIN_F1` | `0.90` | quality-gate threshold used by the pipeline |
| `RETRAIN_MAX_MODEL_AGE_DAYS` | `30` | freshness policy used for retraining recommendations |

`.env` is git-ignored. No credential is stored in the repository.

## 4. Dataset versioning with DVC

The dataset is the Wisconsin breast-cancer dataset, exported to CSV by `data/generate_dataset.py`.
Two versions exist and are tagged in git:

| Tag | Rows | DVC md5 |
|---|---|---|
| `data-v1` | 400 | `57370680ae532c99e90633796be7dc65` |
| `data-v2` | 569 | `597f07978de0eb402b7f22d9f256e744` |

The CSV itself is not in git. Git stores only `data/raw/breast_cancer.csv.dvc`, which records the
hash. To get the data after cloning:

```bash
# Option A: you have access to the DVC remote
dvc pull

# Option B: regenerate it. The output is byte-identical, so the hash matches the .dvc file
python data/generate_dataset.py --version v2
dvc status data/raw/breast_cancer.csv.dvc   # "Data and pipelines are up to date." = hash matches
```

To set DVC up from scratch on a new machine (a local folder as the remote):

```bash
dvc init
dvc remote add -d localremote ../dvc-storage
python data/generate_dataset.py --version v1
dvc add data/raw/breast_cancer.csv
git add data/raw/breast_cancer.csv.dvc data/raw/.gitignore
git commit -m "Track dataset v1 with DVC"
git tag data-v1
dvc push
```

Repeat with `--version v2` and the tag `data-v2` for the second version.

Reproducing an old version without touching your working copy:

```bash
dvc get . data/raw/breast_cancer.csv --rev data-v1 -o reproductions/breast_cancer_v1.csv
```

`dvc repro` runs the offline training stage defined in `dvc.yaml` and records the result in
`dvc.lock` and `metrics.json`. It is skipped when neither the data nor the training code has changed;
`dvc metrics show` prints the metrics.

## 5. Launching MLflow and Airflow

```bash
docker compose up -d --build
```

The first build takes a few minutes. Then:

| Service | URL | Login |
|---|---|---|
| MLflow | http://localhost:5000 | none |
| Airflow | http://localhost:8080 | `admin` / your `AIRFLOW_PASSWORD` |

Airflow runs in standalone mode with the project folder mounted at `/opt/project`, so the DAG in
`dags/training_pipeline.py` runs the same training code you have locally and reads your
DVC-tracked dataset. Check both services with `docker compose ps`; stop them with `docker compose down`.
MLflow data is kept in the `mlflow-data` Docker volume.

## 6. Training a model

The training DAG `training_pipeline` runs:

```
validate_data → preprocess → train_model → evaluate_and_log → quality_gate → promote_model
```

* Every task retries twice on transient errors.
* `evaluate_and_log` records metrics, parameters, the model and the **dataset provenance**: DVC md5,
  git commit and Airflow run id, stored as MLflow tags.
* `quality_gate` fails the run when F1 or recall is below the threshold. `promote_model` then never
  runs, so a weak model can never reach the deployment.
* `promote_model` sets the `champion` alias in the MLflow registry and exports `models/model.joblib`
  plus `models/model_metadata.json` for the API image.

Trigger it in any of three ways:

* **Airflow UI**: open `training_pipeline` → *Trigger*. Run parameters: `model_type` (`logreg` or
  `random_forest`), `C`, `n_estimators`, `min_f1`, `inject_failure`.
* **The agent**: *"Trigger the training pipeline with model_type logreg and report whether it
  succeeded."* You will be asked to confirm.
* **Command line, without Airflow**: `python -m training.train --model-type logreg`

## 7. Deploying to Kubernetes

After a model has passed the quality gate (so `models/model.joblib` exists):

```bash
python scripts/deploy_k8s.py
```

This builds the image `inference-api:1.0.0` and applies `kubernetes/namespace.yaml`,
`configmap.yaml`, `service.yaml` and `deployment.yaml` to the `mlops-demo` namespace, with the model
version filled in from `models/model_metadata.json`. It then waits for the rollout to finish.

The deployment has 2 replicas, readiness (`/health/ready`) and liveness (`/health/live`) probes,
CPU and memory requests and limits, a ClusterIP Service and a ConfigMap. Check it with:

```bash
python scripts/deploy_k8s.py --status
kubectl -n mlops-demo port-forward svc/inference-api 8000:80
# in another terminal:
curl http://localhost:8000/version
```

`kubernetes/agent-rbac.yaml` defines a least-privilege ServiceAccount for running the agent against a
shared cluster. On Docker Desktop the agent simply uses your kubeconfig.

## 8. Talking to the agent

```bash
python -m agent.main                      # interactive chat
python -m agent.main --ask "Which dataset version was used to train the champion model?"
python -m agent.main --list-tools         # all 31 tools; state-changing ones are marked
```

Useful options:

| Option | Effect |
|---|---|
| `--evidence evidence/my_question.md` | save the answer together with the raw output of every tool call |
| `--transcript logs/chat.json` | save the whole conversation as JSON |
| `-v` | also print the structured logs to the terminal |

Example questions:

* *Which dataset version was used for the champion model, and how does it differ from data-v1?*
* *Compare the last five training runs and recommend the best candidate based on F1 score.*
* *Did the latest training pipeline run succeed? If not, why?*
* *Why is my prediction API unavailable?*
* *What version of the model is currently deployed?*

Every tool call is logged to `logs/agent.jsonl` as one JSON line with the tool name, status,
duration and request id. Secrets are masked.

**Safety rules, enforced in code:**

* Triggering a DAG, restarting a deployment and scaling a deployment all ask for confirmation in the
  terminal. When the agent is not running in an interactive terminal, these operations are refused.
* Only DAGs listed in `AIRFLOW_APPROVED_DAGS` can be triggered, and only with known run parameters.
* Every Kubernetes call, reads included, is limited to `K8S_ALLOWED_NAMESPACES`.
* DVC and git run from a fixed allow-list of read-only commands, with no shell.
* A grounding check compares the numbers and hashes in each answer with the tool results. Anything
  that does not match is sent back for correction, and flagged as unverified if it still doesn't match.

## 9. Running the tests

```bash
pytest
ruff check .
```

The 38 tests need no running services: MLflow, Airflow and Kubernetes are replaced by fakes. The
DVC tests build a real temporary git and DVC repository. The suite covers:

* tool selection and multi-tool execution with a scripted model;
* tool timeouts, invalid requests, unknown tools and service failures;
* **the agent does not invent results when a tool is unavailable**: made-up values are caught and
  flagged (`tests/test_orchestrator.py`);
* **unauthorised Kubernetes changes are rejected**, both outside the approved namespace and without
  confirmation (`tests/test_safety.py`);
* structured logs contain tool, status, duration and request id, and no secrets;
* the DVC, MLflow, Airflow and Kubernetes tools, the training guards (validation, quality gate) and
  the inference API.

GitHub Actions (`.github/workflows/ci.yml`) runs ruff and the tests on every pull request and on
pushes to `main`.

## 10. Failure injection and diagnosis

Four failures can be injected on purpose, two in the pipeline and two in the deployment:

| # | Inject with | What breaks | Ask the agent |
|---|---|---|---|
| 1 | Trigger the DAG with `{"inject_failure": "bad_data"}` | `validate_data` fails (target column missing), all downstream tasks are blocked | *Why did the latest training run fail?* |
| 2 | Trigger the DAG with `{"min_f1": 0.999}` | `quality_gate` fails, `promote_model` is blocked and the deployed model is unchanged | *Why was the new model not promoted?* |
| 3 | `python scripts/deploy_k8s.py --inject missing-model` | pods start but never become ready (model file not found), so the Service has 0 endpoints | *Why is my prediction API unavailable?* |
| 4 | `python scripts/deploy_k8s.py --inject bad-image` | new pods stay in `ImagePullBackOff`, and the rollout stalls | *Why isn't the new version rolling out?* |

Undo 3 and 4 with `python scripts/deploy_k8s.py --restore`. The expected diagnosis and its evidence
are described in [docs/technical_report.md](docs/technical_report.md#6-failure-investigations).

## 11. Demonstration scenarios

```bash
python scripts/run_scenarios.py --list
python scripts/run_scenarios.py              # all scenarios
python scripts/run_scenarios.py --only 4     # one scenario
```

Each scenario is saved to `evidence/NN_name.md`, with the question, the answer and every tool call
and its raw result. These files are the conversation evidence used in the report.

| # | Scenario | Systems |
|---|---|---|
| 1 | Dataset provenance | MLflow → DVC |
| 2 | Model selection | MLflow |
| 3 | Pipeline execution | Airflow (asks for confirmation) |
| 4 | Deployment diagnosis | Kubernetes (run after injecting failure 3) |
| 5 | Multi-tool reasoning | all four |
| 6 | Pipeline failure investigation | Airflow (run after injecting failure 1) |

## 12. Repository layout

```
agent/
  main.py              command-line interface
  orchestrator.py      tool-calling loop, confirmations, grounding check, logging
  llm.py               language-model client (OpenAI-compatible API, Gemini by default)
  schemas.py           ToolSpec / ToolResult / AgentResponse
  safety.py            namespace, DAG and confirmation policy
  grounding.py         checks answers against tool evidence
  logging_utils.py     structured JSON logs with secret masking
  tools/               dvc_tools.py, mlflow_tools.py, airflow_tools.py, kubernetes_tools.py
api/main.py            FastAPI inference service
dags/training_pipeline.py  Airflow 3 training DAG
training/              pipeline steps, provenance capture, CLI (python -m training.train)
data/generate_dataset.py   reproducible dataset versions (the data itself is tracked by DVC)
kubernetes/            namespace, configmap, deployment, service, agent RBAC
docker/                MLflow and Airflow images;  Dockerfile = inference API image
scripts/               deploy_k8s.py (deploy and inject failures), run_scenarios.py
tests/                 pytest suite
docs/technical_report.md, architecture.md, screenshots/, evidence/
```

## 13. Troubleshooting

| Problem | Fix |
|---|---|
| `GEMINI_API_KEY is not set` | create `.env` from `.env.example` and add the key |
| `Daily API quota exhausted` / 503 high demand | set `LLM_FALLBACK_MODEL` (see `.env.example`); free quotas are per model |
| Agent says MLflow or Airflow is *unavailable* | `docker compose ps`; both containers must be running; check `AIRFLOW_PASSWORD` matches |
| Airflow shows no DAG | wait about 30 s after start-up; check `docker compose logs airflow` for import errors |
| DAG fails at `validate_data` with "dataset not found" | the CSV is missing: run `dvc pull` or regenerate it (section 4) |
| Agent says Kubernetes is *unavailable* | enable Kubernetes in Docker Desktop; `kubectl get nodes` must work |
| Pods stuck in `ErrImageNeverPull` / `ImagePullBackOff` right after deploying | Docker Desktop's *kind* cluster mode does not share local images; use the default *kubeadm* mode, or load the image into the cluster |
| `models/model_metadata.json not found` when deploying | train a model that passes the quality gate first (section 6) |

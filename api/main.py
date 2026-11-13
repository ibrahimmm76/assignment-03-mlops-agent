"""FastAPI inference service for the breast-cancer classifier.

Endpoints
    GET  /health/live   liveness: the process is up
    GET  /health/ready  readiness: the model is loaded and can serve predictions
    GET  /version       application version and model provenance
    POST /predict       predictions for one or more feature rows

Configuration comes from environment variables (populated by the Kubernetes ConfigMap):
    MODEL_PATH      path to the joblib model (default /app/models/model.joblib)
    METADATA_PATH   path to model_metadata.json written by the training pipeline
    APP_VERSION     application version shown by /version
    LOG_LEVEL       logging level
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import joblib
import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

MODEL_PATH = Path(os.getenv("MODEL_PATH", "/app/models/model.joblib"))
METADATA_PATH = Path(os.getenv("METADATA_PATH", "/app/models/model_metadata.json"))
APP_VERSION = os.getenv("APP_VERSION", "dev")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}')
log = logging.getLogger("inference-api")

state: dict = {"model": None, "metadata": {}, "error": None, "features": None, "started": time.time()}


def load_model() -> None:
    try:
        state["model"] = joblib.load(MODEL_PATH)
        state["features"] = list(getattr(state["model"], "feature_names_in_", []))
        state["error"] = None
        log.info("model loaded from %s", MODEL_PATH)
    except FileNotFoundError:
        state["error"] = f"model file not found at {MODEL_PATH}"
        log.error(state["error"])
    except Exception as exc:  # corrupt or incompatible file
        state["error"] = f"failed to load model from {MODEL_PATH}: {exc}"
        log.error(state["error"])
    if METADATA_PATH.exists():
        state["metadata"] = json.loads(METADATA_PATH.read_text(encoding="utf-8"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_model()
    yield


app = FastAPI(title="Breast cancer inference API", version=APP_VERSION, lifespan=lifespan)


class PredictRequest(BaseModel):
    rows: list[dict[str, float]] = Field(..., min_length=1, max_length=1000,
                                         description="Feature rows keyed by feature name")


@app.get("/health/live")
def live() -> dict:
    return {"status": "alive"}


@app.get("/health/ready")
def ready() -> dict:
    if state["model"] is None:
        raise HTTPException(status_code=503, detail=state["error"] or "model not loaded")
    return {"status": "ready"}


@app.get("/version")
def version() -> dict:
    return {"app_version": APP_VERSION, "model_path": str(MODEL_PATH), "model_loaded": state["model"] is not None,
            "model": state["metadata"], "uptime_seconds": round(time.time() - state["started"])}


@app.post("/predict")
def predict(request: PredictRequest) -> dict:
    if state["model"] is None:
        raise HTTPException(status_code=503, detail=state["error"] or "model not loaded")
    frame = pd.DataFrame(request.rows)
    if state["features"]:
        missing = sorted(set(state["features"]) - set(frame.columns))
        if missing:
            raise HTTPException(status_code=422, detail=f"missing features: {missing[:5]}")
        frame = frame[state["features"]]
    proba = state["model"].predict_proba(frame)[:, 1]
    return {"predictions": [{"label": int(p >= 0.5), "probability": round(float(p), 4)} for p in proba],
            "model_version": state["metadata"].get("model_version")}

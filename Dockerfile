# Inference API image. The promoted model (models/model.joblib) is baked in, so build it
# after a training run has passed the quality gate: python scripts/deploy_k8s.py does both.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements-api.txt .
RUN pip install -r requirements-api.txt

COPY api/ api/
COPY models/model.joblib models/model_metadata.json models/

RUN useradd --create-home --uid 10001 appuser
USER 10001

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]

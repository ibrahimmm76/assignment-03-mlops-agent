FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 MLFLOW_DISABLE_AGENT_HINT=1
RUN pip install "mlflow==3.18.0"

# Backend store and artifacts live in the mlflow-data volume (see docker-compose.yml).
RUN mkdir -p /mlflow
EXPOSE 5000

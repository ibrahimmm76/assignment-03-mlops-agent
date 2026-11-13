FROM apache/airflow:3.3.2

# Training dependencies for the DAG tasks, installed against Airflow's own constraints so
# Airflow's dependencies are not upgraded. skops is needed by MLflow 3 to save sklearn models.
ARG AIRFLOW_VERSION=3.3.2
ARG PYTHON_VERSION=3.12
RUN pip install --no-cache-dir \
      "mlflow-skinny==3.18.0" "skops==0.16.0" "scikit-learn==1.9.1" pandas joblib pyyaml \
      --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"

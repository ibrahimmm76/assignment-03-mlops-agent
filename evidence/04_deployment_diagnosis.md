# Agent conversation: Why is my prediction API unavailable?

- Time: 2026-11-13T10:55:13+00:00
- Request id: `bbf6a24df60b`
- Model: `gemini-3.5-flash-lite`
- Tool calls: 4

## Question

Why is my prediction API unavailable?

## Answer

**Facts**:
- Total pods for `inference-api` is 2, and both are unhealthy (`ready: 0/1`) with readiness probe failing due to HTTP status code 503 [k8s_find_unhealthy_pods].
- The `inference-api` service has 0 ready endpoints and 2 not-ready endpoints, meaning it cannot serve traffic [k8s_list_services].
- The deployment is configured to load a model file at `/app/models/model-v2.joblib` (Model Version 4) [k8s_get_deployed_version].
- Pod logs show an explicit error on startup: `model file not found at /app/models/model-v2.joblib` [k8s_get_pod_logs].

**Uncertainties**:
- None.

**Recommended actions**:
- Verify whether the model file was correctly bundled into the container image `inference-api:1.0.0` or if a ConfigMap/Volume mount pointing to the model path is misconfigured or missing.
- Fix the model path or rebuild/redeploy the container image with the correct model artifact included at `/app/models/model-v2.joblib`.

## Tool evidence

### 1. `k8s_find_unhealthy_pods` (ok, 638 ms)

Arguments: `{}`

```json
{
  "namespace": "mlops-demo",
  "total_pods": 2,
  "unhealthy_count": 2,
  "unhealthy_pods": [
    {
      "pod": "inference-api-c7b857dc5-5jzgk",
      "phase": "Running",
      "ready": "0/1",
      "restarts": 0,
      "problems": [
        "container api not ready (readiness probe failing?)"
      ]
    },
    {
      "pod": "inference-api-c7b857dc5-vz5c5",
      "phase": "Running",
      "ready": "0/1",
      "restarts": 0,
      "problems": [
        "container api not ready (readiness probe failing?)"
      ]
    }
  ],
  "recent_warning_events": [
    {
      "type": "Warning",
      "reason": "Unhealthy",
      "object": "Pod/inference-api-c7b857dc5-5jzgk",
      "message": "Readiness probe failed: HTTP probe failed with statuscode: 503",
      "count": 11,
      "last_seen": "0s"
    },
    {
      "type": "Warning",
      "reason": "Unhealthy",
      "object": "Pod/inference-api-c7b857dc5-vz5c5",
      "message": "Readiness probe failed: HTTP probe failed with statuscode: 503",
      "count": 11,
      "last_seen": "0s"
    },
    {
      "type": "Warning",
      "reason": "Unhealthy",
      "object": "Pod/inference-api-c7b857dc5-b4kfk",
      "message": "Readiness probe failed: Get \"http://10.1.0.17:8000/health/ready\": dial tcp 10.1.0.17:8000: connect: connection refused",
      "count": 1,
      "last_seen": "57s"
    },
    {
      "type": "Warning",
      "reason": "Unhealthy",
      "object": "Pod/inference-api-6cc5775cb8-4cq59",
      "message": "Readiness probe failed: Get \"http://10.1.0.14:8000/health/ready\": context deadline exceeded (Client.Timeout exceeded while awaiting headers)",
      "count": 1,
      "last_seen": "15m"
    },
    {
      "type": "Warning",
      "reason": "Unhealthy",
      "object": "Pod/inference-api-9bc76f787-lqgsg",
      "message": "Readiness probe failed: HTTP probe failed with statuscode: 503",
      "count": 25,
      "last_seen": "16m"
    },
    {
      "type": "Warning",
      "reason": "Unhealthy",
      "object": "Pod/inference-api-59fcb665db-6cpr4",
      "message": "Readiness probe failed: HTTP probe failed with statuscode: 503",
      "count": 25,
      "last_seen": "42m"
    }
  ],
  "healthy": false
}
```

### 2. `k8s_list_services` (ok, 5 ms)

Arguments: `{}`

```json
{
  "namespace": "mlops-demo",
  "services": [
    {
      "name": "inference-api",
      "type": "ClusterIP",
      "cluster_ip": "10.106.182.46",
      "ports": [
        {
          "port": 80,
          "target_port": "http",
          "protocol": "TCP"
        }
      ],
      "selector": {
        "app": "inference-api"
      },
      "ready_endpoints": 0,
      "not_ready_endpoints": 2,
      "serving": false
    }
  ]
}
```

### 3. `k8s_get_deployed_version` (ok, 9 ms)

Arguments: `{}`

```json
{
  "namespace": "mlops-demo",
  "deployment": "inference-api",
  "images": [
    "inference-api:1.0.0"
  ],
  "app_version": "1.0.0",
  "model_version": "4",
  "revision": "8",
  "replicas": {
    "desired": 2,
    "ready": 0
  },
  "config_maps": {
    "inference-api-config": {
      "APP_VERSION": "1.0.0",
      "LOG_LEVEL": "INFO",
      "METADATA_PATH": "/app/models/model_metadata.json",
      "MODEL_PATH": "/app/models/model-v2.joblib",
      "MODEL_VERSION": "4"
    }
  }
}
```

### 4. `k8s_get_pod_logs` (ok, 32 ms)

Arguments: `{"tail_lines": 50, "pod": "inference-api-c7b857dc5-5jzgk"}`

```json
{
  "namespace": "mlops-demo",
  "pod": "inference-api-c7b857dc5-5jzgk",
  "previous_container": false,
  "lines": [
    "INFO:     Started server process [1]",
    "INFO:     Waiting for application startup.",
    "{\"ts\":\"2026-11-13 10:54:02,819\",\"level\":\"ERROR\",\"msg\":\"model file not found at /app/models/model-v2.joblib\"}",
    "INFO:     Application startup complete.",
    "INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)"
  ],
  "probe_requests": {
    "/health/ready 503": 12,
    "/health/live 200": 5
  },
  "error_lines": [
    "{\"ts\":\"2026-11-13 10:54:02,819\",\"level\":\"ERROR\",\"msg\":\"model file not found at /app/models/model-v2.joblib\"}"
  ]
}
```

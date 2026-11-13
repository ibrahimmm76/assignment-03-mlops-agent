"""Kubernetes operations tools (official Python client).

Every call is limited to allow-listed namespaces. The two state-changing tools
(restart, scale) are marked ``mutating`` so the orchestrator asks the user before
running them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent.config import Settings
from agent.errors import InvalidRequest, ServiceUnavailable, ToolError
from agent.safety import SafetyPolicy
from agent.schemas import ToolSpec, boolean, integer, obj, string

BAD_WAITING_REASONS = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError",
                       "InvalidImageName", "RunContainerError", "CreateContainerError"}


def _age(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    seconds = int((datetime.now(UTC) - ts).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


class KubernetesTools:
    def __init__(self, settings: Settings, policy: SafetyPolicy, core: Any = None, apps: Any = None) -> None:
        self.settings = settings
        self.policy = policy
        self._core, self._apps = core, apps

    # ------------------------------------------------------------------ clients
    def _load(self) -> None:
        if self._core is not None:
            return
        try:
            from kubernetes import client, config
        except ImportError as exc:
            raise ServiceUnavailable("the 'kubernetes' Python package is not installed") from exc
        try:
            config.load_kube_config(context=self.settings.k8s_context)
        except Exception:
            try:
                config.load_incluster_config()
            except Exception as exc:
                raise ServiceUnavailable("no Kubernetes configuration found (kubeconfig or in-cluster)") from exc
        self._core, self._apps = client.CoreV1Api(), client.AppsV1Api()

    @property
    def core(self):
        self._load()
        return self._core

    @property
    def apps(self):
        self._load()
        return self._apps

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status == 404:
                what = f"{getattr(exc, 'reason', '')} {kwargs.get('name', '')}".strip()
                raise InvalidRequest(f"not found: {what}") from exc
            if status in (401, 403):
                raise ToolError(f"Kubernetes API denied access ({status}); check RBAC for the agent's "
                                "credentials") from exc
            text = f"{type(exc).__name__}: {exc}"
            if any(s in text for s in ("MaxRetryError", "Connection refused", "NewConnectionError", "timed out")):
                raise ServiceUnavailable("Kubernetes API server is unreachable (is the cluster running?)") from exc
            raise ToolError(text[:300]) from exc

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _pod_summary(p) -> dict[str, Any]:
        statuses = p.status.container_statuses or []
        containers = []
        for cs in statuses:
            state, reason, detail = "unknown", None, None
            if cs.state and cs.state.running:
                state = "running"
            elif cs.state and cs.state.waiting:
                state, reason, detail = "waiting", cs.state.waiting.reason, cs.state.waiting.message
            elif cs.state and cs.state.terminated:
                state, reason = "terminated", cs.state.terminated.reason
            last = cs.last_state.terminated if cs.last_state and cs.last_state.terminated else None
            containers.append({"name": cs.name, "image": cs.image, "ready": bool(cs.ready),
                               "restarts": cs.restart_count, "state": state, "reason": reason,
                               "message": (detail or "")[:300] or None,
                               "last_termination": {"reason": last.reason, "exit_code": last.exit_code}
                               if last else None})
        ready = sum(c["ready"] for c in containers)
        return {"name": p.metadata.name, "phase": p.status.phase, "ready": f"{ready}/{len(containers)}",
                "restarts": sum(c["restarts"] or 0 for c in containers), "node": p.spec.node_name,
                "age": _age(p.metadata.creation_timestamp), "labels": p.metadata.labels or {},
                "containers": containers}

    @staticmethod
    def _is_unhealthy(pod: dict[str, Any]) -> list[str]:
        problems = []
        if pod["phase"] not in ("Running", "Succeeded"):
            problems.append(f"phase {pod['phase']}")
        for c in pod["containers"]:
            if c["reason"] in BAD_WAITING_REASONS:
                problems.append(f"container {c['name']}: {c['reason']}")
            elif not c["ready"] and pod["phase"] == "Running":
                problems.append(f"container {c['name']} not ready (readiness probe failing?)")
            if c["restarts"]:
                last = c["last_termination"]
                problems.append(f"container {c['name']} restarted {c['restarts']}x"
                                + (f" (last: {last['reason']}, exit {last['exit_code']})" if last else ""))
        return problems

    def _events(self, ns: str, warnings_only: bool = True, limit: int = 20) -> list[dict[str, Any]]:
        events = self._call(self.core.list_namespaced_event, ns).items
        rows = []
        for ev in events:
            if warnings_only and ev.type != "Warning":
                continue
            when = ev.last_timestamp or ev.event_time or ev.metadata.creation_timestamp
            rows.append({"type": ev.type, "reason": ev.reason, "object": f"{ev.involved_object.kind}/"
                         f"{ev.involved_object.name}", "message": (ev.message or "")[:300], "count": ev.count,
                         "last_seen": _age(when), "_ts": when})
        rows.sort(key=lambda r: r["_ts"] or datetime.min.replace(tzinfo=UTC), reverse=True)
        for r in rows:
            r.pop("_ts")
        return rows[:limit]

    # ------------------------------------------------------------------ read tools
    def list_deployments(self, namespace: str | None = None) -> dict[str, Any]:
        ns = self.policy.check_namespace(namespace)
        items = self._call(self.apps.list_namespaced_deployment, ns).items
        return {"namespace": ns, "deployments": [{
            "name": d.metadata.name, "desired": d.spec.replicas, "ready": d.status.ready_replicas or 0,
            "available": d.status.available_replicas or 0, "updated": d.status.updated_replicas or 0,
            "images": [c.image for c in d.spec.template.spec.containers],
            "version_label": (d.metadata.labels or {}).get("app.kubernetes.io/version"),
            "conditions": [{"type": c.type, "status": c.status, "reason": c.reason}
                           for c in d.status.conditions or []]} for d in items]}

    def list_pods(self, namespace: str | None = None, label_selector: str | None = None) -> dict[str, Any]:
        ns = self.policy.check_namespace(namespace)
        pods = self._call(self.core.list_namespaced_pod, ns, label_selector=label_selector or "").items
        return {"namespace": ns, "pods": [self._pod_summary(p) for p in pods]}

    def get_pod_logs(self, pod: str, namespace: str | None = None, tail_lines: int = 50,
                     previous: bool = False) -> dict[str, Any]:
        ns = self.policy.check_namespace(namespace)
        tail = max(1, min(int(tail_lines), 500))
        # Probes write an access-log line every few seconds, which quickly pushes the start-up
        # messages (e.g. "model file not found") out of a short tail. Read further back and keep
        # probe lines only as counts.
        logs = self._call(self.core.read_namespaced_pod_log, name=pod, namespace=ns,
                          tail_lines=2000, previous=bool(previous))
        all_lines = (logs or "").splitlines()
        probes = [ln for ln in all_lines if '"GET /health/' in ln]
        other = [ln for ln in all_lines if '"GET /health/' not in ln]
        probe_summary: dict[str, int] = {}
        for ln in probes:
            path = "/health/ready" if "/health/ready" in ln else "/health/live"
            code = ln.rsplit('" ', 1)[-1].split(" ", 1)[0] if '" ' in ln else "?"
            key = f"{path} {code}"
            probe_summary[key] = probe_summary.get(key, 0) + 1
        return {"namespace": ns, "pod": pod, "previous_container": bool(previous), "lines": other[-tail:],
                "probe_requests": probe_summary,
                "error_lines": [ln for ln in other if any(k in ln for k in ("ERROR", "Error", "Traceback",
                                                                           "not found", "Exception"))][-10:]}

    def list_services(self, namespace: str | None = None) -> dict[str, Any]:
        ns = self.policy.check_namespace(namespace)
        services = self._call(self.core.list_namespaced_service, ns).items
        out = []
        for s in services:
            try:
                ep = self._call(self.core.read_namespaced_endpoints, name=s.metadata.name, namespace=ns)
                ready = sum(len(sub.addresses or []) for sub in ep.subsets or [])
                not_ready = sum(len(sub.not_ready_addresses or []) for sub in ep.subsets or [])
            except InvalidRequest:
                ready, not_ready = 0, 0
            out.append({"name": s.metadata.name, "type": s.spec.type, "cluster_ip": s.spec.cluster_ip,
                        "ports": [{"port": p.port, "target_port": str(p.target_port), "protocol": p.protocol}
                                  for p in s.spec.ports or []],
                        "selector": s.spec.selector, "ready_endpoints": ready, "not_ready_endpoints": not_ready,
                        "serving": ready > 0})
        return {"namespace": ns, "services": out}

    def get_events(self, namespace: str | None = None, warnings_only: bool = True) -> dict[str, Any]:
        ns = self.policy.check_namespace(namespace)
        return {"namespace": ns, "events": self._events(ns, warnings_only)}

    def find_unhealthy_pods(self, namespace: str | None = None) -> dict[str, Any]:
        """Pods that are not ready, crash-looping, failing to pull images or restarting, plus warnings."""
        ns = self.policy.check_namespace(namespace)
        pods = [self._pod_summary(p) for p in self._call(self.core.list_namespaced_pod, ns).items]
        unhealthy = []
        for p in pods:
            problems = self._is_unhealthy(p)
            if problems:
                unhealthy.append({"pod": p["name"], "phase": p["phase"], "ready": p["ready"],
                                  "restarts": p["restarts"], "problems": problems})
        return {"namespace": ns, "total_pods": len(pods), "unhealthy_count": len(unhealthy),
                "unhealthy_pods": unhealthy, "recent_warning_events": self._events(ns, True, limit=15),
                "healthy": not unhealthy}

    def get_deployed_version(self, deployment: str = "inference-api", namespace: str | None = None) -> dict:
        ns = self.policy.check_namespace(namespace)
        d = self._call(self.apps.read_namespaced_deployment, name=deployment, namespace=ns)
        labels = d.metadata.labels or {}
        annotations = d.spec.template.metadata.annotations or {}
        config_maps = {ref.config_map_ref.name for c in d.spec.template.spec.containers
                       for ref in (c.env_from or []) if ref.config_map_ref}
        cm_data = {}
        for name in config_maps:
            try:
                cm_data[name] = self._call(self.core.read_namespaced_config_map, name=name, namespace=ns).data or {}
            except InvalidRequest:
                cm_data[name] = {"error": "config map not found"}
        cm_model_version = next((data.get("MODEL_VERSION") for data in cm_data.values() if data.get("MODEL_VERSION")),
                                None)
        return {"namespace": ns, "deployment": deployment,
                "images": [c.image for c in d.spec.template.spec.containers],
                "app_version": labels.get("app.kubernetes.io/version"),
                "model_version": annotations.get("mlops/model-version") or cm_model_version,
                "revision": (d.metadata.annotations or {}).get("deployment.kubernetes.io/revision"),
                "replicas": {"desired": d.spec.replicas, "ready": d.status.ready_replicas or 0},
                "config_maps": cm_data}

    # ------------------------------------------------------------------ mutating tools
    def restart_deployment(self, deployment: str, namespace: str | None = None) -> dict[str, Any]:
        ns = self.policy.check_namespace(namespace)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}}
        self._call(self.apps.patch_namespaced_deployment, name=deployment, namespace=ns, body=body)
        return {"namespace": ns, "deployment": deployment, "restarted_at": now,
                "note": "rolling restart started; check pods in ~30s"}

    def scale_deployment(self, deployment: str, replicas: int, namespace: str | None = None) -> dict[str, Any]:
        ns = self.policy.check_namespace(namespace)
        replicas = int(replicas)
        if not 0 <= replicas <= 5:
            raise InvalidRequest("replicas must be between 0 and 5")
        self._call(self.apps.patch_namespaced_deployment_scale, name=deployment, namespace=ns,
                   body={"spec": {"replicas": replicas}})
        return {"namespace": ns, "deployment": deployment, "replicas": replicas}

    # ------------------------------------------------------------------ registry
    def specs(self) -> list[ToolSpec]:
        ns = {"namespace": string("Namespace (default: the approved application namespace)")}
        return [
            ToolSpec("k8s_list_deployments", "kubernetes",
                     "Deployments with desired/ready replicas, images, version label and conditions.",
                     obj(ns), self.list_deployments),
            ToolSpec("k8s_list_pods", "kubernetes",
                     "Pods with phase, readiness, restart counts and container states/reasons.",
                     obj({**ns, "label_selector": string("e.g. app=inference-api")}), self.list_pods),
            ToolSpec("k8s_get_pod_logs", "kubernetes", "Recent log lines of a pod (optionally the previous "
                     "container after a restart).",
                     obj({"pod": string("Pod name"), **ns, "tail_lines": integer("Lines (default 50)"),
                          "previous": boolean("Logs of the previous (crashed) container")}, ["pod"]),
                     self.get_pod_logs),
            ToolSpec("k8s_list_services", "kubernetes",
                     "Services with ports, selectors and how many ready endpoints back them (0 means the service "
                     "cannot serve traffic).", obj(ns), self.list_services),
            ToolSpec("k8s_get_events", "kubernetes", "Recent namespace events (warnings by default), e.g. probe "
                     "failures, image pull errors, back-off restarts.",
                     obj({**ns, "warnings_only": boolean("Only Warning events (default true)")}), self.get_events),
            ToolSpec("k8s_find_unhealthy_pods", "kubernetes",
                     "Diagnose application health: unhealthy pods (not ready, crash-looping, image errors, "
                     "restarts) with reasons, plus recent warning events.", obj(ns), self.find_unhealthy_pods),
            ToolSpec("k8s_get_deployed_version", "kubernetes",
                     "Currently deployed application version: image, version label, model version annotation, "
                     "rollout revision, replicas and ConfigMap settings.",
                     obj({"deployment": string("Deployment name (default inference-api)"), **ns}),
                     self.get_deployed_version),
            ToolSpec("k8s_restart_deployment", "kubernetes",
                     "Rolling restart of a deployment (state-changing: requires explicit user confirmation and an "
                     "approved namespace).", obj({"deployment": string("Deployment name"), **ns}, ["deployment"]),
                     self.restart_deployment, mutating=True),
            ToolSpec("k8s_scale_deployment", "kubernetes",
                     "Scale a deployment to 0-5 replicas (state-changing: requires explicit user confirmation and "
                     "an approved namespace).",
                     obj({"deployment": string("Deployment name"), "replicas": integer("0-5"), **ns},
                         ["deployment", "replicas"]), self.scale_deployment, mutating=True),
        ]

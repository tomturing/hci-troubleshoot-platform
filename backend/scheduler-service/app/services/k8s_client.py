"""
Kubernetes Client - K8s API Client (v2.0 多类型AI助手)
"""

import time
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from shared.observability.logger import get_logger

from app.config import settings

logger = get_logger("k8s-client")


class K8sClient:
    """Kubernetes API客户端"""

    def __init__(self):
        try:
            # 尝试加载集群内配置
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes config")
        except config.ConfigException:
            try:
                # 尝试加载本地配置 (kubeconfig)
                config.load_kube_config()
                logger.info("Loaded local kubeconfig")
            except Exception as e:
                logger.error(f"Failed to load Kubernetes config: {e}")
                # 在某些环境下（如CI/CD或纯Docker Compose），可能没有K8s环境
                # 这里不抛出异常，允许应用启动，但在调用时可能会失败

        self.core_v1 = client.CoreV1Api()
        self.namespace = settings.K8S_NAMESPACE
        self.image_pull_secret = settings.K8S_IMAGE_PULL_SECRET

    def create_pod(
        self,
        pod_name: str,
        case_id: str | None = None,
        trace_id: str | None = None,
        assistant_type: str = "htp-agent",
        assistant_config: dict[str, Any] | None = None,
        case_info: dict[str, Any] | None = None,
    ) -> bool:
        """
        创建AI助手Pod (v3.0: 支持 ProductionClaw 一 Pod 一 Case 模式)

        Args:
            pod_name: Pod名称
            case_id: 工单ID
            trace_id: 追踪ID
            assistant_type: AI助手类型（htp-agent 等）
            assistant_config: 助手配置（base_url, model 等）
            case_info: 工单附加信息（title, description, created_at），注入为环境变量

        Returns:
            bool: 是否成功触发创建
        """
        cfg = assistant_config or {}
        base_url = cfg.get("base_url", "")
        model = cfg.get("model", "glm-5")
        custom_labels = cfg.get("labels", {})
        custom_env = cfg.get("env", [])

        labels = {
            "app": assistant_type,
            "assistant-type": assistant_type,
            # A-3: 统一使用 scheduler-service 标签，供部署前孤立 Pod 清理脚本 selector 使用
            "managed-by": "scheduler-service",
            "pod-name": pod_name,
        }
        labels.update(custom_labels)
        if case_id:
            labels["case-id"] = case_id

        # ── 基础环境变量 ─────────────────────────────────────────
        env_vars = [
            {"name": "HOME", "value": "/home/node"},
            {"name": "TERM", "value": "xterm-256color"},
            # Pod 身份（Downward API）
            {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
            # LLM 配置
            {"name": "LLM_BASE_URL", "value": base_url},
            {"name": "LLM_MODEL", "value": model},
            {"name": "LLM_API_KEY", "valueFrom": {"secretKeyRef": {"name": "hci-secrets", "key": "LLM_API_KEY"}}},
            {
                "name": "INTERNAL_API_TOKEN",
                "valueFrom": {"secretKeyRef": {"name": "hci-secrets", "key": "INTERNAL_API_TOKEN"}},
            },
            # 服务地址
            {
                "name": "KB_SERVICE_URL",
                "valueFrom": {"configMapKeyRef": {"name": "hci-common-config", "key": "KB_SERVICE_URL"}},
            },
            {
                "name": "CASE_SERVICE_URL",
                "valueFrom": {"configMapKeyRef": {"name": "hci-common-config", "key": "CASE_SERVICE_URL"}},
            },
            {
                "name": "CONVERSATION_SERVICE_URL",
                "valueFrom": {"configMapKeyRef": {"name": "hci-common-config", "key": "CONVERSATION_SERVICE_URL"}},
            },
        ]

        # ── 工单信息注入 ────────────────────────────────────────
        if case_id:
            env_vars.append({"name": "CASE_ID", "value": case_id})
        if case_info:
            env_vars.extend([
                {"name": "CASE_TITLE", "value": case_info.get("title", "")},
                {"name": "CASE_DESCRIPTION", "value": case_info.get("description", "")},
                {"name": "CASE_CREATED_AT", "value": case_info.get("created_at", "")},
            ])

        # 其他自定义环境变量（来自 AssistantRegistry）
        for ev in custom_env:
            if isinstance(ev, dict):
                env_vars.append({"name": ev.get("name", ""), "value": ev.get("value", "")})

        # ── volumes ───────────────────────────────────────────────
        volumes: list[dict[str, Any]] = []
        init_containers: list[dict[str, Any]] = []
        volume_mounts: list[dict[str, Any]] = []

        # ── Pod Manifest ────────────────────────────────────────
        # 注意：当前架构不再使用动态 Pod 创建，所有助手通过 base_url 直连
        # 此代码保留用于未来可能的动态 Pod 场景
        pod_manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": pod_name,
                "labels": labels,
                "annotations": {
                    "case-id": case_id or "",
                    "trace-id": trace_id or "",
                    "assistant-type": assistant_type,
                },
            },
            "spec": {
                "securityContext": {"runAsUser": 1001, "runAsGroup": 1001, "fsGroup": 1001},
                "initContainers": init_containers,
                "containers": [
                    {
                        "name": assistant_type,
                        "image": cfg.get("image", ""),
                        "imagePullPolicy": settings.K8S_IMAGE_PULL_POLICY,
                        "env": env_vars,
                        "volumeMounts": volume_mounts,
                    }
                ],
                "volumes": volumes,
                "imagePullSecrets": ([{"name": self.image_pull_secret}] if self.image_pull_secret else []),
                "restartPolicy": "Never",
                # 绕过宿主机 Clash TUN fake-ip DNS 劫持（参考 PIT-034）
                "dnsPolicy": "None",
                "dnsConfig": {
                    "nameservers": ["114.114.114.114", "1.2.4.8"],
                    "options": [{"name": "ndots", "value": "1"}],
                },
            },
        }

        manifest_version = "v4_llm_config"
        has_init_container = bool(init_containers)
        has_claw_home_volume = any(v.get("name") == "claw-home" for v in volumes)

        # 指数退避重试，应对 K8s API 瞬时抖动（最多 3 次，间隔 0.5s / 1.0s）
        max_retries, base_delay = 3, 0.5
        for attempt in range(max_retries):
            try:
                self.core_v1.create_namespaced_pod(body=pod_manifest, namespace=self.namespace)
                logger.info(
                    event="pod_create_initiated",
                    message=f"Created {assistant_type} pod {pod_name}",
                    pod_name=pod_name,
                    assistant_type=assistant_type,
                    case_id=case_id,
                    manifest_version=manifest_version,
                    has_init_container=has_init_container,
                    has_claw_home_volume=has_claw_home_volume,
                    trace_id=trace_id,
                )
                return True
            except ApiException as e:
                # 参数错误/已存在/资源不合法，无需重试
                if e.status in (400, 409, 422):
                    logger.error(
                        event="pod_create_failed",
                        message=f"Failed to create {assistant_type} pod {pod_name}: {e.reason}",
                        error=str(e),
                        trace_id=trace_id,
                    )
                    return False
                if attempt < max_retries - 1:
                    delay = base_delay * (2**attempt)
                    logger.warning(
                        event="pod_create_retry",
                        message=f"K8s API error (attempt {attempt + 1}/{max_retries}), retrying in {delay:.1f}s",
                        pod_name=pod_name,
                        error=str(e),
                        trace_id=trace_id,
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        event="pod_create_failed",
                        message=f"Failed to create {assistant_type} pod {pod_name} after {max_retries} attempts: {e.reason}",
                        error=str(e),
                        trace_id=trace_id,
                    )
        return False

    def delete_pod(self, pod_name: str) -> bool:
        """删除Pod"""
        try:
            self.core_v1.delete_namespaced_pod(name=pod_name, namespace=self.namespace)
            logger.info(f"Deleted pod {pod_name}")
            return True
        except ApiException as e:
            if e.status == 404:
                return True  # 已经不存在了
            logger.error(f"Failed to delete pod {pod_name}: {e}")
            return False

    def get_pod_status(self, pod_name: str) -> str | None:
        """获取Pod状态 (Pending, Running, Succeeded, Failed, Unknown)"""
        try:
            pod = self.core_v1.read_namespaced_pod(name=pod_name, namespace=self.namespace)
            return pod.status.phase
        except ApiException as e:
            if e.status == 404:
                return None
            logger.error(f"Failed to get status for pod {pod_name}: {e}")
            return None

    def get_pod_ip(self, pod_name: str) -> str | None:
        """获取Pod IP"""
        try:
            pod = self.core_v1.read_namespaced_pod(name=pod_name, namespace=self.namespace)
            return pod.status.pod_ip
        except ApiException:
            return None

    def list_pods(self, label_selector: str = "app=openclaw") -> list[dict[str, Any]]:
        """列出Pod，返回标准化的字典列表"""
        try:
            pods = self.core_v1.list_namespaced_pod(namespace=self.namespace, label_selector=label_selector)
            result = []
            for pod in pods.items:
                annotations = {}
                if pod.metadata and pod.metadata.annotations:
                    annotations = dict(pod.metadata.annotations)
                result.append(
                    {
                        "name": pod.metadata.name if pod.metadata else "",
                        "status": pod.status.phase if pod.status else "Unknown",
                        "labels": dict(pod.metadata.labels or {}) if pod.metadata else {},
                        "annotations": annotations,
                    }
                )
            return result
        except ApiException as e:
            logger.error(f"Failed to list pods: {e}")
            return []

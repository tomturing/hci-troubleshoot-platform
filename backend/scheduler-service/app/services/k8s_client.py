"""
Kubernetes Client - K8s API Client (v2.0 多类型AI助手)
"""

import time
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from shared.utils.logger import get_logger

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

    def create_pod(
        self,
        pod_name: str,
        case_id: str | None = None,
        trace_id: str | None = None,
        assistant_type: str = "openclaw",
        assistant_config: dict[str, Any] | None = None,
    ) -> bool:
        """
        创建AI助手Pod (v2.0: 根据助手配置动态生成Pod Spec)

        Args:
            pod_name: Pod名称
            case_id: 工单ID (用于标签)
            trace_id: 追踪ID
            assistant_type: AI助手类型
            assistant_config: 助手配置 (image, port, labels, env等)

        Returns:
            bool: 是否成功触发创建
        """
        # 从 assistant_config 或默认值获取配置
        cfg = assistant_config or {}
        image = cfg.get("image", settings.OPENCLAW_IMAGE)
        port = cfg.get("port", 18789)
        custom_labels = cfg.get("labels", {})
        custom_env = cfg.get("env", [])

        labels = {
            "app": assistant_type,
            "assistant-type": assistant_type,
            "managed-by": "hci-scheduler",
            "pod-name": pod_name,
        }
        labels.update(custom_labels)
        if case_id:
            labels["case-id"] = case_id

        # 构建环境变量列表
        env_vars = []
        for ev in custom_env:
            if isinstance(ev, dict):
                env_vars.append({"name": ev.get("name", ""), "value": ev.get("value", "")})

        # Pod Spec
        pod_manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": pod_name, "labels": labels},
            "spec": {
                "containers": [
                    {
                        "name": assistant_type,
                        "image": image,
                        "ports": [{"containerPort": port}],
                        "env": env_vars,
                    }
                ],
                "restartPolicy": "Never",
            },
        }

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

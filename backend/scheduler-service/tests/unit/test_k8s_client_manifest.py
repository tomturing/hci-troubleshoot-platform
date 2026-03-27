"""
K8sClient Pod Manifest 单元测试

目标：防止 productionclaw Pod 创建逻辑回退为“无 initContainer/无配置挂载”的旧实现。
"""

from unittest.mock import MagicMock

from app.services.k8s_client import K8sClient


def test_create_pod_contains_init_workspace_and_config_mount(monkeypatch):
    """productionclaw Pod 必须包含 init-workspace + claw-home + init-config。"""
    fake_core_v1 = MagicMock()

    # 避免真实加载集群配置，完全在内存中验证 manifest。
    import app.services.k8s_client as k8s_module

    monkeypatch.setattr(k8s_module.config, "load_incluster_config", lambda: None)
    monkeypatch.setattr(k8s_module.client, "CoreV1Api", lambda: fake_core_v1)

    k8s = K8sClient()
    ok = k8s.create_pod(
        pod_name="productionclaw-pool-ut-001",
        assistant_type="productionclaw",
        assistant_config={
            "image": "hci-openclaw:test",
            "port": 18789,
            "init_configmap": "productionclaw-init-config",
            "labels": {"claw-role": "production"},
        },
    )

    assert ok is True
    fake_core_v1.create_namespaced_pod.assert_called_once()

    manifest = fake_core_v1.create_namespaced_pod.call_args.kwargs["body"]
    spec = manifest["spec"]

    assert spec["initContainers"][0]["name"] == "init-workspace"

    volume_names = {v["name"] for v in spec["volumes"]}
    assert "claw-home" in volume_names
    assert "init-config" in volume_names

    init_cfg = next(v for v in spec["volumes"] if v["name"] == "init-config")
    assert init_cfg["configMap"]["name"] == "productionclaw-init-config"

    mount_names = {m["name"] for m in spec["containers"][0]["volumeMounts"]}
    assert "claw-home" in mount_names

    assert manifest["metadata"]["annotations"]["assistant-type"] == "productionclaw"

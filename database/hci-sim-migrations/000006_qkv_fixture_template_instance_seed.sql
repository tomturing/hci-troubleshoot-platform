-- hci-sim 过渡资产种子：将已确认的 qkv 模板和实例长期写入现有模板表。
--
-- 当前最小化模型不新增 fixture_library 和实例表，因此使用 template_json.asset_type
-- 区分 template/instance。实例仍是不可变 JSON 快照；后续若出现独立检索、审批、
-- revision diff 和 stale 传播需求，再迁移到规范化资产模型。

INSERT INTO fixture.bundle_template
    (id, support_id, kbd_revision, name, template_json, template_digest, owner, version, status)
VALUES
    (
        '70000000-0000-0000-0000-000000000001', 'qkv-library', 1, 'qkv_alert.template.v1',
        $$
        {
          "asset_type": "template",
          "signal_type": "qkv_alert",
          "template_revision": 1,
          "stdout_format": "json",
          "record_path": "data[]",
          "keyword_strategy": {"field": "type", "mode": "contains"},
          "category_baseline": {"source": "backend/kb-service/config/category_baseline.yaml", "revision": "1.0", "checksum": "sha256:4aaa1e4811c5347efe2f270b62eb9a58eb7c7453927ccf5a115af281ebe82b21"},
          "catalog_baseline": {"source": "backend/shared/resolution/catalogs/resolution_catalog.json", "revision": "2026-08-13.1", "checksum": "sha256:3a85084e74dd6911472e0717a988f9692e59cd279d2a2c80c05a164bd19d5612"},
          "template": {
            "data": [{
              "alert_type": "{{ALERT_TYPE}}",
              "description": "{{DESCRIPTION}}",
              "object_type": "{{OBJECT_TYPE}}",
              "object_name": "{{OBJECT_NAME}}",
              "urgent_type": "{{URGENT_TYPE}}",
              "start": "{{START}}",
              "end": "{{END}}"
            }]
          }
        }
        $$::jsonb,
        NULL, 'fixture-seed', 1, 'published'
    ),
    (
        '70000000-0000-0000-0000-000000000002', 'qkv-library', 1, 'qkv_task.template.v1',
        $$
        {
          "asset_type": "template",
          "signal_type": "qkv_task",
          "template_revision": 1,
          "stdout_format": "json",
          "record_path": "data[]",
          "keyword_strategy": {"field": "alert_type|type", "mode": "contains"},
          "category_baseline": {"source": "backend/kb-service/config/category_baseline.yaml", "revision": "1.0", "checksum": "sha256:4aaa1e4811c5347efe2f270b62eb9a58eb7c7453927ccf5a115af281ebe82b21"},
          "catalog_baseline": {"source": "backend/shared/resolution/catalogs/resolution_catalog.json", "revision": "2026-08-13.1", "checksum": "sha256:3a85084e74dd6911472e0717a988f9692e59cd279d2a2c80c05a164bd19d5612"},
          "template": {
            "data": [{
              "alert_type": "{{TASK_TYPE}}",
              "description": "{{DESCRIPTION}}",
              "process": "{{PROCESS}}",
              "status": "{{STATUS}}",
              "target": "{{TARGET}}",
              "start": "{{START}}",
              "end": "{{END}}"
            }]
          }
        }
        $$::jsonb,
        NULL, 'fixture-seed', 1, 'published'
    ),
    (
        '70000000-0000-0000-0000-000000000003', 'qkv-library', 1, 'qkv_dialog.template.v1',
        $$
        {
          "asset_type": "template",
          "signal_type": "qkv_dialog",
          "template_revision": 1,
          "stdout_format": "log_lines",
          "log_file": "sfvt_vtpdaemon.log",
          "log_path_rule": "/sf/log/<D>/vt/sfvt_vtpdaemon.log",
          "time_rule": "END_MS -> END (second precision)",
          "keyword_strategy": {"field": "message", "mode": "contains"},
          "category_baseline": {"source": "backend/kb-service/config/category_baseline.yaml", "revision": "1.0", "checksum": "sha256:4aaa1e4811c5347efe2f270b62eb9a58eb7c7453927ccf5a115af281ebe82b21"},
          "catalog_baseline": {"source": "backend/shared/schemas/log_source_catalog.py", "revision": "1.2", "checksum": "sha256:fff11db3f684629ddcc288e876df478359fe3e2e2ad50bc1ff92b4109e0e6eba"},
          "template": {
            "lines": [
              "{{LOG_PATH}}:{{EVENT_TIME_US}} err [sfvt_vtpdaemon] {{EVENT_CLOCK_MS}} E {{PID}} QemuServer.pm(VTP::QemuServer::vm_start_error_deal):12936 | [{{TRACE_ROOT}}:{{TRACE_SPAN}}:{{TRACE_SEGMENT}}] [my_die_with_errcode {{ERRCODE}}] message: {{KEYWORD}}（{{VM_NAME}}）失败，错误信息：{{ERROR_MESSAGE}}",
              "{{LOG_PATH}}:{{CONTEXT_TIME_US}} warning [sfvt_vtpdaemon] {{CONTEXT_CLOCK_MS}} W {{PID}} OpLog.pm((eval)):586 | [{{TRACE_ROOT}}:{{TRACE_SPAN}}:{{TRACE_SEGMENT}}] Errcode tracing: {{ERRCODE_TRACE}}, message: {{KEYWORD}}（{{VM_NAME}}）失败，错误信息：{{ERROR_MESSAGE}}"
            ]
          }
        }
        $$::jsonb,
        NULL, 'fixture-seed', 1, 'published'
    ),
    (
        '70000000-0000-0000-0000-000000000011', 'qkv-library', 1, 'qkv_alert.instance.sample.v1',
        $$
        {
          "asset_type": "instance",
          "signal_type": "qkv_alert",
          "template_name": "qkv_alert.template.v1",
          "instance_revision": 1,
          "category_baseline": {"source": "backend/kb-service/config/category_baseline.yaml", "revision": "1.0", "checksum": "sha256:4aaa1e4811c5347efe2f270b62eb9a58eb7c7453927ccf5a115af281ebe82b21"},
          "catalog_baseline": {"source": "backend/shared/resolution/catalogs/resolution_catalog.json", "revision": "2026-08-13.1", "checksum": "sha256:3a85084e74dd6911472e0717a988f9692e59cd279d2a2c80c05a164bd19d5612"},
          "stdout": {"data": [
            {"alert_type": "ha_out_of_resource", "bcancel": 0, "description": "预测性告警：发现HA预留资源不足。当主机出现异常时，可能没有足够的资源去恢复异常主机上所有虚拟机。建议关闭不重要的虚拟机以释放资源，并扩容HA预留资源。该告警可在告警设置中关闭。", "end": "2026-08-26 10:01:02", "host": "SVR_aCloud_668", "hostid": "host-047bcb4bc820", "hostname": "SVR_aCloud_668", "id": 4962, "log_id": "host-047bcb4bc820:24690:1787709662:6444152146619", "mark_read": "0", "object_id": "host-047bcb4bc820", "object_name": "SVR_aCloud_668", "object_type": "集群", "otype": "集群", "pid": "", "process": 100, "reserved3": "{\"maintenance_mode\":\"\",\"host_maintenance_mode_list\":[]}", "start": "2026-08-26 10:01:02", "status": 2, "sysloged": 0, "target": "SVR_aCloud_668", "type": "HA预留资源不足", "upid": null, "urgent_type": "紧急", "user": "admin (172.28.24.2)", "vm": ""},
            {"alert_type": "vs_disk_state", "bcancel": 0, "description": "检测到硬盘（主机<172.28.24.3>，硬盘名称：4号盘）存在坏道，请尽快联系硬件供应商进行专业检查，确认能否继续使用。", "end": "2026-08-26 10:02:03", "host": "SVR_aCloud_669", "hostid": "host-70e284243d2d", "hostname": "SVR_aCloud_669", "id": 4963, "log_id": "host-70e284243d2d:11015:1787709723:5925404369523", "mark_read": "0", "object_id": "172.28.24.3", "object_name": "172.28.24.3", "object_type": "存储", "otype": "存储", "pid": "", "process": 100, "reserved3": "{\"maintenance_mode\":\"\",\"host_maintenance_mode_list\":[]}", "start": "2026-08-26 10:02:03", "status": 2, "sysloged": 0, "target": "172.28.24.3", "type": "磁盘损坏告警", "upid": null, "urgent_type": "紧急", "user": "admin (172.28.24.3)", "vm": ""}
          ]}
        }
        $$::jsonb,
        NULL, 'fixture-seed', 1, 'published'
    ),
    (
        '70000000-0000-0000-0000-000000000012', 'qkv-library', 1, 'qkv_task.instance.sample.v1',
        $$
        {
          "asset_type": "instance",
          "signal_type": "qkv_task",
          "template_name": "qkv_task.template.v1",
          "instance_revision": 1,
          "category_baseline": {"source": "backend/kb-service/config/category_baseline.yaml", "revision": "1.0", "checksum": "sha256:4aaa1e4811c5347efe2f270b62eb9a58eb7c7453927ccf5a115af281ebe82b21"},
          "catalog_baseline": {"source": "backend/shared/resolution/catalogs/resolution_catalog.json", "revision": "2026-08-13.1", "checksum": "sha256:3a85084e74dd6911472e0717a988f9692e59cd279d2a2c80c05a164bd19d5612"},
          "stdout": {"data": [
            {"action_type": 10, "alert_type": "系统备份", "bcancel": 0, "description": "执行系统备份失败，系统备份失败：备份存储可用空间小于10GB，请清理存储空间或切换备份存储。", "dest_host": "", "end": "2026-08-26 02:20:54", "errcode_tracing": "0x01001331/0x01000190/0x0100018E", "event_id": 53084160, "ha_handle_action": "", "ha_handle_result": "", "host": "SVR_aCloud_668", "hostid": "host-047bcb4bc820", "hostname": "SVR_aCloud_668", "id": 1078, "log_id": "host-047bcb4bc820:12443:1787682053:5937677803553", "module_type": 50, "object_id": "", "object_name": "SVR_aCloud_668", "object_type": "", "otype": "主机", "pid": "UPID:host-047bcb4bc820:0000309B:2814071A:6A8DDD05:系统备份::admin@vtp:", "process": "失败", "request_id": ",a8ddd0494805f1cc46a360dadbe1c706", "reserved2": "", "reserved3": "", "risk_level": 10, "sched_effect": "", "sched_reason": "", "start": "2026-08-26 02:20:53", "status": 3, "target": "SVR_aCloud_668", "type": "系统备份", "upid": "", "user": "admin (172.28.24.2)", "vm": ""},
            {"action_type": 0, "alert_type": "启动虚拟机", "bcancel": 0, "description": "启动虚拟机（Server-IMG）失败，错误信息：虚拟机镜像忙，正在执行其他操作！", "dest_host": "", "end": "2026-07-28 00:54:36", "errcode_tracing": "0x0100186F/0x010015BE/0x010015BE/0x010015BE/0x010015BE/0x01002D46", "event_id": 1179651, "ha_handle_action": "", "ha_handle_result": "", "host": "SVR_aCloud_670", "hostid": "host-70e284243e19", "hostname": "SVR_aCloud_670", "id": 920, "log_id": "host-047bcb4bc820:15170:1785171263:2279004061713", "module_type": 1, "object_id": "", "object_name": "Server-IMG", "object_type": "虚拟机", "otype": "虚拟机", "pid": "UPID:host-70e284243e19:000038E6:191CF681:6A678D40:启动虚拟机:4359974862144:admin@vtp:", "process": "失败", "request_id": ",a678d3fb5fdf2af4e78e6dae896a06e2", "reserved2": "", "reserved3": "1003", "risk_level": 5, "sched_effect": "", "sched_reason": "", "start": "2026-07-28 00:54:23", "status": 3, "target": "Server-IMG", "type": "启动虚拟机", "upid": "", "user": "admin (172.28.24.22)", "vm": "4359974862144"}
          ]}
        }
        $$::jsonb,
        NULL, 'fixture-seed', 1, 'published'
    ),
    (
        '70000000-0000-0000-0000-000000000013', 'qkv-library', 1, 'qkv_dialog.instance.sample.v1',
        $$
        {
          "asset_type": "instance",
          "signal_type": "qkv_dialog",
          "template_name": "qkv_dialog.template.v1",
          "instance_revision": 1,
          "category_baseline": {"source": "backend/kb-service/config/category_baseline.yaml", "revision": "1.0", "checksum": "sha256:4aaa1e4811c5347efe2f270b62eb9a58eb7c7453927ccf5a115af281ebe82b21"},
          "catalog_baseline": {"source": "backend/shared/schemas/log_source_catalog.py", "revision": "1.2", "checksum": "sha256:fff11db3f684629ddcc288e876df478359fe3e2e2ad50bc1ff92b4109e0e6eba"},
          "bindings": {"END_MS": "2026-08-26 09:45:19.991807", "END": "2026-08-26 09:45:19", "VM_NAME": "Rocky-IMG", "KEYWORD": "启动虚拟机"},
          "stdout": "/sf/log/26/vt/sfvt_vtpdaemon.log:2026-08-26 09:45:19.991807 err [sfvt_vtpdaemon] 09:45:19.991 E 6955 QemuServer.pm(VTP::QemuServer::vm_start_error_deal):12936 | [a8e4524c9151ac0956995f05d1289081:d41339:45e4a7] [my_die_with_errcode 0x0100186F] message: 启动虚拟机（Rocky-IMG）失败，错误信息：虚拟机镜像忙，正在执行其他操作！\n/sf/log/26/vt/sfvt_vtpdaemon.log:2026-08-26 09:45:20.330764 warning [sfvt_vtpdaemon] 09:45:20.330 W 6955 OpLog.pm((eval)):586 | [a8e4524c9151ac0956995f05d1289081:d41339:231e62] Errcode tracing: 0x0100186F/0x010015BE/0x010015BE/0x01002D46, message: 启动虚拟机（Rocky-IMG）失败，错误信息：虚拟机镜像忙，正在执行其他操作！"
        }
        $$::jsonb,
        NULL, 'fixture-seed', 1, 'published'
    )
ON CONFLICT (support_id, kbd_revision, name) DO UPDATE SET
    template_json = EXCLUDED.template_json,
    version = EXCLUDED.version,
    status = EXCLUDED.status,
    updated_at = now();

COMMENT ON TABLE fixture.bundle_template IS
    '模板和实例过渡种子均存于 template_json；asset_type=template|instance。'
    '该结构只满足当前最小化落库和回放，不替代未来规范化 fixture_library。';

-- Bundle Factory qkv_task 高保真模板 (r2) 与“删除虚拟机”真实实例
-- 严格对齐 Tool Registry 的 10 个标准产出变量 (produces)：
-- DESCRIPTION, END, ERRCODE_TRACING, HOST, HOSTID, PROCESS, REQUEST_ID, TARGET, TYPE, VM
-- 其余审计/固定字段（pid, log_id, user, id, event_id, status 等）作为模板内部固定高保真常量，无需专家配置。

-- 1. 将 qkv_task.template 的 r1 修订设为 retired，为 r2 让出 published 状态
UPDATE fixture.asset_revision
SET status = 'retired', updated_at = now()
WHERE asset_key = 'qkv_task.template' AND revision = 1 AND status = 'published';

-- 2. 插入高保真模板 qkv_task.template Revision 2 (只留 10 个标准变量插槽)
INSERT INTO fixture.asset_revision
    (id, asset_key, asset_type, signal_type, revision, status, content_json, template_asset_key, template_revision, category_baseline_json, catalog_baseline_json, content_digest, created_by, trace_id)
VALUES
('71000000-0000-0000-0000-000000000004','qkv_task.template','template','qkv_task',2,'published',
 '{"stdout_template":"{\\\"data\\\":[{\\\"action_type\\\":1,\\\"alert_type\\\":\\\"{{TYPE}}\\\",\\\"bcancel\\\":0,\\\"description\\\":\\\"{{DESCRIPTION}}\\\",\\\"dest_host\\\":\\\"\\\",\\\"end\\\":\\\"{{END}}\\\",\\\"errcode_tracing\\\":{{ERRCODE_TRACING}},\\\"event_id\\\":1245196,\\\"ha_handle_action\\\":\\\"\\\",\\\"ha_handle_result\\\":\\\"\\\",\\\"host\\\":\\\"{{HOST}}\\\",\\\"hostid\\\":\\\"{{HOSTID}}\\\",\\\"hostname\\\":\\\"{{HOST}}\\\",\\\"id\\\":566,\\\"log_id\\\":\\\"host-047bcb4bc820:9408:1782226435:1568763286126\\\",\\\"module_type\\\":1,\\\"object_id\\\":\\\"\\\",\\\"object_name\\\":\\\"{{TARGET}}\\\",\\\"object_type\\\":\\\"虚拟机\\\",\\\"otype\\\":\\\"虚拟机\\\",\\\"pid\\\":\\\"UPID:host-047bcb4bc820:000024C0:78F6A31:6A3A9E03:task:1114365066966:admin@vtp:\\\",\\\"process\\\":\\\"{{PROCESS}}\\\",\\\"request_id\\\":\\\"{{REQUEST_ID}}\\\",\\\"reserved2\\\":\\\"\\\",\\\"reserved3\\\":\\\"\\\",\\\"risk_level\\\":1,\\\"sched_effect\\\":\\\"\\\",\\\"sched_reason\\\":\\\"\\\",\\\"start\\\":\\\"{{END}}\\\",\\\"status\\\":2,\\\"sysloged\\\":0,\\\"target\\\":\\\"{{TARGET}}\\\",\\\"type\\\":\\\"{{TYPE}}\\\",\\\"upid\\\":\\\"\\\",\\\"user\\\":\\\"admin (172.28.24.22)\\\",\\\"vm\\\":\\\"{{VM}}\\\"}]}"}'::jsonb,
 NULL,NULL,'{"source":"backend/kb-service/config/category_baseline.yaml","revision":"1.0","checksum":"sha256:4aaa1e4811c5347efe2f270b62eb9a58eb7c7453927ccf5a115af281ebe82b21"}'::jsonb,'{"source":"backend/shared/resolution/catalogs/resolution_catalog.json","revision":"2026-08-13.1","checksum":"sha256:3a85084e74dd6911472e0717a988f9692e59cd279d2a2c80c05a164bd19d5612"}'::jsonb,'sha256:qkv-task-template-v2-10vars','fixture-seed','migration-000007-fixture-assets')
ON CONFLICT (asset_key, revision) DO UPDATE
SET status = 'published',
    content_json = EXCLUDED.content_json,
    content_digest = EXCLUDED.content_digest,
    updated_at = now();

-- 3. 插入“删除虚拟机”场景专属实例 qkv_task.instance.delete_vm (Revision 1, published)
INSERT INTO fixture.asset_revision
    (id, asset_key, asset_type, signal_type, revision, status, content_json, template_asset_key, template_revision, category_baseline_json, catalog_baseline_json, content_digest, created_by, trace_id)
VALUES
('71000000-0000-0000-0000-000000000021','qkv_task.instance.delete_vm','instance','qkv_task',1,'published',
 '{"selection":{"keyword":"删除虚拟机","default":false},"bindings":{"TYPE":"删除虚拟机","DESCRIPTION":"创建回收站目录失败","PROCESS":"完成","ERRCODE_TRACING":"null","REQUEST_ID":",a3a9e0350ab8121dd7ac9fbbe66bea77","HOST":"SVR_aCloud_668","HOSTID":"host-047bcb4bc820","TARGET":"Ubuntu-26.04_import_1","VM":"1114365066966","END":"2026-06-23 22:54:03"}}'::jsonb,
 'qkv_task.template',2,'{"source":"backend/kb-service/config/category_baseline.yaml","revision":"1.0","checksum":"sha256:4aaa1e4811c5347efe2f270b62eb9a58eb7c7453927ccf5a115af281ebe82b21"}'::jsonb,'{"source":"backend/shared/resolution/catalogs/resolution_catalog.json","revision":"2026-08-13.1","checksum":"sha256:3a85084e74dd6911472e0717a988f9692e59cd279d2a2c80c05a164bd19d5612"}'::jsonb,'sha256:qkv-task-instance-delete-vm-10vars','fixture-seed','migration-000007-fixture-assets')
ON CONFLICT (asset_key, revision) DO UPDATE
SET status = 'published',
    content_json = EXCLUDED.content_json,
    template_revision = EXCLUDED.template_revision,
    content_digest = EXCLUDED.content_digest,
    updated_at = now();

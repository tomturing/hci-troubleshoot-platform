-- Bundle Factory 的最小 stdout 资产库。
--
-- 一个表保存模板和实例的不可变修订：模板/实例均携带分类与 Catalog 基线快照，
-- 实例以 (template_asset_key, template_revision) 明确指向模板。这里不恢复已误提交的
-- fixture.bundle_template；旧表若仍存在只作为历史遗留，运行时不再依赖它。

CREATE TABLE IF NOT EXISTS fixture.asset_revision (
    id uuid PRIMARY KEY,
    asset_key varchar(128) NOT NULL,
    asset_type varchar(16) NOT NULL,
    signal_type varchar(64) NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    status varchar(16) NOT NULL DEFAULT 'draft',
    content_json jsonb NOT NULL,
    template_asset_key varchar(128),
    template_revision integer,
    category_baseline_json jsonb NOT NULL,
    catalog_baseline_json jsonb NOT NULL,
    content_digest varchar(71) NOT NULL,
    created_by varchar(128) NOT NULL,
    -- 每行保留创建/最后状态变更调用链，可与 Gateway/Runtime 日志互查。
    trace_id varchar(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fixture_asset_type CHECK (asset_type IN ('template', 'instance')),
    CONSTRAINT fixture_asset_status CHECK (status IN ('draft', 'published', 'retired')),
    CONSTRAINT fixture_asset_signal CHECK (signal_type IN ('qkv_alert', 'qkv_task', 'qkv_dialog')),
    CONSTRAINT fixture_asset_template_ref CHECK (
        (asset_type = 'template' AND template_asset_key IS NULL AND template_revision IS NULL)
        OR (asset_type = 'instance' AND template_asset_key IS NOT NULL AND template_revision IS NOT NULL AND template_revision > 0)
    ),
    CONSTRAINT fixture_asset_revision_unique UNIQUE (asset_key, revision)
);

CREATE INDEX IF NOT EXISTS fixture_asset_lookup_idx
    ON fixture.asset_revision (signal_type, asset_type, status, revision DESC);
CREATE UNIQUE INDEX IF NOT EXISTS fixture_asset_one_published_idx
    ON fixture.asset_revision (asset_key) WHERE status = 'published';

COMMENT ON TABLE fixture.asset_revision IS
    'Bundle Factory stdout 模板/实例的不可变修订；每条记录均含分类、Catalog 基线和调用链。';

-- 分类和 Catalog 基线取自 2026-08-26 已确认的基线快照。stdout_template 是可渲染
-- 模板，bindings 是实例事实；编译器仅以当前 qkv 的 -k/--keyword 覆盖 KEYWORD。
INSERT INTO fixture.asset_revision
    (id, asset_key, asset_type, signal_type, revision, status, content_json, template_asset_key, template_revision, category_baseline_json, catalog_baseline_json, content_digest, created_by, trace_id)
VALUES
('71000000-0000-0000-0000-000000000001','qkv_alert.template','template','qkv_alert',1,'published',
 '{"stdout_template":"{\\\"data\\\":[{\\\"alert_type\\\":\\\"{{ALERT_TYPE}}\\\",\\\"description\\\":\\\"{{DESCRIPTION}}\\\",\\\"object_name\\\":\\\"{{OBJECT_NAME}}\\\",\\\"object_type\\\":\\\"{{OBJECT_TYPE}}\\\",\\\"target\\\":\\\"{{TARGET}}\\\",\\\"start\\\":\\\"{{START}}\\\",\\\"end\\\":\\\"{{END}}\\\",\\\"urgent_type\\\":\\\"{{URGENT_TYPE}}\\\"}]}"}'::jsonb,
 NULL,NULL,'{"source":"backend/kb-service/config/category_baseline.yaml","revision":"1.0","checksum":"sha256:4aaa1e4811c5347efe2f270b62eb9a58eb7c7453927ccf5a115af281ebe82b21"}'::jsonb,'{"source":"backend/shared/resolution/catalogs/resolution_catalog.json","revision":"2026-08-13.1","checksum":"sha256:3a85084e74dd6911472e0717a988f9692e59cd279d2a2c80c05a164bd19d5612"}'::jsonb,'sha256:qkv-alert-template-v1','fixture-seed','migration-000006-fixture-assets'),
('71000000-0000-0000-0000-000000000002','qkv_task.template','template','qkv_task',1,'published',
 '{"stdout_template":"{\\\"data\\\":[{\\\"alert_type\\\":\\\"{{KEYWORD}}\\\",\\\"description\\\":\\\"{{DESCRIPTION}}\\\",\\\"process\\\":\\\"{{PROCESS}}\\\",\\\"status\\\":{{STATUS}},\\\"target\\\":\\\"{{TARGET}}\\\",\\\"start\\\":\\\"{{START}}\\\",\\\"end\\\":\\\"{{END}}\\\",\\\"errcode_tracing\\\":\\\"{{ERRCODE_TRACING}}\\\"}]}"}'::jsonb,
 NULL,NULL,'{"source":"backend/kb-service/config/category_baseline.yaml","revision":"1.0","checksum":"sha256:4aaa1e4811c5347efe2f270b62eb9a58eb7c7453927ccf5a115af281ebe82b21"}'::jsonb,'{"source":"backend/shared/resolution/catalogs/resolution_catalog.json","revision":"2026-08-13.1","checksum":"sha256:3a85084e74dd6911472e0717a988f9692e59cd279d2a2c80c05a164bd19d5612"}'::jsonb,'sha256:qkv-task-template-v1','fixture-seed','migration-000006-fixture-assets'),
('71000000-0000-0000-0000-000000000003','qkv_dialog.template','template','qkv_dialog',1,'published',
 '{"stdout_template":"/sf/log/{{DAY}}/vt/sfvt_vtpdaemon.log:{{END_MS}} err [sfvt_vtpdaemon] {{END}} E {{PID}} QemuServer.pm(VTP::QemuServer::vm_start_error_deal):12936 | [{{TRACE_ROOT}}:{{TRACE_SPAN}}:{{TRACE_SEGMENT}}] [my_die_with_errcode {{ERRCODE}}] message: {{KEYWORD}}（{{VM_NAME}}）失败，错误信息：{{ERROR_MESSAGE}}\\n/sf/log/{{DAY}}/vt/sfvt_vtpdaemon.log:{{CONTEXT_MS}} warning [sfvt_vtpdaemon] {{CONTEXT}} W {{PID}} OpLog.pm((eval)):586 | [{{TRACE_ROOT}}:{{TRACE_SPAN}}:{{CONTEXT_SEGMENT}}] Errcode tracing: {{ERRCODE_TRACE}}, message: {{KEYWORD}}（{{VM_NAME}}）失败，错误信息：{{ERROR_MESSAGE}}"}'::jsonb,
 NULL,NULL,'{"source":"backend/kb-service/config/category_baseline.yaml","revision":"1.0","checksum":"sha256:4aaa1e4811c5347efe2f270b62eb9a58eb7c7453927ccf5a115af281ebe82b21"}'::jsonb,'{"source":"backend/shared/schemas/log_source_catalog.py","revision":"1.2","checksum":"sha256:fff11db3f684629ddcc288e876df478359fe3e2e2ad50bc1ff92b4109e0e6eba"}'::jsonb,'sha256:qkv-dialog-template-v1','fixture-seed','migration-000006-fixture-assets'),
('71000000-0000-0000-0000-000000000011','qkv_alert.instance.sample','instance','qkv_alert',1,'published',
 '{"selection":{"default":true},"bindings":{"ALERT_TYPE":"ha_out_of_resource","DESCRIPTION":"预测性告警：发现HA预留资源不足。当主机出现异常时，可能没有足够的资源去恢复异常主机上所有虚拟机。建议关闭不重要的虚拟机以释放资源，并扩容HA预留资源。","OBJECT_NAME":"SVR_aCloud_668","OBJECT_TYPE":"集群","TARGET":"SVR_aCloud_668","START":"2026-08-26 10:01:02","END":"2026-08-26 10:01:02","URGENT_TYPE":"紧急"}}'::jsonb,
 'qkv_alert.template',1,'{"source":"backend/kb-service/config/category_baseline.yaml","revision":"1.0","checksum":"sha256:4aaa1e4811c5347efe2f270b62eb9a58eb7c7453927ccf5a115af281ebe82b21"}'::jsonb,'{"source":"backend/shared/resolution/catalogs/resolution_catalog.json","revision":"2026-08-13.1","checksum":"sha256:3a85084e74dd6911472e0717a988f9692e59cd279d2a2c80c05a164bd19d5612"}'::jsonb,'sha256:qkv-alert-instance-sample-v1','fixture-seed','migration-000006-fixture-assets'),
('71000000-0000-0000-0000-000000000012','qkv_task.instance.sample','instance','qkv_task',1,'published',
 '{"selection":{"keyword":"启动虚拟机","default":false},"bindings":{"DESCRIPTION":"启动虚拟机（Server-IMG）失败，错误信息：虚拟机镜像忙，正在执行其他操作！","PROCESS":"失败","STATUS":"3","TARGET":"Server-IMG","START":"2026-07-28 00:54:23","END":"2026-07-28 00:54:36","ERRCODE_TRACING":"0x0100186F/0x010015BE/0x010015BE/0x010015BE/0x010015BE/0x01002D46"}}'::jsonb,
 'qkv_task.template',1,'{"source":"backend/kb-service/config/category_baseline.yaml","revision":"1.0","checksum":"sha256:4aaa1e4811c5347efe2f270b62eb9a58eb7c7453927ccf5a115af281ebe82b21"}'::jsonb,'{"source":"backend/shared/resolution/catalogs/resolution_catalog.json","revision":"2026-08-13.1","checksum":"sha256:3a85084e74dd6911472e0717a988f9692e59cd279d2a2c80c05a164bd19d5612"}'::jsonb,'sha256:qkv-task-instance-sample-v1','fixture-seed','migration-000006-fixture-assets'),
('71000000-0000-0000-0000-000000000013','qkv_dialog.instance.sample','instance','qkv_dialog',1,'published',
 '{"selection":{"keyword":"启动虚拟机","default":false},"bindings":{"DAY":"26","END_MS":"2026-08-26 09:45:19.991807","END":"09:45:19.991","CONTEXT_MS":"2026-08-26 09:45:20.330764","CONTEXT":"09:45:20.330","PID":"6955","TRACE_ROOT":"a8e4524c9151ac0956995f05d1289081","TRACE_SPAN":"d41339","TRACE_SEGMENT":"45e4a7","CONTEXT_SEGMENT":"231e62","ERRCODE":"0x0100186F","ERRCODE_TRACE":"0x0100186F/0x010015BE/0x010015BE/0x01002D46","VM_NAME":"Rocky-IMG","ERROR_MESSAGE":"虚拟机镜像忙，正在执行其他操作！"}}'::jsonb,
 'qkv_dialog.template',1,'{"source":"backend/kb-service/config/category_baseline.yaml","revision":"1.0","checksum":"sha256:4aaa1e4811c5347efe2f270b62eb9a58eb7c7453927ccf5a115af281ebe82b21"}'::jsonb,'{"source":"backend/shared/schemas/log_source_catalog.py","revision":"1.2","checksum":"sha256:fff11db3f684629ddcc288e876df478359fe3e2e2ad50bc1ff92b4109e0e6eba"}'::jsonb,'sha256:qkv-dialog-instance-sample-v1','fixture-seed','migration-000006-fixture-assets')
ON CONFLICT (asset_key, revision) DO NOTHING;

# hci_sim migrations

该目录是 `hci_sim` 数据库的唯一 schema 入口，与 `database/atlas-migrations/`（`hci_troubleshoot`）相互独立。迁移 Job 必须显式接收 `HCI_SIM_DATABASE_URL`，禁止回退到平台 `DATABASE_URL`。Helm PreSync 会自动打包该目录下全部 `*.sql` 文件，并按文件名顺序逐个幂等执行；新增迁移只需放入本目录并通过 CI 的渲染校验，禁止再在 Helm 模板或 Job 中手工维护单文件清单。

当前迁移按领域创建 `control_plane`、`fixture`、`artifact`、`audit` 四个 schema。跨数据库引用只保存 `support_id`、KBD revision/checksum 与 digest，不建立外键。`000005` 起，可靠投递统一使用 `control_plane.outbox`；旧 `run_outbox` 与 `stale_outbox` 仅在迁移观察窗口内保留，并通过镜像触发器保障滚动发布不漏事件。

`000006_qkv_fixture_template_instance_seed.sql` 将已确认的 `qkv_alert`、`qkv_task`、`qkv_dialog` 三个模板和三个实例写入现有 `fixture.bundle_template`。这是当前最小化设计下的过渡存储：`template_json.asset_type` 区分模板与实例，并冻结分类基线、Catalog 基线摘要；实例 stdout 保留用户提供的完整字段/日志样例。该迁移可重复执行而不增加记录或版本；它不创建独立 `fixture_library` 领域、实例表或 Bundle Factory 管理页面。

迁移前必须完成源表 inventory、备份/恢复演练和行数/PK/FK/sequence 校验；旧库的 `agent_test_*` 表只有在切换观察窗口结束后，才能由独立 contract 变更删除。

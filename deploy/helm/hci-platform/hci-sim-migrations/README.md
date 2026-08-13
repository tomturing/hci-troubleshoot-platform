# hci_sim migrations

该目录是 `hci_sim` 数据库的唯一 schema 入口，与 `database/atlas-migrations/`（`hci_troubleshoot`）相互独立。迁移 Job 必须显式接收 `HCI_SIM_DATABASE_URL`，禁止回退到平台 `DATABASE_URL`。

当前迁移按领域创建 `control_plane`、`fixture`、`artifact`、`audit` 四个 schema。跨数据库引用只保存 `support_id`、KBD revision/checksum 与 digest，不建立外键。

迁移前必须完成源表 inventory、备份/恢复演练和行数/PK/FK/sequence 校验；旧库的 `agent_test_*` 表只有在切换观察窗口结束后，才能由独立 contract 变更删除。

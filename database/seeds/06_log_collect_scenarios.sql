-- 06：日志收集「故障场景」字典初始数据（seeds 通道，不是 data-migrations）
--
-- 为什么落在 seeds/ 而不是 data-migrations/：
--   `scripts/db-migrate.sh` 的执行顺序是 Step 1 数据迁移 → Step 3 atlas schema apply。
--   存量库路径下 data-migrations 跑在 DDL 之前，而本文件 INSERT 的 log_collect_scenario
--   表恰恰由 Step 3 才创建，因此放进 data-migrations 必然失败：
--     ERROR: relation "log_collect_scenario" does not exist
--   `set -e` 会让整个 Job 中止，Atlas 也永远走不到建表那一步（2026-10-29 staging PreSync
--   实测如此）。seeds/ 由 db-seed Job 在 PostSync（hook-weight 0，晚于 db-migrate 的 -5）
--   执行，此时 Schema 已收敛，表一定存在。
--   规则详见 database/README.md「新表 + 初始数据的落点」。
--
-- 改动可见性（2026-10-29 起）：本文件内容（含注释）变化会让 db-migrate 镜像产生新 tag，
-- 而该 tag 被普通资源 db-migration-trigger 暴露给 ArgoCD 漂移对比，因此必然触发一次真实
-- sync（无需人工 `kubectl patch`）。这就是本次「改一行注释」探针所验证的契约。
--
-- 背景（2026-10-29 客户反馈）：
--   客户在日志收集面板看到「虚拟机挂起专项 / 卡慢盘专项 / 中台专项 / 默认收集专项（不收集上述场景日志）」
--   这一排勾选框，不知道每项到底是什么、该选哪个、能不能多选。根因不是文案写错，而是**平台从来没维护过
--   这层解释**：acli 的 -m 只给出枚举值，bridge 把短名写死在 Go 里，客户前端又抄了一份，两处都只有名字。
--
-- 本次落地的字典是本表的唯一真源：
--   - 可选范围（哪些枚举真的存在）仍由集群 `acli log_collect create --help` 探测决定，本表不越权；
--   - 本表只提供「叫什么 / 收什么 / 什么症状选它 / 大概多久 / 是否互斥」。
--
-- 内容来源与边界（必须保留，避免后来者把草稿当事实）：
--   1. label 取自 acli 1.0.1 help 原文（与 terminal_bridge/log_collect.go 的 logCollectModuleLabels 一致）；
--   2. collects / typical_symptoms 是**平台侧按专项名的中文起草**，acli 没有对应字段，
--      因此 review_state 一律为 pending_review，需支持工程师审核后由管理端改为 approved 再对外；
--   3. duration_hint 只有 slowdisk 有 acli 原文依据（「单天收集时间约4min」），其余留空，不编造；
--   4. exclusive：本列保留为字典能力（true = 选中它必须清空其它项），但当前**无一项启用**。
--      原因：支持工程师 2026-10-29 实机确认 `-m other,vm_suspend` 是**叠加**语义（两类日志
--      一起收），而不是互斥；若当初按 help 文字“不收集上述场景日志”强制互斥，会把客户合法的
--      叠加诉求挡掉。将来真出现互斥场景，在管理台把该项打开即可，不需要改代码。
--
-- 唯一调用链标记：log_collect_scenario_seed_v1
-- 幂等：ON CONFLICT (value) DO NOTHING —— 已存在的行（含运营已改过的文案）绝不被覆盖。
-- 与 02_system_prompts.sql / 03_skill_definitions.sql 同一策略：种子只在首次铺底，
-- 运营在管理台改过的文案/审核状态不会在每次 Sync 时被回滚。

BEGIN;

INSERT INTO log_collect_scenario
    (value, label, summary, collects, typical_symptoms, duration_hint, exclusive, sort_order, enabled, review_state)
VALUES
    ('vm_suspend', '虚拟机挂起专项',
     '虚拟机挂起、恢复操作卡住或长时间无响应',
     '采集与虚拟机挂起/恢复相关的主机侧日志与任务记录',
     '挂起操作长时间不返回、恢复后虚拟机异常、挂起次数异常',
     NULL, false, 10, true, 'pending_review'),

    ('vm_start', '虚拟机开机失败专项',
     '虚拟机开机报错或开机过程卡住',
     '采集与虚拟机开机相关的主机侧日志与任务记录',
     '开机失败、开机卡在某个百分比、批量开机大量失败',
     NULL, false, 20, true, 'pending_review'),

    ('host_offline', '主机离线专项',
     '集群内节点掉线、失联或反复离线',
     '采集节点存活、心跳与管理网络相关日志',
     '节点状态变为离线、节点反复掉线、集群告警主机失联',
     NULL, false, 30, true, 'pending_review'),

    ('network', '网络专项',
     '虚拟机或主机网络不通、丢包、时延异常',
     '采集网络转发、虚拟交换机与连通性相关日志',
     'ping 不通、时延抖动、跨主机通信失败、网络配置变更后异常',
     NULL, false, 40, true, 'pending_review'),

    ('storage', '虚拟存储专项',
     '虚拟存储卷读写异常、容量或副本问题',
     '采集虚拟存储相关日志与存储任务记录',
     '虚拟机 IO 报错、存储卷不可用、容量异常、副本重建中',
     NULL, false, 50, true, 'pending_review'),

    ('slowdisk', '卡慢盘专项',
     '磁盘响应慢，虚拟机整体卡顿',
     '采集磁盘时延与 IO 相关日志',
     '虚拟机卡慢、IO 读写慢、磁盘告警、任务长时间无进展',
     '单天收集时间约 4 分钟', false, 60, true, 'pending_review'),

    ('performance', '性能专项',
     'CPU、内存或整体性能指标异常',
     '采集性能相关指标与主机侧日志',
     'CPU/内存长期高位、虚拟机卡慢但无明确报错、性能告警',
     NULL, false, 70, true, 'pending_review'),

    ('core', 'core 文件专项',
     '组件崩溃并产生 core 文件',
     '采集 core 文件及其对应的进程崩溃信息',
     '服务异常重启、进程崩溃、目录中出现 core 文件',
     NULL, false, 80, true, 'pending_review'),

    ('system', '中台专项',
     '中台（管理平台）自身异常',
     '采集管理平台各组件的运行日志',
     '登录/页面异常、任务下发失败、平台服务重启、升级后异常',
     NULL, false, 90, true, 'pending_review'),

    ('other', '默认收集专项（不收集上述场景日志）',
     '不属于以上任何场景时的兜底采集，可与具体专项叠加',
     '只采集默认日志集合，不包含上述各专项对应的场景日志',
     '故障类别尚不明确、需要先拿一份通用日志',
     NULL, false, 100, true, 'pending_review')
ON CONFLICT (value) DO NOTHING;

COMMIT;

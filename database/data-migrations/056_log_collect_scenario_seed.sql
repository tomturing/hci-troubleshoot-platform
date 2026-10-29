-- 056：日志收集「故障场景」字典初始数据
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
--   4. exclusive：other 的 help 原文是「不收集上述n个场景日志」，与具体专项同选语义自相矛盾，
--      故置 true 由客户端强制互斥。若实机确认 -m other,vm_suspend 为叠加语义，改这一列即可，无需改代码。
--
-- 唯一调用链标记：log_collect_scenario_seed_v1
-- 幂等：ON CONFLICT (value) DO NOTHING —— 已存在的行（含运营已改过的文案）绝不被覆盖。

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
     '不属于以上任何场景时的兜底采集',
     '只采集默认日志集合，不包含上述各专项对应的场景日志',
     '故障类别尚不明确、需要先拿一份通用日志',
     NULL, true, 100, true, 'pending_review')
ON CONFLICT (value) DO NOTHING;

COMMIT;

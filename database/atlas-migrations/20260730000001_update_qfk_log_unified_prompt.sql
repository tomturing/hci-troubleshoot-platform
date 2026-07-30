-- 将部署中 kbd_extract_signals_v2 收口到统一 qfk_log 与真实 aCLI 契约。
-- 采用追加的高优先级规则，兼容不同环境中已有 Prompt 的历史修订内容。
UPDATE system_prompt
SET content_template = content_template || $RULE$

# 补充规则 19：统一 qfk_log、qkv_dialog 与真实 aCLI 边界（高优先级）
- 不存在独立 qfk_blackbox。/sf/log 下 whitebox、blackbox、vn-blackbox 与 pods 日志统一生成 qfk_log；/sf/data/local 不是日志族，仅可携带 request_id 做辅助关联搜索。
- 常规 qfk_log.file 必须是安全 basename，禁止目录分隔符；常规日志 path 仅限 /sf/log，省略时由日志源 Catalog 推断。/sf/data/local 不是日志族，只允许 request_id 辅助关联。BMC SEL、页面操作记录、NBU 与外部存储日志不是本机 qfk_log。
- qfk_log 可选 source_family=auto|whitebox|blackbox|vn_blackbox|pod、parser、request_id、context_lines。time_window 只允许 HCI 时区绝对时间或 {{{{ABSOLUTE_TIME}}}}；now/-1h 必须由 Agent 先解析。
- 普通文本使用 keyword/regex/state/exists；数值阈值使用 threshold；周期采样变化使用 delta/trend 并填写 metric。blackbox 时间戳中的数字不得当作指标值。
- include_archives=true 仅在确认目标日期、路径范围与磁盘空间后使用，并同时设置 archive_precheck=verified。
- qkv_dialog 不生成虚构的 acli dialog get；无任务/告警承载的纯弹框，在当前主控 /sf/log/today 与 /sf/log/today/vt 检索原文并产出 END、REQUEST_ID、HOST。存在失败任务时优先 qkv_task。弹框文本不稳定或无法定位日志时标 needs_review，不得猜测变量。
$RULE$,
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%补充规则 19：统一 qfk_log、qkv_dialog 与真实 aCLI 边界%';

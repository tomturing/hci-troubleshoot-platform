-- 统一 KBD Signal 的候选过滤、取值和输出语义。
--
-- 只更新动态 Prompt；历史 KBD/revision 保持不可变。服务端 Schema/运行时仍兼容
-- expected/not 与 qfk_service resource_keyword/command，模型从本版本起只生成新契约。

UPDATE system_prompt
SET content_template = replace(
        replace(
            replace(
                replace(
                    content_template,
                    '"mode": "or|and|not", "expected": true',
                    '"mode": "or|and", "expected": true'
                ),
                '所有 qkv_ 和 qfk_ 信号的 timeout 默认写为 120',
                '所有 qkv_ 和 qfk_ 信号的 timeout 默认写为 60'
            ),
            'qfk_service：领域服务域为 asv(vt)/anet(vn)/asan(vs)/host；当前版本 `acli service --help` 已验证可执行组为 asv/anet/host，生成命令必须取运行时能力交集。resource_keyword 为服务名，container 为已探测组（默认 asv）；Signal 的 command 仅允许 status 等只读检查命令。',
            'qfk_service：领域服务域为 asv(vt)/anet(vn)/asan(vs)/host；当前版本 `acli service --help` 已验证可执行组为 asv/anet/host，生成命令必须取运行时能力交集。service 为服务名，container 为已探测组（默认 asv）；action 只允许只读 status。'
        ),
        '产出变量模式必须用 resource_keyword 或 request_id 限制输出。',
        '产出变量模式必须用非空 produces.extract.rows.include 或 request_id 限制输出；exclude 单独存在不构成有界查询。'
    ) || $RULE$

# 补充规则 26：统一候选过滤、取值与输出（最高优先级）
- QFK 统一管道为：输入/采集 → 候选记录过滤 → 完整行/文本行列/JSON 路径取值 → 可选 AI 提取 → Match 或 Produce。keyword 不作为所有 QFK 的公共命令参数；qfk_system 的关键字只属于输出过滤。
- 文本候选过滤唯一写在 extract.rows。scope 固定 same_record；include_mode 与 exclude_mode 分别只允许 all/any。最终 selected=include_ok 且未触发 exclude；跨行分别出现 A/B 不能满足 AND。多个关键字使用数组，页面按换行编辑，逗号属于字面量。
- “完整行”指候选过滤后保留整行、不截列，不等于无界整份 stdout。qfk_log Produce 必须有非空 rows.include 或 request_id；exclude 单独存在不构成有界查询。
- AI 提取只能在确定性取值后的候选完整行中摘取逐字可回查的值；不能代替 Match Predicate。Produce 的 AI 结果通过溯源校验后才写变量池。
- qfk_service 新字段使用 service/action，action 只能是 status；不得生成 start/stop/restart。历史 resource_keyword/command 只由运行时兼容读取。
- 新生成或缺省 timeout 使用 60 秒；有明确、可审计耗时依据时可以显式选择其他 1-300 秒值。
- 新 keyword Matcher 的 mode 只使用 or/and 且 expected=true，绝不可把 any 或 all 写入 match.mode。历史 expected=false/not 由运行时兼容，不继续生成新的通用取反组合。
$RULE$,
    version = '2.2',
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%补充规则 26：统一候选过滤、取值与输出%';

DO $verify$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM system_prompt
        WHERE name = 'kbd_extract_signals_v2'
    ) THEN
        RAISE NOTICE '未加载 kbd_extract_signals_v2 Prompt，跳过空库契约断言';
    ELSIF NOT EXISTS (
        SELECT 1
        FROM system_prompt
        WHERE name = 'kbd_extract_signals_v2'
          AND version = '2.2'
          AND content_template LIKE '%补充规则 26：统一候选过滤、取值与输出%'
          AND content_template LIKE '%scope 固定 same_record%'
          AND content_template LIKE '%action 只能是 status%'
          AND content_template LIKE '%timeout 使用 60 秒%'
    ) THEN
        RAISE EXCEPTION 'kbd_extract_signals_v2 统一过滤取值输出 Prompt 升级未生效';
    END IF;
END
$verify$;

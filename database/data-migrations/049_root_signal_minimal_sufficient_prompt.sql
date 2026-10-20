-- ============================================================================
-- 049_root_signal_minimal_sufficient_prompt.sql
-- 描述：从 Prompt 侧给 kbd_extract_root_signal_v2 追加【最少充分集】约束段 ——
--       充分性（每条判据都必须能排除至少一个竞争案例）、最少性（任一条拿掉即丢判别力）、
--       多条必须是缺一不可的链、谓词类型限定为 keyword。
-- 问题：根因信号 v2 现只要求「每条能确证根因 + 能区分同分类其他 KBD」，缺少集合层面的
--       最少性约束，模型倾向「多抽几条保险」：同一环节的上下游现象与结论各输出一条、
--       更强判据已覆盖的弱判据重复输出，直接抬高人工审核量与现场采集成本。
--       本条与 048（关键信号 null/XOR 加固）同一防御模式：生成侧约束 + 服务端门禁
--       （gate_sufficiency/gate_minimality）双保险，Prompt 约束非确定性，漏网由门禁兜底。
-- 影响：仅 kbd_extract_root_signal_v2 一行；占位符契约不变（追加文本零花括号，
--       不新增 {name}，平台以 str.format 渲染，任何裸花括号都会抛 KeyError）。
--       version 1.0 -> 2.0，与运行时准入 gate_version>=2 语义对齐（手工可辨别）。
-- 幂等性：仅当尚未含【最少充分集】标记时追加；可重复执行。
--       空库/CI 未加载该 prompt（seed 未应用）时优雅跳过，由
--       database/seeds/02_system_prompts.sql 的新环境种子提供同等约束。
-- 唯一调用链：migration:20261020000001
-- ============================================================================

DO $$
BEGIN
    RAISE NOTICE '开始执行 049：为 kbd_extract_root_signal_v2 追加【最少充分集】约束...';

    IF NOT EXISTS (SELECT 1 FROM system_prompt WHERE name = 'kbd_extract_root_signal_v2') THEN
        -- 空库/CI 未加载该 prompt seed：由已含该约束的 database/seeds/02_system_prompts.sql 提供，优雅跳过。
        RAISE NOTICE '未加载 kbd_extract_root_signal_v2 Prompt，跳过（trace_id=migration:20261020000001）';
    ELSE
        -- 追加【最少充分集】段（幂等：不含标记才追加）。追加文本零花括号，不引入新占位符。
        UPDATE system_prompt
        SET content_template = content_template || $RULE$

# 【最少充分集】（决定输出几条的唯一标准，优先级高于「多抽几条保险」）
根因信号的条数目标不是「越多越好」也不是「越少越好」，而是「能完整判断命中本 KBD 根因的最少信号数量」：一条够就只输出一条；确需多条时必须是缺一不可的链。
1. 充分性（每条都必须有判别力）：逐条自问——这条信号能否排除至少一个「同分类下的其他 KBD」或「健康态」？若它在健康机器和兄弟案例上都成立（如「磁盘存在」「网络正常」「服务在运行」「版本号是 6.x」），它排除不了任何别的情况，必须删除。
2. 最少性（每条都必须不可替代）：逐条自问——把这条拿掉，其余信号是否还能覆盖它原本排除的情况？能则它冗余，必须删除。典型冗余：同一环节的上下游两句（现象一句、结论一句）、同义改写、更强判据完全包含更弱判据（已有「分区 /run/lock 使用率 100%」，就不要再写「/run/lock 存在」）。
3. 确需多条时必须是链：只有单条无法排除全部竞争案例、必须多条缺一不可地串联才能定位根因时才输出多条；这些信号必须落在根因判定链的不同环节（如「网卡协商速率仅 100M」+「该网卡是 bond 成员」+「bond 仅剩 1 个成员」，缺任一条都定不到根因）。绝不把同一步骤的多个观察点拆成多条凑数。
4. 谓词类型限定：match.type 一律填 keyword（用采集输出中可逐字命中的字符串表达判据）；禁止 threshold / state / boolean / exists / range 等数值或布尔谓词，运行时无法求值，会被整条拒收。
$RULE$,
            version = '2.0',
            updated_at = NOW()
        WHERE name = 'kbd_extract_root_signal_v2'
          AND content_template NOT LIKE '%【最少充分集】%';

        -- verify 门禁：行存在则必须已含最少充分集段，且未因此引入裸花括号占位符，否则 fail-loud 回滚。
        IF EXISTS (
            SELECT 1 FROM system_prompt
            WHERE name = 'kbd_extract_root_signal_v2'
              AND content_template NOT LIKE '%【最少充分集】%'
        ) THEN
            RAISE EXCEPTION 'migration:20261020000001 未生效：kbd_extract_root_signal_v2 缺少【最少充分集】段';
        END IF;

        IF EXISTS (
            SELECT 1 FROM system_prompt
            WHERE name = 'kbd_extract_root_signal_v2'
              AND content_template NOT LIKE '%{title}%'
        ) THEN
            RAISE EXCEPTION 'migration:20261020000001 破坏占位符契约：kbd_extract_root_signal_v2 丢失 {title}（str.format 渲染会失去输入）';
        END IF;

        RAISE NOTICE '✓ 完成 049：kbd_extract_root_signal_v2 已追加最少充分集约束（trace_id=migration:20261020000001）';
    END IF;
END $$;

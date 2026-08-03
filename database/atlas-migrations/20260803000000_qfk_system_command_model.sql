-- 收敛 qfk_system 的命令表达，避免 LLM 再生成 resource_keyword 并将 VM ID 追加给 lsof。
-- 历史 KBD 数据不在这里猜测性改写；包含该旧字段的 revision 必须由专家审核后重新发布。

UPDATE system_prompt
SET content_template = content_template || $RULE$

# 补充规则 20：qfk_system 唯一命令模型与 producer 语义（高优先级）
- qfk_system 只能使用一个命令模型：command 为基础子命令，普通参数唯一写入 command_args 数组。例如 `ps -p {{PID}} -o cmd=` 必须生成 `command="ps", command_args=["-p","{{PID}}","-o","cmd="]`。
- qfk_system 禁止 resource_keyword。尤其禁止把 VM ID 作为 lsof 参数；`lsof` 必须执行基础命令，并由 produces.extract.rows.include 在受控输出筛选 VM 行。
- qfk_system 的 producer（match=null 且 produces 非空）只负责从成功输出提取变量，不配置 matcher；其下游判定信号再使用精确进程身份（例如 ClwDRDBClient），不得用 VM ID 代替进程身份。
- lsof 是高输出命令，必须显式 timeout；虚拟机镜像占用场景使用 timeout=120。
$RULE$,
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%补充规则 20：qfk_system 唯一命令模型与 producer 语义%';

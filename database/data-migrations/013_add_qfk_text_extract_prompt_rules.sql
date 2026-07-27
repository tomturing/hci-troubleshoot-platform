-- 为 KEY 阶段 Prompt 增加 QFK 非 JSON 行列提取与安全管道转换规则。
-- 幂等：通过规则标题去重；只更新现有 kbd_extract_signals_v2。

UPDATE system_prompt
SET content_template = content_template || $RULE$

# 补充规则 12：非 JSON 行列提取与 Shell 管道安全边界
- backend（qfk_*）严格二选一：判定模式配置 match；产出变量模式配置 match=null 与 produces。
- command 只能保存基础命令，禁止包含管道符。grep/awk/cut 安全子集必须转为 produces[].extract。
- grep PATTERN / grep -e PATTERN / grep -F PATTERN → include；grep -v PATTERN → exclude；grep -i → case_sensitive=false。
- awk '{{print $N}}' → column=N,column_mode=index；cut -dX -fN → delimiter=X,column=N。
- grep -v grep 直接删除，因为平台只在内存筛选 stdout，不启动 grep 进程。
- 复杂 awk、sed、sort、聚合、正则歧义或未知管道不得猜测；保留 evidence 并标 provenance.needs_review=true。
- 文本产出示例：{{"name":"KVM_PID","type":"integer","extract":{{"type":"text","include":["-id {{{{VM}}}}"],"column":2,"column_mode":"index"}}}}。
- requires 由 acquire.args 与 extract 中的 {{{{VAR}}}} 占位符自动推导，无需人工重复维护。
$RULE$,
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%补充规则 12：非 JSON 行列提取与 Shell 管道安全边界%';

"""
Agent Service 核心公共工具函数
"""


def smart_truncate(output: str, max_chars: int) -> str:
    """
    智能截断长文本，优先保留包含关键错误信息的行。
    保留首部 20% 和尾部 20% 作为上下文，其余部分用于筛选包含错误关键字的行。

    Args:
        output: 原始命令输出字符串
        max_chars: 最大允许字符长度

    Returns:
        智能截断后的字符串
    """
    if not output or len(output) <= max_chars:
        return output

    lines = output.splitlines(keepends=True)
    if not lines:
        return output[:max_chars]

    # 计算目标段的字符长度限制
    head_limit = int(max_chars * 0.2)
    tail_limit = int(max_chars * 0.2)
    # 留出 150 字符的缓冲空间用于存放提示信息
    middle_limit = max_chars - head_limit - tail_limit - 150
    if middle_limit < 0:
        middle_limit = 0

    # 1. 提取头部上下文（从首行向后累加）
    head_lines = []
    head_len = 0
    head_idx = 0
    while head_idx < len(lines) and head_len + len(lines[head_idx]) <= head_limit:
        head_lines.append(lines[head_idx])
        head_len += len(lines[head_idx])
        head_idx += 1

    # 2. 提取尾部上下文（从尾行向前累加）
    tail_lines = []
    tail_len = 0
    tail_idx = len(lines) - 1
    while tail_idx >= head_idx and tail_len + len(lines[tail_idx]) <= tail_limit:
        tail_lines.insert(0, lines[tail_idx])
        tail_len += len(lines[tail_idx])
        tail_idx -= 1

    # 3. 在中间未包含在首尾上下文的行中，筛选包含错误关键字的行
    error_patterns = ["error", "fail", "exception", "critical", "fatal", "panic"]
    middle_lines = lines[head_idx : tail_idx + 1]

    error_lines = []
    current_middle_len = 0
    last_added_idx = -1

    for idx, line in enumerate(middle_lines):
        line_lower = line.lower()
        if any(pat in line_lower for pat in error_patterns) and (current_middle_len + len(line) + 30 <= middle_limit):
            # 若前一次添加的行不是连续的，则加入截断标识
            if last_added_idx != -1 and idx > last_added_idx + 1:
                trunc_marker = "... [此处截断了若干行] ...\n"
                error_lines.append(trunc_marker)
                current_middle_len += len(trunc_marker)
            error_lines.append(line)
            current_middle_len += len(line)
            last_added_idx = idx

    # 4. 拼接最终结果
    parts = []
    if head_lines:
        parts.extend(head_lines)
        parts.append("... [此处截断，保留头部上下文] ...\n")

    if error_lines:
        parts.append("--- [过滤保留的关键错误行] ---\n")
        parts.extend(error_lines)
        parts.append("----------------------------\n")
    else:
        parts.append("... [此处截断了中间的非关键输出] ...\n")

    if tail_lines:
        parts.append("... [此处截断，保留尾部上下文] ...\n")
        parts.extend(tail_lines)

    truncated_result = "".join(parts)

    # 双重保险：如果因额外标记字符导致仍然超出限制，执行强制截断
    if len(truncated_result) > max_chars:
        return truncated_result[:max_chars]
    return truncated_result

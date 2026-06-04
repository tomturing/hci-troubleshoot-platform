"""
内置 Skill 注册与执行中心
"""

import fnmatch
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# 全局技能注册表
_SKILL_REGISTRY: dict[str, Callable[[dict[str, Any]], Any]] = {}


def register_skill(name: str) -> Callable:
    """技能注册装饰器"""

    def decorator(func: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
        _SKILL_REGISTRY[name] = func
        return func

    return decorator


async def execute_skill(skill_name: str, context_variables: dict[str, Any]) -> Any:
    """执行指定名称的技能"""
    if skill_name not in _SKILL_REGISTRY:
        raise ValueError(f"未注册的技能: {skill_name}")

    func = _SKILL_REGISTRY[skill_name]
    logger.info(f"开始执行技能: {skill_name}")
    try:
        # 兼容同步与异步技能执行
        if callable(func):
            import inspect

            if inspect.iscoroutinefunction(func):
                result = await func(context_variables)
            else:
                result = func(context_variables)
            logger.info(f"技能 {skill_name} 执行成功，结果为: {result}")
            return result
        else:
            raise ValueError(f"技能 {skill_name} 的实现不是有效的函数")
    except Exception as exc:
        logger.error(f"技能 {skill_name} 执行失败: {exc}", exc_info=True)
        raise exc


def match_vendor(model_name: str) -> str | None:
    """根据型号名称匹配硬盘厂商"""
    model_lower = model_name.lower()
    patterns = {
        "kioxia_toshiba": ["kcm*", "kpm*", "krm*", "kcd*", "kxd*", "kfl*", "klc*", "khk*", "*kioxia*", "*toshiba*"],
        "intel": ["ssdsc*", "ssdpe*", "ssdpf*", "mdtpe*", "*intel*"],
        "samsung": ["mz*", "*samsung*"],
        "micron": ["mtfd*", "*micron*"],
        "transcend": ["ts*", "*transcend*"],
        "hikvision": ["hs-ssd*", "*hikvision*"],
        "datang": ["sts*", "dts*", "datssd*"],
        "huawei": ["hwe*", "hssd*", "*huawei*"],
        "longsys": ["rsye*", "*longsys*"],
        "foresee": ["fi*", "*foresee*"],
        "liteon_ssstc": ["liteon*", "*ssstc*"],
    }
    for vendor, pat_list in patterns.items():
        for pat in pat_list:
            if fnmatch.fnmatchcase(model_lower, pat.lower()):
                return vendor
    return None


def parse_smart_attributes(smart_info: str) -> dict[int, dict]:
    """解析 SMART 属性表"""
    attrs = {}
    for line in smart_info.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 6 and parts[0].isdigit():
            try:
                attr_id = int(parts[0])
                val = int(parts[3])
                raw_str = parts[-1]
                # 提取 raw_str 中的数字
                raw_digits = "".join(c for c in raw_str if c.isdigit())
                raw_val = int(raw_digits) if raw_digits else 0
                attrs[attr_id] = {"value": val, "raw": raw_val, "raw_str": raw_str}
            except Exception:
                continue
    return attrs


@register_skill("disk_vendor_lifetime")
def disk_vendor_lifetime(context_variables: dict[str, Any]) -> str:
    """硬盘厂商识别与寿命判定技能"""
    smart_info = context_variables.get("smart_info")
    if not smart_info or not isinstance(smart_info, str) or not smart_info.strip():
        raise ValueError("缺少或空的 'smart_info' 变量，无法执行寿命判定")

    # 提取型号
    model = ""
    for line in smart_info.splitlines():
        line_lower = line.lower()
        if "device model:" in line_lower or "model number:" in line_lower or "product:" in line_lower:
            model = line.split(":", 1)[1].strip()
            break

    if not model:
        # 如果没有找到明确的型号，尝试看前 10 行有没有类似信息，或者使用整段文本匹配（降级方案）
        model = smart_info[:200]  # 限制长度以防过长

    vendor = match_vendor(model)
    if not vendor:
        logger.warning(f"无法识别硬盘型号为任何已知厂商的 SATA/SAS 硬盘: {model}")
        return "正常"

    attrs = parse_smart_attributes(smart_info)

    if vendor == "kioxia_toshiba":
        # Rule: SMART 第 173 项 (VALUE 字段) < 100 -> 返修
        if 173 in attrs and attrs[173]["value"] < 100:
            return "返修"
    elif vendor == "intel":
        # Rule: SMART 第 233 项 (VALUE 字段) <= 10 -> 返修
        if 233 in attrs and attrs[233]["value"] <= 10:
            return "返修"
    elif vendor == "samsung":
        # Rule: SMART 第 177 项 (VALUE 字段) <= 10 -> 返修
        if 177 in attrs and attrs[177]["value"] <= 10:
            return "返修"
    elif vendor == "micron":
        # Rule: SMART 第 202 项 (VALUE 字段) <= 10 -> 返修
        if 202 in attrs and attrs[202]["value"] <= 10:
            return "返修"
    elif vendor == "transcend":
        # Rule: SMART 第 167 项 (RAW_VALUE 字段) >= 2700 -> 返修
        if 167 in attrs and attrs[167]["raw"] >= 2700:
            return "返修"
    elif vendor == "hikvision":
        # Rule: SMART 第 233 项 (RAW_VALUE 字段) >= 90 -> 返修
        if 233 in attrs and attrs[233]["raw"] >= 90:
            return "返修"
    elif vendor == "datang":
        # Rule: SMART 第 233 项 (VALUE 字段) <= 10 -> 返修
        if 233 in attrs and attrs[233]["value"] <= 10:
            return "返修"
    elif vendor == "huawei":
        # Rule: SMART 第 231 项 (VALUE 字段) <= 10 -> 返修
        if 231 in attrs and attrs[231]["value"] <= 10:
            return "返修"
    elif vendor == "longsys":
        # Rule: SMART 第 202 项 (VALUE 字段) <= 10 -> 返修
        if 202 in attrs and attrs[202]["value"] <= 10:
            return "返修"
    elif vendor == "foresee":
        # Rule: SMART 第 167 项 (RAW_VALUE 字段) >= 2700 -> 返修
        if 167 in attrs and attrs[167]["raw"] >= 2700:
            return "返修"
    elif vendor == "liteon_ssstc":
        # Rule: 优先使用 SMART 第 202 项 (VALUE 字段) <= 10 -> 返修；若无 202 项，则使用 177 项 (VALUE 字段) <= 10 -> 返修。
        if 202 in attrs:
            if attrs[202]["value"] <= 10:
                return "返修"
        elif 177 in attrs and attrs[177]["value"] <= 10:
            return "返修"

    return "正常"

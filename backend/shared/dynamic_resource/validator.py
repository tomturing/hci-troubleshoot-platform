"""动态资源通用校验。"""

from __future__ import annotations

from typing import Any

from .models import ValidationIssue, ValidationResult


class DynamicResourceValidator:
    """跨资源通用校验器。"""

    @staticmethod
    def validate_dependencies(dependencies: list[dict[str, Any]]) -> ValidationResult:
        """校验依赖声明基本结构。存在性校验由各服务结合本地表完成。"""
        issues: list[ValidationIssue] = []
        for idx, dep in enumerate(dependencies):
            location = f"dependency_json[{idx}]"
            if not isinstance(dep, dict):
                issues.append(
                    ValidationIssue("error", location, "依赖项必须是对象", "DEPENDENCY_NOT_OBJECT")
                )
                continue
            if not dep.get("resource_type"):
                issues.append(
                    ValidationIssue("error", f"{location}.resource_type", "依赖缺少 resource_type", "DEPENDENCY_TYPE_MISSING")
                )
            if not dep.get("resource_name"):
                issues.append(
                    ValidationIssue("error", f"{location}.resource_name", "依赖缺少 resource_name", "DEPENDENCY_NAME_MISSING")
                )

        if any(issue.level == "error" for issue in issues):
            return ValidationResult(status="error", issues=issues)
        if issues:
            return ValidationResult(status="warning", issues=issues)
        return ValidationResult.ok()

    @staticmethod
    def validate_prompt_placeholders(
        actual_placeholders: set[str],
        expected_placeholders: set[str],
        *,
        resource_name: str,
    ) -> ValidationResult:
        """校验 Prompt 占位符契约。"""
        issues: list[ValidationIssue] = []
        for name in sorted(expected_placeholders - actual_placeholders):
            issues.append(
                ValidationIssue(
                    "error",
                    "content_template",
                    f"Prompt {resource_name} 缺少运行时必需的占位符 {name}",
                    "PROMPT_PLACEHOLDER_MISSING",
                )
            )
        for name in sorted(actual_placeholders - expected_placeholders):
            issues.append(
                ValidationIssue(
                    "error",
                    "content_template",
                    f"Prompt {resource_name} 包含运行时无法识别的非法占位符 {name}",
                    "PROMPT_PLACEHOLDER_UNKNOWN",
                )
            )
        return ValidationResult(status="error", issues=issues) if issues else ValidationResult.ok()

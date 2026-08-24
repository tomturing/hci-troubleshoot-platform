"""qkv_effect 条件型效果验证生产者在线执行包。

本包是在线路径的唯一执行入口：KBD 差异诊断遇到 qkv_effect 信号时必须路由到
`adapter.run_effect_verification_signal`，绝不落入自由文本 qkv_exec 路径。
观测全部委派已批准的只读采集原语（封闭通道集合），判定规则是封闭 matcher
集合；执行层受 `EFFECT_VERIFICATION_ENABLED` 策略开关控制（默认关闭）。
"""

from app.tools.effect.adapter import EffectVerificationResult, run_effect_verification_signal

__all__ = ["EffectVerificationResult", "run_effect_verification_signal"]

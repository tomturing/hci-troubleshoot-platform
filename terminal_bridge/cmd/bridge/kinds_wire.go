package main

// kinds_wire.go 是节点同步链路的登记接线：装配层是唯一知道"哪条链路由谁实现"的地方。
//
// 为什么必须有人登记：骨架（reconcile）刻意不认识消费者，于是"Kind 定义了但没有
// 真正接入"在编译期完全看不出来——SPC 就是这样只租了持久意图、写入自管了半年。
// 登记之后，接入程度成为可枚举、可断言的事实（守卫见 kinds_wire_test.go）。

import (
	"terminal_bridge/internal/shared/reconcile"
	"terminal_bridge/internal/shared/reconcile/kinds"
)

func installKinds() {
	for _, d := range kindsDescriptors() {
		kinds.MustRegister(d)
	}
}

// kindsDescriptors 是链路声明的单一来源：装配与守卫测试共用，
// 避免"测试写一份、装配写一份"两处漂移。
func kindsDescriptors() []kinds.Descriptor {
	return []kinds.Descriptor{
		// acli 二进制：无账本链路。期望态来自官方源探测（版本 + 打包日期），
		// 节点事实是唯一真相，因此只租用三态判定。
		{
			Kind:    reconcile.KindAcliBinary,
			Mode:    kinds.ModeCompareOnly,
			Owner:   "acli",
			Explain: "期望态=官方源探测到的版本/日期；与节点 `acli --version` 比对后决定是否重装（无账本）",
			Note: "不落账本：节点被第三方清空后必须重新探测，靠账本 Skip 会永久跳过；" +
				"另声明了 InstallSpec{acli_install} 与 acli 域内渲染器，但编排未走 Run（见事件文档 2026-11-22）。" +
				"包复用/断点由 acli 域内实现（findRemoteAcliPackage/verifyReusedRemotePackage），" +
				"骨架不承担——扩骨架的决策门见 docs/plans/2026-11-23-S4-W4骨架契约反推清单.md",
		},
		// 配置下发：走 Provider → DesiredState → Plan → Run → Verify 全流程。
		{
			Kind:    reconcile.KindConfigFile,
			Mode:    kinds.ModePlanActivate,
			Owner:   "logcollect",
			Explain: "期望态=WS 下发的配置原文；按任务隔离写入节点固定路径，摘要校验后回执",
			Note:    "前端是不可信链路，Provider 必须二次校验并 fail-closed",
		},
		// 场景清单：多槽位文件，写入脚本自管（目录重建 + 写前自愈 + 备份保真 cmp + 回读校验）。
		//
		// 这是目前唯一"没走骨架编排"的写入链路，债务在这里显式登记而不是留给人考古。
		{
			Kind:    reconcile.KindSpcSlot,
			Mode:    kinds.ModeIntentOnly,
			Owner:   "logcollect",
			Explain: "期望态=平台签发的逐槽位清单；写入节点 spc 目录，采集结束后按持久意图恢复原件",
			Note:    "写入未接 Plan/Activate：自愈顺序（先放回历史备份再备份）与 cmp 保真校验骨架尚未表达，硬接会破坏 golden 命令序列",
		},
		// 插件目录：DesiredState 只充当"安装后落盘校验"的目标清单（目录非空 / 文件可执行）。
		// 注意：这是**只读校验**，不是插件同步；同步收敛见下面的 KindPluginSet。
		{
			Kind:    reconcile.KindPluginBundle,
			Mode:    kinds.ModeDeclarative,
			Owner:   "acli",
			Explain: "期望态=内置插件目录树 + log_collect 可执行入口；逐节点探测落盘结果",
			Note:    "空目录必须判失败（Q2026111700614：47 个空目录被\"安装成功\"掩盖）；本链路只校验，不做任何写入",
		},
		// 插件同步收敛：域内自持 Observe/Compare/Act/Receipt，未走骨架编排。
		//
		// 这条之前被误记在 KindPluginBundle 上（那个 Kind 实际只做安装后落盘校验），
		// 结果是"实际能力比登记的强"——与 SPC 的 intent_only（登记比实际强）方向相反、
		// 危害相同：没人能从登记册判断该看哪儿。
		{
			Kind:    reconcile.KindPluginSet,
			Mode:    kinds.ModeDomainLocal,
			Owner:   "acli",
			Explain: "期望态=平台签发的逐插件清单（name/version/spec_hash/文件/注册项）；逐节点三态收敛并按 .old.<deploy_id> 备份回滚",
			Note:    "未走 Run：集群扇出（一枚 Hub 会话探多节点）与脚本原子性（同一 SSH 会话内完成备份/写入/注册/自检）骨架契约表达不了；能力完整，缺的是与骨架类型同源",
		},
	}
}

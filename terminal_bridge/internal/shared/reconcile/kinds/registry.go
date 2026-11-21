// Package kinds 是节点同步链路的**登记册**：每条链路必须在此声明自己是
// 什么、由谁实现、接入骨架到哪一层。
//
// 为什么不是"把各链路的 DesiredState 构建搬进来"：
//
//	构建逻辑依赖各域的产品语义（acli 版本判序、spc 槽位布局、插件注册表），
//	搬进 shared 只会制造一堆转发参数。真正缺的不是文件位置，而是
//	"链路可枚举、接入程度可验证"——本包只提供这一层声明与守卫。
//
// 分层约束（与 reconcile 一致）：
//   - 本包只 import 父包 reconcile 与标准库，不认识任何业务域；
//   - 业务域/装配层反过来 import 本包做注册；
//   - 未注册的 Kind 由守卫测试判失败，因此"定义了链路但没人接"不可能长期存在。
package kinds

import (
	"fmt"
	"sort"
	"strings"
	"sync"

	"terminal_bridge/internal/shared/reconcile"
)

// Mode 声明一条链路租用了骨架的哪一层能力。
//
// 把"接入程度"做成枚举而不是散落在注释里，是因为它决定后续维护方式：
// plan_activate 的链路改判定改骨架即可；intent_only 的链路改写入必须看域内脚本。
type Mode string

const (
	// ModePlanActivate：走 Compare/Plan/Run 全流程（Provider → DesiredState → 写入 → 验证 → 回执）。
	ModePlanActivate Mode = "plan_activate"
	// ModeCompareOnly：只租用三态判定（无账本链路，节点事实是唯一真相）。
	ModeCompareOnly Mode = "compare_only"
	// ModeIntentOnly：只租用持久还原意图（写入脚本自管，含骨架未表达的语义）。
	ModeIntentOnly Mode = "intent_only"
	// ModeDeclarative：只租用声明式建模（DesiredState 用作"验证目标清单"），
	// 既不判定也不编排。用于"安装后落盘校验"这类只读链路。
	ModeDeclarative Mode = "declarative"
)

// Valid 报告 Mode 是否已登记。
func (m Mode) Valid() bool {
	switch m {
	case ModePlanActivate, ModeCompareOnly, ModeIntentOnly, ModeDeclarative:
		return true
	}
	return false
}

// Descriptor 是一条链路的自我声明。
type Descriptor struct {
	// Kind 必须来自 reconcile.AllKinds()。
	Kind string
	// Mode 见常量说明。
	Mode Mode
	// Owner 是实现所在业务域（诊断用，例如 "acli" / "logcollect"）。
	Owner string
	// Explain 用一句话说明这条链路的期望态从哪来、写到哪去。
	Explain string
	// Note 显式记录"尚未接入骨架的部分"。债务必须写在这里，而不是留在
	// 某个函数注释里等人考古：守卫测试会把它打印出来。
	Note string
}

// Registry 是登记册。包级默认实例供装配层使用；测试可自建实例隔离。
type Registry struct {
	mu    sync.RWMutex
	items map[string]Descriptor
}

// NewRegistry 构造空登记册。
func NewRegistry() *Registry {
	return &Registry{items: make(map[string]Descriptor, len(reconcile.AllKinds()))}
}

var defaultRegistry = NewRegistry()

// Default 返回装配层使用的登记册。
func Default() *Registry { return defaultRegistry }

// Register 校验并登记一条链路。
//
// 校验放在写入侧而不是守卫侧：注册错了（未知 Kind、非法 Mode、空说明）
// 应当在启动装配时就 panic 掉，而不是等 CI 跑守卫测试才发现。
func (r *Registry) Register(d Descriptor) error {
	if !reconcile.ValidKind(d.Kind) {
		return fmt.Errorf("kinds: 未登记的 Kind %q", d.Kind)
	}
	if !d.Mode.Valid() {
		return fmt.Errorf("kinds: %s 的 Mode %q 非法", d.Kind, d.Mode)
	}
	if strings.TrimSpace(d.Owner) == "" {
		return fmt.Errorf("kinds: %s 缺少 Owner", d.Kind)
	}
	if strings.TrimSpace(d.Explain) == "" {
		return fmt.Errorf("kinds: %s 缺少 Explain", d.Kind)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, exists := r.items[d.Kind]; exists {
		return fmt.Errorf("kinds: %s 重复登记", d.Kind)
	}
	r.items[d.Kind] = d
	return nil
}

// Get 按 Kind 取声明。
func (r *Registry) Get(kind string) (Descriptor, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	d, ok := r.items[kind]
	return d, ok
}

// All 按 Kind 字典序返回全部声明（稳定输出，便于对账）。
func (r *Registry) All() []Descriptor {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]Descriptor, 0, len(r.items))
	for _, d := range r.items {
		out = append(out, d)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Kind < out[j].Kind })
	return out
}

// Missing 返回 reconcile 已登记但尚未在册的 Kind。
//
// 守卫测试把它当断言用：返回非空即说明"加了链路却没声明接入程度"，
// 后续维护者无法判断该改骨架还是该改域内脚本。
func (r *Registry) Missing() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	var missing []string
	for _, kind := range reconcile.AllKinds() {
		if _, ok := r.items[kind]; !ok {
			missing = append(missing, kind)
		}
	}
	return missing
}

// Reset 清空登记册。只给测试用，避免用例之间互相污染。
func (r *Registry) Reset() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.items = make(map[string]Descriptor, len(reconcile.AllKinds()))
}

// MustRegister 是装配层入口：登记失败直接 panic。
//
// 装配期即失败优于运行期才发现：链路声明错误意味着编排走错分支，
// 那种 bug 在生产上表现为"同步成功但节点没变更"，极难定位。
func MustRegister(d Descriptor) {
	if err := Default().Register(d); err != nil {
		panic(err)
	}
}

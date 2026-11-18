package reconcile

// fanout.go —— 集合性写入/探测的编排（设计 §2.4.2）。
//
// 三条用事故换来的原则（#1546、Q2026110302166）：
//  1. 写不全就绝不能下发：只写发起节点时，其余节点会继续用原清单收集且不报任何错
//     （spc 缺失只是 warning），这是整条链路最危险的"看起来成功了"。
//  2. 回滚只针对已写入节点：把"SSH 不可达而未写入"的节点纳入回滚，
//     会制造"可能残留平台清单"的假告警，把排障引向完全错误的方向。
//  3. 节点不可达 vs 节点环境坏是两件事：前者本轮不参与（可从采集范围剔除），
//     后者必须停手（继续采会得到混合清单，产物无法解释）。
//
// 因此 written-set 由骨架维护而不是调用方自觉：调用方只报告逐台事实，
// "谁可以被回滚"这个决定收敛到一处，任何新 Kind 都自动继承同一条不变量。

import (
	"context"
	"sort"
	"strings"
	"sync"
	"time"
)

// 默认值与现有 spc 扇出一致：并发 4（每台 3 次 SSH，全串行排不进 180s 预算，
// 全并发会打满被采节点的 sshd）、单台单步 30s（BatchMode + ConnectTimeout=5 之后
// 最坏 5s 建连，30s 覆盖探测与回读校验，又能把"某台卡住"限制在局部）。
const (
	DefaultFanoutConcurrency = 4
	DefaultHostTimeout       = 30 * time.Second
)

// HostResult 是一台主机上一步操作的结论。
//
// Value 携带消费者自己的逐台证据（spc 的逐槽位明细、插件的注册表事实）。
// 骨架不认识这些形状，也不该认识——它只负责"谁成功、谁失败、谁能被回滚"。
type HostResult struct {
	Host        string
	Written     bool
	ErrorCode   string
	ErrorDetail string
	Evidence    []Evidence
	Value       any
}

// OK 报告该台写入成功。
func (h HostResult) OK() bool { return h.ErrorCode == "" }

// WrittenSet 是本次真正写入成功的主机集合（保持首次写入顺序）。
//
// 用集合而不是 []HostResult 过滤：回滚的输入必须是"已写入"这个事实本身，
// 让调用方再从结果里挑一遍 Written 等于把不变量交回给自觉。
type WrittenSet struct {
	hosts map[string]bool
	order []string
}

// Hosts 返回已写入主机（按写入顺序）。
func (ws WrittenSet) Hosts() []string {
	out := make([]string, 0, len(ws.order))
	out = append(out, ws.order...)
	return out
}

// Has 报告某台是否在写入集里。
func (ws WrittenSet) Has(host string) bool { return ws.hosts[host] }

// Len 返回写入台数。
func (ws WrittenSet) Len() int { return len(ws.order) }

func (ws *WrittenSet) mark(host string) {
	if ws.hosts == nil {
		ws.hosts = map[string]bool{}
	}
	if ws.hosts[host] {
		return
	}
	ws.hosts[host] = true
	ws.order = append(ws.order, host)
}

// WriteFn 是消费者提供的"在一台主机上完成写入"的动作。
// 骨架负责并发、超时、互斥与 written-set；动作内部的步骤语义归消费者。
type WriteFn func(ctx context.Context, host string) HostResult

// RollbackFn 是消费者提供的"撤销一台主机上的写入"的动作。
type RollbackFn func(ctx context.Context, host string) HostResult

// Fanout 管理多主机编排。
//
// Guard 可选：给定时按**主机键**互斥（与 Run 的 kind@host 键不同命名空间，
// 因此"扇出里再跑单节点流水线"不会自己把自己锁死）。
// BatchMode 只是声明位：真正的 ssh 参数由消费者的跳板命令渲染，
// 骨架不拼 ssh 命令（那会让横向原语认识具体登录方式）。
//
// HostTimeout 为负表示**禁用主机级超时**：spc 逐台动作含多条命令，
// 每条的 30s 预算已在消费者内部强制；再套一层 30s 主机级会把
// 多槽位写入误杀成"单台超时"。零值仍取默认 30s。
type Fanout struct {
	Concurrency int
	HostTimeout time.Duration
	BatchMode   bool
	Guard       *Guard
}

// DefaultFanout 给出与现有 spc 扇出逐字一致的默认参数。
func DefaultFanout() Fanout {
	return Fanout{Concurrency: DefaultFanoutConcurrency, HostTimeout: DefaultHostTimeout, BatchMode: true}
}

func (f Fanout) normalized() Fanout {
	if f.Concurrency <= 0 {
		f.Concurrency = DefaultFanoutConcurrency
	}
	if f.HostTimeout == 0 {
		f.HostTimeout = DefaultHostTimeout
	}
	return f
}

// WriteAll 对目标主机列表执行写入，返回逐台结果与写入集。
//
// 结果顺序与入参一致（去重后），便于消费者按原序落回执；
// 空主机名直接跳过——一个空 IP 会让 ssh 跳到本机，那是最坏的静默错误。
func (f Fanout) WriteAll(ctx context.Context, hosts []string, fn WriteFn) ([]HostResult, WrittenSet) {
	targets := dedupeHosts(hosts)
	results := f.each(ctx, targets, fn)
	ws := WrittenSet{}
	for _, result := range results {
		if result.Written && result.ErrorCode == "" {
			ws.mark(result.Host)
		}
	}
	return results, ws
}

// RollbackWritten 只对写入集中的主机执行回滚。
//
// 写入集为空时一条命令都不发：这不是优化，是不变量——
// 从未写入的节点没有需要撤销的改动，碰它只会制造假告警。
func (f Fanout) RollbackWritten(ctx context.Context, ws WrittenSet, fn RollbackFn) []HostResult {
	hosts := ws.Hosts()
	if len(hosts) == 0 {
		return nil
	}
	return f.each(ctx, hosts, WriteFn(fn))
}

// Each 对主机列表并发执行同一动作，不跟踪 written-set。
// 用于集合性探测/验证（B8：按 descriptor 声明的目标逐节点落盘验证）与终态还原。
func (f Fanout) Each(ctx context.Context, hosts []string, fn WriteFn) []HostResult {
	return f.each(ctx, dedupeHosts(hosts), fn)
}

func (f Fanout) each(ctx context.Context, hosts []string, fn WriteFn) []HostResult {
	if len(hosts) == 0 || fn == nil {
		return nil
	}
	f = f.normalized()
	results := make([]HostResult, len(hosts))
	sem := make(chan struct{}, f.Concurrency)
	var wg sync.WaitGroup
	for index, host := range hosts {
		wg.Add(1)
		go func(index int, host string) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			results[index] = f.runHost(ctx, host, fn)
		}(index, host)
	}
	wg.Wait()
	return results
}

// runHost 套上互斥与单台超时再执行动作。
//
// 互斥失败不阻塞、不重试：返回 busy 让调用方按既有退避重新入队（B3）。
// 超时用 context 而不是 time.After：动作内部的 SSH 调用必须能收到取消，
// 否则"超时了"只是骨架不再等，节点上那条命令还在跑。
func (f Fanout) runHost(ctx context.Context, host string, fn WriteFn) HostResult {
	if f.Guard != nil {
		release, err := f.Guard.Acquire(host)
		if err != nil {
			return HostResult{Host: host, ErrorCode: CodeBusy, ErrorDetail: err.Error()}
		}
		defer release()
	}
	hostCtx := ctx
	if f.HostTimeout > 0 {
		var cancel context.CancelFunc
		hostCtx, cancel = context.WithTimeout(ctx, f.HostTimeout)
		defer cancel()
	}
	result := fn(hostCtx, host)
	result.Host = host
	if hostCtx.Err() != nil && result.ErrorCode == "" {
		result.ErrorCode = CodeProbeFailed
		result.ErrorDetail = "单台动作超时：" + hostCtx.Err().Error()
	}
	return result
}

// dedupeHosts 去空、去重、保序。
func dedupeHosts(hosts []string) []string {
	out := make([]string, 0, len(hosts))
	seen := map[string]bool{}
	for _, host := range hosts {
		key := strings.TrimSpace(host)
		if key == "" || seen[key] {
			continue
		}
		seen[key] = true
		out = append(out, key)
	}
	return out
}

// SortedHosts 返回排序后的主机列表（回执与日志用，保证可比对）。
func SortedHosts(hosts []string) []string {
	out := dedupeHosts(hosts)
	sort.Strings(out)
	return out
}

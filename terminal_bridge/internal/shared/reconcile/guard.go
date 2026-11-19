package reconcile

// guard.go —— 并发与 fencing（设计 §2.4.3）。
//
// 第一性原理：节点上的文件是共享可变状态，而桥是常驻进程、可同时服务多个工单。
// 没有主机键互斥时，两个工单对同一台节点的"备份→写入→还原"会交错：
// A 备份了原件、B 备份了 A 的平台清单，还原时原件就永久丢了。
// 这不是概率问题，是顺序问题——必须用锁把顺序钉死，而不是靠退避碰运气。
//
// 为什么 Acquire 返回 ErrBusy 而不是阻塞：桥的动作预算是秒级（spc 180s、
// acli 720s），阻塞等锁会把预算吃光并让前端只看到超时。调用方收到 ErrBusy
// 必须重新入队（沿用既有退避），不得视为失败终态（修 B3）。

import (
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"sync"
)

// ErrBusy 表示主机键已被占用。它是"稍后再来"，不是"做不成"。
var ErrBusy = errors.New("host busy")

// Guard 提供主机键互斥与租约 fencing。
//
// 零值可用（locks 懒初始化）；nil 接收者同样安全——Deps.Guard 是可选依赖，
// 单节点链路（config）不需要互斥，不该被迫构造一个空 Guard。
type Guard struct {
	mu      sync.Mutex
	locks   map[string]bool
	basedir string
}

// NewGuard 建一个带基线目录的 Guard。basedir 为空表示只做互斥、不做 fencing。
func NewGuard(basedir string) *Guard {
	return &Guard{basedir: basedir}
}

// HostKey 拼出主机键：同一主机上不同 Kind 的操作互不阻塞
// （写 spc 与装 acli 动的是不同文件，串行化只会白白拉长动作预算）。
func HostKey(kind, nodeIP string) string {
	return strings.TrimSpace(kind) + "@" + strings.TrimSpace(nodeIP)
}

// Acquire 取主机键的独占锁；已占用时立刻返回 ErrBusy。
//
// release 幂等：重复调用不会把别人刚拿到的锁放掉（这是 map[string]bool
// 方案最容易踩的坑——用 sync.Once 把"释放"钉成一次性动作）。
func (g *Guard) Acquire(hostKey string) (func(), error) {
	if g == nil {
		return func() {}, nil
	}
	key := strings.TrimSpace(hostKey)
	if key == "" {
		return nil, fmt.Errorf("主机键为空，无法建立互斥")
	}
	g.mu.Lock()
	if g.locks == nil {
		g.locks = map[string]bool{}
	}
	if g.locks[key] {
		g.mu.Unlock()
		return nil, ErrBusy
	}
	g.locks[key] = true
	g.mu.Unlock()

	var once sync.Once
	return func() {
		once.Do(func() {
			g.mu.Lock()
			delete(g.locks, key)
			g.mu.Unlock()
		})
	}, nil
}

// Held 报告主机键当前是否被占用（只用于日志与测试断言，不得拿它做"先查后锁"）。
func (g *Guard) Held(hostKey string) bool {
	if g == nil {
		return false
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.locks[strings.TrimSpace(hostKey)]
}

// LeasePath 给分片/临时文件拼出带代际标识的路径（租约 fencing）。
//
// execID 必须非空：没有代际标识时，两个并发执行者会算出同一个分片路径并互写，
// 校验和都"对得上"（各自写了自己那半），产物却是拼接出来的坏文件。
// 因此这里 fail-closed，而不是退化成不带代际的路径。
func (g *Guard) LeasePath(hostKey, execID, name string) (string, error) {
	if g == nil || g.basedir == "" {
		return "", fmt.Errorf("未配置基线目录，无法生成 fencing 路径")
	}
	if strings.TrimSpace(execID) == "" {
		return "", fmt.Errorf("缺少执行代际标识，拒绝生成共享分片路径")
	}
	if strings.TrimSpace(name) == "" {
		return "", fmt.Errorf("分片名为空")
	}
	return filepath.Join(g.basedir, fenceSegment(hostKey), fenceSegment(execID), fenceSegment(name)), nil
}

// fenceSegment 把任意键压成单层路径片段：分隔符与 .. 一律换掉，
// 否则一个含 "/" 的 execID 就能把分片写到基线目录之外。
func fenceSegment(value string) string {
	replaced := strings.NewReplacer("/", "_", "\\", "_", ":", "_", "..", "_").Replace(strings.TrimSpace(value))
	if replaced == "" {
		return "_"
	}
	return replaced
}

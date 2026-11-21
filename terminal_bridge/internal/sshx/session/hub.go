// Package session 是 SSH 会话的**唯一所有者**：注册表、引用计数、空闲租约与
// 重连认证快照都归这里，app 只做装配与事件。
//
// 为什么独立成包：会话是多条链路（终端输入、EXEC、日志采集、交付、acli、SFTP）
// 共享的核心资源，此前它和 sftp 任务表、acli 缓存一起挤在 app.Bridge 里，
// 导致 11 个业务域为了读会话而反向 import 装配层。把"会话"抽出来后，
// 域的依赖方向回到：业务域 → sshx/session → sshx，装配层只在 cmd/bridge 注入。
//
// 为什么挂在 sshx 而不是 shared：本包持有并调用 *sshx.SSHSession 的具体方法
// （HasLongTask / IsBusy / Close / Address），与 sshx 是同一层的基础设施。
// 而 shared/ 在本仓库被约定为"不认识上层的原语层"（scripts/check_bridge_imports.py
// 禁令1 明确禁止 shared 依赖 sshx），放进去会打破分层，故下沉为 sshx 的子包。
// 业务域无需直接 import 本包——它们通过 app.Bridge 暴露的方法访问会话。
//
// 锁纪律：Hub.mu 保护全部四张表（sessions / refs / idleAt / lastAuth），
// 它们必须原子协同——入表即 refs=1、release 归零才记 idle、reap 同时读两表。
package session

import (
	"log"
	"net"
	"strings"
	"sync"
	"time"

	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/sshx"
)

// SessionKey 生成会话键：有节点 IP 时为 caseID@nodeIP，否则只用工单号（默认会话）。
func SessionKey(caseID, nodeIP string) string {
	if nodeIP == "" {
		return caseID
	}
	return caseID + "@" + nodeIP
}

// SessionKeyCase 从会话键里取回工单号（`caseID@nodeIP` → `caseID`）。
func SessionKeyCase(key string) string {
	if idx := strings.Index(key, "@"); idx >= 0 {
		return key[:idx]
	}
	return key
}

// Hub 是会话注册表。零值不可用，必须 NewHub。
type Hub struct {
	mu       sync.Mutex
	sessions map[string]*sshx.SSHSession // key: caseID@nodeIP
	// lastAuth 保存每个 caseID 最近一次 ssh_connect 的认证信息，用于自动连接新节点。
	lastAuth map[string]models.InMessage
	// refs/idleAt 是引用计数与空闲租约：WS 关闭只 release 不销毁，
	// 会话在 TTL 内仍可被复用（日志面板借用、刷新页面重连都不应被连坐清理）。
	refs   map[string]int
	idleAt map[string]time.Time
}

func NewHub() *Hub {
	return &Hub{
		sessions: make(map[string]*sshx.SSHSession),
		lastAuth: make(map[string]models.InMessage),
		refs:     make(map[string]int),
		idleAt:   make(map[string]time.Time),
	}
}

// SetSessionIdleAt 覆写会话空闲租约起点（测试注入过期租约用；正常路径由 Release 维护）。
func (h *Hub) SetSessionIdleAt(key string, at time.Time) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.idleAt[key] = at
}

func (h *Hub) Get(key string) *sshx.SSHSession {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.sessions[key]
}

// SessionByNodeIP 在同一 bridge 实例内按节点 IP 查找任意工单下的会话。
//
// 为什么需要：SSH 会话的真实归属是「操作者 + 节点」，工单号只决定采集结果归档到哪张单。
// 只按 caseID@nodeIP 精确匹配时，"在 SSH 终端或旧工单里登录过、换到新工单再采集"
// 会直接报 session_missing（LC-ENV-006），用户已经看到绿色"已连接"却无法采集。
// 这里仅做读取回退，不改变会话创建语义；bridge 是单用户本地进程，不存在跨用户越权。
func (h *Hub) SessionByNodeIP(nodeIP string) (*sshx.SSHSession, string) {
	nodeIP = strings.TrimSpace(nodeIP)
	if nodeIP == "" {
		return nil, ""
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	// 精确后缀匹配，避免 10.1.1.1 命中 10.1.1.11 这类前缀误判
	suffix := "@" + nodeIP
	for key, session := range h.sessions {
		if session != nil && strings.HasSuffix(key, suffix) {
			return session, key
		}
	}
	return nil, ""
}

// SessionByAddressHost 按会话实际连到的主机名/IP 匹配。
//
// 为什么需要：`ssh_connect` 历史上不带 node_ip，会话键只落成 `caseID`（没有 @IP），
// 于是"在 SSH 终端里连好、命令都能跑"的情况下，日志收集按 `caseID@nodeIP` 仍然查不到。
// 会话对象的 Address 记录了真实目标地址，用它兜底可以覆盖这类存量会话。
func (h *Hub) SessionByAddressHost(nodeIP string) (*sshx.SSHSession, string) {
	nodeIP = strings.TrimSpace(nodeIP)
	if nodeIP == "" {
		return nil, ""
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	for key, session := range h.sessions {
		if session == nil {
			continue
		}
		host, _, err := net.SplitHostPort(session.Address)
		if err != nil {
			continue
		}
		if strings.EqualFold(strings.TrimSpace(host), nodeIP) {
			return session, key
		}
	}
	return nil, ""
}

// Set 登记会话并标记为"有人持有"（refs=1，WS 关闭时由 Release 归零）。
func (h *Hub) Set(key string, s *sshx.SSHSession) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.sessions[key] = s
	h.holdLocked(key)
}

func (h *Hub) Remove(key string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	delete(h.sessions, key)
}

// holdLocked 在会话入表时标记为"有人持有"（refs=1）。调用方必须已持有 h.mu。
//
// refs 是 0/1 状态位而不是计数器：当前每会话只有唯一持有者——建立它的那条 WS 连接。
// 复用该会话的其它连接（日志面板、交付续传）不会增加持有数，它们靠
// sshx.SSHSession 的业务活跃窗（MarkBusy / BeginLongTask）保护，而不是靠这里。
// 因此不要在此累加：没有对称的递减点，一旦累加就永远归不了零，会话会永久泄漏。
//
// 后缀 Locked 是硬约定：Go 的 sync.Mutex 不可重入，Set 已在临界区内，
// 早期实现这里自己 Lock 过，导致任何一次建会话都自死锁（表现为测试拖满超时而非快速失败）。
func (h *Hub) holdLocked(key string) {
	h.refs[key] = 1
	delete(h.idleAt, key)
}

func (h *Hub) SetLastAuth(caseID string, msg models.InMessage) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.lastAuth[caseID] = msg
}

// LastAuth 读取最近一次认证快照（自动建连用）。
func (h *Hub) LastAuth(caseID string) (models.InMessage, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	msg, ok := h.lastAuth[caseID]
	return msg, ok
}

func (h *Hub) ClearLastAuth(caseID string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	delete(h.lastAuth, caseID)
}

// Count 返回当前存活会话数。
//
// 用途：日志收集报 session_missing 时区分两件本质不同的事——数量为 0 说明连接生命周期
// 已结束（断连/远端退出），数量 >0 说明是键或地址没对上。
func (h *Hub) Count() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.sessions)
}

// Keys 返回当前会话键快照（最多 limit 个，0 表示不限）。
// 键形如 `caseID@nodeIP`，是判断"会话在、但键不匹配"的唯一客观依据。
func (h *Hub) Keys(limit int) []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	keys := make([]string, 0, len(h.sessions))
	for key := range h.sessions {
		if limit > 0 && len(keys) >= limit {
			break
		}
		keys = append(keys, key)
	}
	return keys
}

// List 返回当前所有活跃 SSH 会话，供管理端枚举"custom-ui + bridge 已登录"的环境。
// 只暴露会话键与连接地址，不含用户名、口令、私钥等任何凭据字段。
func (h *Hub) List() []models.BridgeSessionInfo {
	h.mu.Lock()
	defer h.mu.Unlock()
	items := make([]models.BridgeSessionInfo, 0, len(h.sessions))
	for key, s := range h.sessions {
		if s == nil {
			continue
		}
		caseID, nodeIP := key, ""
		if idx := strings.Index(key, "@"); idx >= 0 {
			caseID, nodeIP = key[:idx], key[idx+1:]
		}
		if nodeIP == "" {
			if host, _, err := net.SplitHostPort(s.Address); err == nil {
				nodeIP = host
			} else {
				nodeIP = s.Address
			}
		}
		items = append(items, models.BridgeSessionInfo{
			CaseID: caseID, NodeIP: nodeIP, Host: s.Address, Key: key,
		})
	}
	return items
}

// Counts 返回 (存活会话数, 缓存认证的工单数)，供 /status 使用。
func (h *Hub) Counts() (sessions, cachedAuthCases int) {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.sessions), len(h.lastAuth)
}

// Resolve 根据 caseID + nodeIP 解析 SSH 会话。
// 有 nodeIP 时只精确匹配，不 fallback 到默认会话（避免命令在错误节点执行）。
func (h *Hub) Resolve(msg models.InMessage) (*sshx.SSHSession, string) {
	if msg.NodeIP != "" {
		key := SessionKey(msg.CaseID, msg.NodeIP)
		if s := h.Get(key); s != nil {
			log.Printf("[Bridge] resolveSession: 精确匹配 key=%s", key)
			return s, key
		}
		// 有 nodeIP 但找不到匹配会话 → 返回 nil，由调用方触发 autoConnect
		log.Printf("[Bridge] resolveSession: nodeIP=%s 无匹配会话，需自动连接", msg.NodeIP)
		return nil, ""
	}

	defaultKey := SessionKey(msg.CaseID, "")
	if s := h.Get(defaultKey); s != nil {
		log.Printf("[Bridge] resolveSession: 使用默认会话 key=%s", defaultKey)
		return s, defaultKey
	}
	return nil, ""
}

// ResolveAcliSync 解析 acli 安装要落到哪条 SSH 会话。
//
// 兜底顺序与 log_collect 保持一致：精确键 → 按 node_ip 后缀 → 按会话实际地址 →
// 本工单唯一会话。历史实现只做"键为 caseID"的默认匹配，而 ssh_connect 落库的键是
// `caseID@nodeIP`，只要调用方没带 node_ip 就恒定 session_missing——表现为
// "环境明明没装 acli，却从来没有下载/安装日志"，链路根本没启动过。
func (h *Hub) ResolveAcliSync(msg models.InMessage) (*sshx.SSHSession, string) {
	if s, key := h.Resolve(msg); s != nil {
		return s, key
	}
	if msg.NodeIP != "" {
		if s, key := h.SessionByNodeIP(msg.NodeIP); s != nil {
			log.Printf("[Bridge] resolveAcliSyncSession: 按节点 IP 复用会话 key=%s", key)
			return s, key
		}
		if s, key := h.SessionByAddressHost(msg.NodeIP); s != nil {
			log.Printf("[Bridge] resolveAcliSyncSession: 按会话实际地址复用 key=%s", key)
			return s, key
		}
	}
	if s, key := h.OnlySessionForCase(msg.CaseID); s != nil {
		log.Printf("[Bridge] resolveAcliSyncSession: 工单唯一会话兜底 key=%s", key)
		return s, key
	}
	return nil, ""
}

// ResolveInput 解析键盘输入 / AI 命令注入要落到哪条 SSH 会话。
//
// 为什么不能沿用 Resolve：`ssh_connect` 带 node_ip 后会话键是 `caseID@nodeIP`
// （#1229 为对齐日志收集而改），而输入侧历史上只查 `caseID`，于是查不到就直接返回，
// 表现为"界面绿色已连接、打字回车毫无反应、日志里也干干净净"。
// 输入不触发自动建连（建连属于 exec 的语义），只做只读解析。
func (h *Hub) ResolveInput(msg models.InMessage, activeKey string, owned *OwnedTracker) (*sshx.SSHSession, string) {
	if s, key := h.Resolve(msg); s != nil {
		return s, key
	}
	// 同一块终端：输入天然属于本连接刚建立的那个会话，即便消息里没带 node_ip。
	if activeKey != "" && (msg.CaseID == "" || SessionKeyCase(activeKey) == msg.CaseID) {
		if s := h.Get(activeKey); s != nil {
			log.Printf("[Bridge] resolveInputSession: 命中本连接活动会话 key=%s", activeKey)
			return s, activeKey
		}
	}
	// 本连接自己建立的会话优先于全局猜测，避免多工单并行时打错节点。
	if msg.CaseID != "" && owned != nil {
		for key, sess := range owned.Snapshot() {
			if sess != nil && (key == msg.CaseID || strings.HasPrefix(key, msg.CaseID+"@")) {
				if current := h.Get(key); current == sess {
					log.Printf("[Bridge] resolveInputSession: 命中本连接所属工单会话 key=%s", key)
					return sess, key
				}
			}
		}
	}
	if s, key := h.OnlySessionForCase(msg.CaseID); s != nil {
		log.Printf("[Bridge] resolveInputSession: 工单唯一会话兜底 key=%s", key)
		return s, key
	}
	return nil, ""
}

// OnlySessionForCase 返回该工单唯一的 SSH 会话；有多条时返回 nil（宁可不装，也不猜节点）。
func (h *Hub) OnlySessionForCase(caseID string) (*sshx.SSHSession, string) {
	caseID = strings.TrimSpace(caseID)
	if caseID == "" {
		return nil, ""
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	var (
		found    *sshx.SSHSession
		foundKey string
	)
	prefix := caseID + "@"
	for key, sess := range h.sessions {
		if sess == nil || (key != caseID && !strings.HasPrefix(key, prefix)) {
			continue
		}
		if found != nil {
			return nil, ""
		}
		found, foundKey = sess, key
	}
	return found, foundKey
}

// Release 释放唯一持有者的持有权：归零后进入空闲租约，**不销毁会话**。
//
// 这是与历史行为的关键差别：过去 WS 一断就 close+remove，导致刷新页面/网络抖动
// 会把正在被日志面板使用的会话一起带走（2026-10-27 14:37:49 的故障链）。
//
// 归零后是否被回收由 reaper 二次判定：空闲超过 TTL **且** 不在业务活跃窗
// （MarkBusy / BeginLongTask）内，见 ReapCandidates。因此"复用会话的第二条连接"
// 不需要在这里加持有计数——它的安全由活跃窗保证，加计数反而会因为没有对称递减点
// 而永久泄漏会话。重复调用本方法是幂等的（已归零则只刷新空闲起点）。
func (h *Hub) Release(key string) (idle bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.refs[key] = 0
	h.idleAt[key] = time.Now()
	return true
}

// ForgetLease 清掉租约簿记（销毁时必须调用，否则 reaper 会反复命中空键）。
func (h *Hub) ForgetLease(key string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	delete(h.refs, key)
	delete(h.idleAt, key)
}

// ReapCandidates 返回空闲超时、可回收的会话键。
//
// 只在 refs==0 且空闲超过 TTL 时回收：远端已 EOF 的会话会先被 onExit 清掉，
// 这里只处理"连接都走了但会话还活着"的那一类。
// 有长任务在跑或近期仍被业务使用的会话不回收，并把 idleAt 推到当前时刻——保护期内
// 不计空闲，否则保护一解除就会按历史空闲时长立刻回收（2026-10-28 交付中断的成因）。
func (h *Hub) ReapCandidates(ttl time.Duration) []string {
	if ttl <= 0 {
		return nil
	}
	now := time.Now()
	h.mu.Lock()
	defer h.mu.Unlock()
	victims := make([]string, 0, 4)
	for key, idleAt := range h.idleAt {
		if h.refs[key] > 0 {
			continue
		}
		if sess := h.sessions[key]; sess != nil && (sess.HasLongTask() || sess.IsBusy()) {
			h.idleAt[key] = now
			continue
		}
		if now.Sub(idleAt) >= ttl {
			victims = append(victims, key)
		}
	}
	return victims
}

// OwnedTracker 记录"本连接建立了哪些会话"：输入路由与断连清理都靠它区分
// "自己建的"和"从别的连接/工单复用的"，避免多工单并行时打错节点或误清他人会话。
type OwnedTracker struct {
	mu       sync.Mutex
	sessions map[string]*sshx.SSHSession
}

func NewOwnedTracker() *OwnedTracker {
	return &OwnedTracker{sessions: make(map[string]*sshx.SSHSession)}
}

func (t *OwnedTracker) Set(key string, session *sshx.SSHSession) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.sessions[key] = session
}

func (t *OwnedTracker) Remove(key string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	delete(t.sessions, key)
}

// DrainCase 取出当前连接拥有的指定工单全部会话，包括自动建立的节点会话。
func (t *OwnedTracker) DrainCase(caseID string) map[string]*sshx.SSHSession {
	t.mu.Lock()
	defer t.mu.Unlock()
	out := make(map[string]*sshx.SSHSession)
	for key, sess := range t.sessions {
		if key == caseID || strings.HasPrefix(key, caseID+"@") {
			out[key] = sess
			delete(t.sessions, key)
		}
	}
	return out
}

// Snapshot 返回当前连接所拥有会话的浅拷贝，供只读遍历（持锁时间最短）。
func (t *OwnedTracker) Snapshot() map[string]*sshx.SSHSession {
	t.mu.Lock()
	defer t.mu.Unlock()
	snapshot := make(map[string]*sshx.SSHSession, len(t.sessions))
	for key, sess := range t.sessions {
		snapshot[key] = sess
	}
	return snapshot
}

func (t *OwnedTracker) Drain() map[string]*sshx.SSHSession {
	t.mu.Lock()
	defer t.mu.Unlock()
	out := t.sessions
	t.sessions = make(map[string]*sshx.SSHSession)
	return out
}

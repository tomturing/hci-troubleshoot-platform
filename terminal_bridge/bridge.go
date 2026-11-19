package main

import (
	"terminal_bridge/internal/sshx"
	"terminal_bridge/internal/logpush"
	"fmt"
	"log"
	"net"
	"strings"
	"sync"
	"sync/atomic"
	"terminal_bridge/internal/shared/models"
	"time"

	"golang.org/x/net/websocket"
)

func sessionKey(caseID, nodeIP string) string {
	if nodeIP == "" {
		return caseID
	}
	return caseID + "@" + nodeIP
}

// sessionKeyCase 从会话键里取回工单号（`caseID@nodeIP` → `caseID`）。
func sessionKeyCase(key string) string {
	if idx := strings.Index(key, "@"); idx >= 0 {
		return key[:idx]
	}
	return key
}

type Bridge struct {
	mu       sync.Mutex
	sessions map[string]*sshx.SSHSession // key: caseID@nodeIP
	// 保存每个 caseID 最近一次 ssh_connect 的认证信息，用于自动连接新节点
	lastAuth map[string]models.InMessage // key: caseID
	// sftpTasks 登记进行中的文件下载任务（key: caseID|taskID），
	// 用于取消、去重与并发上限；由 sftpTasksMu 保护。
	sftpTasksMu sync.Mutex
	sftpTasks   map[string]*sftpTask
	// 会话引用计数与空闲租约簿记：WS 关闭只 release 不销毁，会话在 TTL 内仍可被复用
	// （日志面板借用的会话、以及刷新页面后准备接上的会话都不应被连坐清理）。
	sessionRefs   map[string]int
	sessionIdleAt map[string]time.Time

	// acli 安装包传输互斥与已校验包缓存（见 acli_package.go）。
	// 互斥不是性能优化而是正确性要求：并发的两个上传会各自 cat>tmp 再 mv 到
	// 同一个 destPath，谁最后落盘完全取决于时序——这正是「包不对」的成因之一。
	acliMu    sync.Mutex
	acliBusy  map[string]bool
	acliCache *acliPackageCache
}

func newBridge() *Bridge {
	return &Bridge{
		sessions:      make(map[string]*sshx.SSHSession),
		lastAuth:      make(map[string]models.InMessage),
		sftpTasks:     make(map[string]*sftpTask),
		sessionRefs:   make(map[string]int),
		sessionIdleAt: make(map[string]time.Time),
	}
}

func (b *Bridge) get(key string) *sshx.SSHSession {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.sessions[key]
}

// sessionByNodeIP 在同一 bridge 实例内按节点 IP 查找任意工单下的会话。
//
// 为什么需要：SSH 会话的真实归属是「操作者 + 节点」，工单号只决定采集结果归档到哪张单。
// 历史实现只按 caseID@nodeIP 精确匹配，于是"在 SSH 终端或旧工单里登录过、换到新工单再采集"
// 会直接报 session_missing（LC-ENV-006），用户已经看到绿色"已连接"却无法采集。
// 这里仅做读取回退，不改变会话创建语义；bridge 是单用户本地进程，不存在跨用户越权。
func (b *Bridge) sessionByNodeIP(nodeIP string) (*sshx.SSHSession, string) {
	nodeIP = strings.TrimSpace(nodeIP)
	if nodeIP == "" {
		return nil, ""
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	// 精确后缀匹配，避免 10.1.1.1 命中 10.1.1.11 这类前缀误判
	suffix := "@" + nodeIP
	for key, session := range b.sessions {
		if session != nil && strings.HasSuffix(key, suffix) {
			return session, key
		}
	}
	return nil, ""
}

// sessionByAddressHost 按会话实际连到的主机名/IP 匹配。
//
// 为什么需要：`ssh_connect` 历史上不带 node_ip，会话键只落成 `caseID`（没有 @IP），
// 于是"在 SSH 终端里连好、命令都能跑"的情况下，日志收集按 `caseID@nodeIP` 仍然查不到。
// 会话对象的 address 记录了真实目标地址，用它兜底可以覆盖这类存量会话。
func (b *Bridge) sessionByAddressHost(nodeIP string) (*sshx.SSHSession, string) {
	nodeIP = strings.TrimSpace(nodeIP)
	if nodeIP == "" {
		return nil, ""
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	for key, session := range b.sessions {
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

func (b *Bridge) set(key string, s *sshx.SSHSession) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.sessions[key] = s
	// 入表即登记一个持有者：引用计数从 1 开始，WS 关闭时由 releaseSession 减回 0。
	// 必须用 Locked 版本：本函数已持有 b.mu，而 Go 的 Mutex 不可重入。
	b.holdSessionLocked(key)
}

func (b *Bridge) remove(key string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	delete(b.sessions, key)
}

func (b *Bridge) setLastAuth(caseID string, msg models.InMessage) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.lastAuth[caseID] = msg
}

func (b *Bridge) getLastAuth(caseID string) (models.InMessage, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	msg, ok := b.lastAuth[caseID]
	return msg, ok
}

func (b *Bridge) clearLastAuth(caseID string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	delete(b.lastAuth, caseID)
}

// sessionCount 返回当前存活会话数。
//
// 用途：日志收集报 session_missing 时区分两件本质不同的事——
// 数量为 0 说明连接生命周期已结束（断连/远端退出），数量 >0 说明是键或地址没对上。
func (b *Bridge) sessionCount() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return len(b.sessions)
}

// sessionKeyList 返回当前会话键快照（最多 limit 个，0 表示不限）。
// 键形如 `caseID@nodeIP`，是判断「会话在、但键不匹配」的唯一客观依据。
func (b *Bridge) sessionKeyList(limit int) []string {
	b.mu.Lock()
	defer b.mu.Unlock()
	keys := make([]string, 0, len(b.sessions))
	for key := range b.sessions {
		if limit > 0 && len(keys) >= limit {
			break
		}
		keys = append(keys, key)
	}
	return keys
}

// models.BridgeSessionInfo 是「当前已登录、可下发」的环境摘要。
// 只暴露会话键与连接地址，不含用户名、口令、私钥等任何凭据字段。

// listSessions 返回当前所有活跃 SSH 会话，供管理端枚举「custom-ui + bridge 已登录」的环境。
func (b *Bridge) listSessions() []models.BridgeSessionInfo {
	b.mu.Lock()
	defer b.mu.Unlock()
	items := make([]models.BridgeSessionInfo, 0, len(b.sessions))
	for key, s := range b.sessions {
		if s == nil {
			continue
		}
		caseID, nodeIP := key, ""
		if idx := strings.Index(key, "@"); idx >= 0 {
			caseID, nodeIP = key[:idx], key[idx+1:]
		}
		if nodeIP == "" {
			if h, _, err := net.SplitHostPort(s.Address); err == nil {
				nodeIP = h
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

type bridgeStatus struct {
	ActiveSessions  int `json:"active_sessions"`
	CachedAuthCases int `json:"cached_auth_cases"`
}

func (b *Bridge) status() bridgeStatus {
	b.mu.Lock()
	defer b.mu.Unlock()
	return bridgeStatus{
		ActiveSessions:  len(b.sessions),
		CachedAuthCases: len(b.lastAuth),
	}
}

// resolveSession 根据 caseID + nodeIP 解析 SSH 会话
// 有 nodeIP 时只精确匹配，不 fallback 到默认会话（避免命令在错误节点执行）
func (b *Bridge) resolveSession(msg models.InMessage) (*sshx.SSHSession, string) {
	if msg.NodeIP != "" {
		key := sessionKey(msg.CaseID, msg.NodeIP)
		if s := b.get(key); s != nil {
			log.Printf("[Bridge] resolveSession: 精确匹配 key=%s", key)
			return s, key
		}
		// 有 nodeIP 但找不到匹配会话 → 返回 nil，由调用方触发 autoConnect
		log.Printf("[Bridge] resolveSession: nodeIP=%s 无匹配会话，需自动连接", msg.NodeIP)
		return nil, ""
	}

	// 无 nodeIP → 使用默认会话
	defaultKey := sessionKey(msg.CaseID, "")
	if s := b.get(defaultKey); s != nil {
		log.Printf("[Bridge] resolveSession: 使用默认会话 key=%s", defaultKey)
		return s, defaultKey
	}
	return nil, ""
}

// resolveAcliSyncSession 解析 acli 安装要落到哪条 SSH 会话。
//
// 兜底顺序与 log_collect 保持一致：精确键 → 按 node_ip 后缀 → 按会话实际地址 →
// 本工单唯一会话。
//
// 为什么必须兜底：历史上这里只做「键为 caseID」的默认匹配，而 ssh_connect 落库的键是
// `caseID@nodeIP`，只要调用方没带 node_ip 就恒定 session_missing——表现为
// 「环境明明没装 acli，却从来没有下载/安装日志」，链路根本没启动过。
func (b *Bridge) resolveAcliSyncSession(msg models.InMessage) (*sshx.SSHSession, string) {
	if s, key := b.resolveSession(msg); s != nil {
		return s, key
	}
	if msg.NodeIP != "" {
		if s, key := b.sessionByNodeIP(msg.NodeIP); s != nil {
			log.Printf("[Bridge] resolveAcliSyncSession: 按节点 IP 复用会话 key=%s", key)
			return s, key
		}
		if s, key := b.sessionByAddressHost(msg.NodeIP); s != nil {
			log.Printf("[Bridge] resolveAcliSyncSession: 按会话实际地址复用 key=%s", key)
			return s, key
		}
	}
	if s, key := b.onlySessionForCase(msg.CaseID); s != nil {
		log.Printf("[Bridge] resolveAcliSyncSession: 工单唯一会话兜底 key=%s", key)
		return s, key
	}
	return nil, ""
}

// resolveInputSession 解析键盘输入 / AI 命令注入要落到哪条 SSH 会话。
//
// 为什么不能沿用 resolveSession：`ssh_connect` 带 node_ip 后会话键是 `caseID@nodeIP`
// （#1229 为对齐日志收集而改），而输入侧历史上只查 `caseID`，于是查不到就直接返回，
// 表现为「界面绿色已连接、打字回车毫无反应、日志里也干干净净」。
//
// 输入不触发自动建连（建连属于 exec 的语义），只做只读解析；
// 顺序：精确键 → 本连接最近一次 ssh_connect 的会话 → 本工单唯一会话（多节点时不猜）。
func (b *Bridge) resolveInputSession(msg models.InMessage, activeKey string, ownedSessions *ownedSessionTracker) (*sshx.SSHSession, string) {
	if s, key := b.resolveSession(msg); s != nil {
		return s, key
	}
	// 同一块终端：输入天然属于本连接刚建立的那个会话，即便消息里没带 node_ip。
	if activeKey != "" && (msg.CaseID == "" || sessionKeyCase(activeKey) == msg.CaseID) {
		if s := b.get(activeKey); s != nil {
			log.Printf("[Bridge] resolveInputSession: 命中本连接活动会话 key=%s", activeKey)
			return s, activeKey
		}
	}
	// 本连接自己建立的会话优先于全局猜测，避免多工单并行时打错节点。
	if msg.CaseID != "" {
		for key, session := range ownedSessions.sessionsSnapshot() {
			if session != nil && (key == msg.CaseID || strings.HasPrefix(key, msg.CaseID+"@")) {
				if current := b.get(key); current == session {
					log.Printf("[Bridge] resolveInputSession: 命中本连接所属工单会话 key=%s", key)
					return session, key
				}
			}
		}
	}
	if s, key := b.onlySessionForCase(msg.CaseID); s != nil {
		log.Printf("[Bridge] resolveInputSession: 工单唯一会话兜底 key=%s", key)
		return s, key
	}
	return nil, ""
}

// onlySessionForCase 返回该工单唯一的 SSH 会话；有多条时返回 nil（宁可不装，也不猜节点）。
func (b *Bridge) onlySessionForCase(caseID string) (*sshx.SSHSession, string) {
	caseID = strings.TrimSpace(caseID)
	if caseID == "" {
		return nil, ""
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	var (
		found    *sshx.SSHSession
		foundKey string
	)
	prefix := caseID + "@"
	for key, session := range b.sessions {
		if session == nil || (key != caseID && !strings.HasPrefix(key, prefix)) {
			continue
		}
		if found != nil {
			return nil, ""
		}
		found, foundKey = session, key
	}
	return found, foundKey
}

// autoConnectNode 使用已保存的认证信息自动连接新节点
// connID/role 由调用方传入并写入 session.created：自动建连同样需要"谁建的"这一事实。
func (b *Bridge) autoConnectNode(ws *websocket.Conn, msg models.InMessage, ownedSessions *ownedSessionTracker, connID, role string) *sshx.SSHSession {
	if msg.NodeIP == "" {
		log.Printf("[Bridge] autoConnect: nodeIP 为空，跳过")
		return nil
	}

	auth, ok := b.getLastAuth(msg.CaseID)
	if !ok {
		log.Printf("[Bridge] autoConnect: case=%s 缺少认证信息 (lastAuth 中无此 caseID)", msg.CaseID)
		models.SendMsg(ws, models.OutMessage{
			Type: "ssh_error", CaseID: msg.CaseID,
			Message: fmt.Sprintf("无法连接节点 %s：缺少 SSH 认证信息，请先建立 SSH 连接", msg.NodeIP),
		})
		return nil
	}

	connectMsg := auth
	connectMsg.Type = "ssh_connect"
	connectMsg.Host = msg.NodeIP
	connectMsg.NodeIP = msg.NodeIP

	log.Printf("[Bridge] autoConnect: 开始连接节点 %s (复用 case=%s 的认证 user=%s)",
		msg.NodeIP, msg.CaseID, auth.Username)
	session, err := sshx.NewSSHSession(connectMsg)
	if err != nil {
		atomic.AddUint64(&promMetrics.SshConnectionErrors, 1)
		message, detail := sshx.BuildSSHError(err)
		models.SendMsg(ws, models.OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
		log.Printf("[Bridge] autoConnect: 连接失败 node=%s case=%s err=%v", msg.NodeIP, msg.CaseID, err)
		return nil
	}

	stdout, err := session.Start()
	if err != nil {
		atomic.AddUint64(&promMetrics.SshConnectionErrors, 1)
		session.Close()
		message, detail := sshx.BuildSSHError(err)
		models.SendMsg(ws, models.OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
		log.Printf("[Bridge] autoConnect: start 失败 node=%s case=%s err=%v", msg.NodeIP, msg.CaseID, err)
		return nil
	}

	key := sessionKey(msg.CaseID, msg.NodeIP)
	b.set(key, session)
	ownedSessions.set(key, session)
	atomic.AddUint64(&promMetrics.SshConnectionsTotal, 1)
	// 自动连接的节点会话同样要保活：它常被日志采集挑中，长跑期间最容易撞上半开连接。
	session.StartKeepAlive(sshx.SshKeepAliveInterval())
	// 自动建连也必须留痕：现场出现过"会话不是用户点出来的、而是 autoConnect 建的"，
	// 事后无法区分，只能靠 source 字段。
	logSessionCreated(session, key, connID, role, "auto_connect")

	go on_output_start(session, ws, stdout, msg.CaseID, func(reason string) {
		if reason == sessionReasonRemoteEOF {
			b.destroySession(key, sessionReasonRemoteEOF, connID)
			ownedSessions.remove(key)
			return
		}
		b.remove(key)
		ownedSessions.remove(key)
	})
	return session
}

// reconnectDeliveryNode 用已保存的认证信息后台重建到节点的 SSH 会话，不依赖任何浏览器连接。
//
// 为什么需要：交付（pull + 上传到平台）常跑十几分钟，而节点 SSH 存在 10 分钟会话上限
// （remote_eof），或面板 WebSocket 抖动也会导致会话随持有连接一起被释放。会话一旦消失，
// 交付就停在 needs_reauth，只能由用户回面板手动重连再续传——Q2026102701403 现场正是
// 传了 4/15 片后被打断，靠人工续传才收尾。桥本就持有 lastAuth（ssh_connect 的认证信息），
// 完全能在会话掉线后自动重连并续传，无需人工介入。
//
// 与 autoConnectNode 的区别：后者要给终端面板回发 ssh_output / exec_result，必须持 ws；
// 交付是后台作业，没有也不该绑定某个浏览器连接，所以输出监控走 nil ws（on_output_start
// 已对 nil ws 做保护，只保留 exec 结果回环与 EOF 销毁）。
func (b *Bridge) reconnectDeliveryNode(caseID, nodeIP, connID string) (*sshx.SSHSession, error) {
	if strings.TrimSpace(nodeIP) == "" {
		return nil, fmt.Errorf("nodeIP 为空，无法自动重连")
	}
	auth, ok := b.getLastAuth(caseID)
	if !ok {
		// 连认证都没存过：说明用户从未通过面板建立过该工单的 SSH 连接，
		// 这种"裸"交付本就该要求人工重连，不能假装能自动恢复。
		return nil, fmt.Errorf("缺少 SSH 认证信息（请先在面板建立 SSH 连接）")
	}
	connectMsg := auth
	connectMsg.Type = "ssh_connect"
	connectMsg.Host = nodeIP
	connectMsg.NodeIP = nodeIP

	session, err := sshx.NewSSHSession(connectMsg)
	if err != nil {
		return nil, fmt.Errorf("自动重连节点失败: %w", err)
	}
	stdout, err := session.Start()
	if err != nil {
		session.Close()
		return nil, fmt.Errorf("自动重连节点启动失败: %w", err)
	}
	key := sessionKey(caseID, nodeIP)
	b.set(key, session)
	atomic.AddUint64(&promMetrics.SshConnectionsTotal, 1)
	// 后台重连的会话同样要保活：它最容易在长跑期间撞上半开连接。
	session.StartKeepAlive(sshx.SshKeepAliveInterval())
	logSessionCreated(session, key, connID, "delivery", "delivery_auto_reconnect")
	logpush.Blog("WARN", "delivery.session_reconnected", "交付期间节点会话掉线，已用已存认证自动重连", "", caseID, nodeIP, "", map[string]any{
		"key":     key,
		"conn_id": connID,
	})
	// 与 autoConnectNode 同构：远端 EOF 时销毁会话（下一轮 advance 会再次自动重连），
	// 其余原因只摘键不销毁。
	go on_output_start(session, nil, stdout, caseID, func(reason string) {
		if reason == sessionReasonRemoteEOF {
			b.destroySession(key, sessionReasonRemoteEOF, connID)
		} else {
			b.remove(key)
		}
	})
	return session, nil
}

type ownedSessionTracker struct {
	mu       sync.Mutex
	sessions map[string]*sshx.SSHSession
}

func newOwnedSessionTracker() *ownedSessionTracker {
	return &ownedSessionTracker{sessions: make(map[string]*sshx.SSHSession)}
}

func (t *ownedSessionTracker) set(key string, session *sshx.SSHSession) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.sessions[key] = session
}

func (t *ownedSessionTracker) remove(key string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	delete(t.sessions, key)
}

// drainCase 取出当前 WebSocket 所拥有的指定工单全部会话，包括自动建立的节点会话。
func (t *ownedSessionTracker) drainCase(caseID string) map[string]*sshx.SSHSession {
	t.mu.Lock()
	defer t.mu.Unlock()
	sessions := make(map[string]*sshx.SSHSession)
	for key, session := range t.sessions {
		if key == caseID || strings.HasPrefix(key, caseID+"@") {
			sessions[key] = session
			delete(t.sessions, key)
		}
	}
	return sessions
}

// sessionsSnapshot 返回当前连接所拥有会话的浅拷贝，供只读遍历（持锁时间最短）。
func (t *ownedSessionTracker) sessionsSnapshot() map[string]*sshx.SSHSession {
	t.mu.Lock()
	defer t.mu.Unlock()
	snapshot := make(map[string]*sshx.SSHSession, len(t.sessions))
	for key, session := range t.sessions {
		snapshot[key] = session
	}
	return snapshot
}

func (t *ownedSessionTracker) drain() map[string]*sshx.SSHSession {
	t.mu.Lock()
	defer t.mu.Unlock()
	sessions := t.sessions
	t.sessions = make(map[string]*sshx.SSHSession)
	return sessions
}

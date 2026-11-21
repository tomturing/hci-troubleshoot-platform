package app

// Bridge 是装配壳：会话状态已下沉到 shared/session.Hub（见该包文档），
// 这里只保留仍需 app 编排的部分——自动建连、事件留痕、SFTP/acli 的过渡状态位。
//
// 为什么不再自持会话表：会话是终端、日志采集、交付、acli、SFTP 共同依赖的
// 核心资源，放在装配层会迫使 11 个业务域反向 import app。下沉后依赖方向变为
// 业务域 → shared/session → sshx，装配层只在 cmd/bridge 做注入。

import (
	"fmt"
	"sync"
	"sync/atomic"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/shared/obslog"
	"terminal_bridge/internal/shared/session"
	"terminal_bridge/internal/sshx"
	"terminal_bridge/internal/terminal"

	"golang.org/x/net/websocket"
)

type Bridge struct {
	// *session.Hub 提供 Get/Set/Remove/ResolveSession 等会话能力（方法提升）。
	*session.Hub
	// sftp 任务表与 acli 缓存是过渡期字段：实体归各自域，这里只保留类型擦除的槽位，
	// 阶段 2 第二刀把它们分别收进 sftp.TaskRegistry / acli.Store。
	SftpTasksMu sync.Mutex
	SftpTasks   map[string]any
	AcliMu      sync.Mutex
	AcliBusy    map[string]bool
	AcliCache   any
}

func NewBridge() *Bridge {
	return &Bridge{
		Hub:       session.NewHub(),
		SftpTasks: make(map[string]any),
	}
}

// NewBridgeWithSessions 是测试夹具：批量登记会话后返回 Bridge。
//
// 必须走 Set（而不是直写会话表）：Set 会同时登记 refs=1，与生产"入表即持有、
// 释放才空闲"的租约语义一致。绕过 Set 就会造出 refs=0 的会话，reaper 立刻可回收，
// 测试断言的"会话仍在"就成了侥幸。需要空闲态请显式调 ReleaseSession。
func NewBridgeWithSessions(sessions map[string]*sshx.SSHSession) *Bridge {
	b := NewBridge()
	for key, sess := range sessions {
		b.Set(key, sess)
	}
	return b
}

// SessionKey / SessionKeyCase 保留为 app 包的兼容入口，实现已归 shared/session。
func SessionKey(caseID, nodeIP string) string { return session.SessionKey(caseID, nodeIP) }

func SessionKeyCase(key string) string { return session.SessionKeyCase(key) }

// OwnedSessionTracker 是 session.OwnedTracker 的兼容别名（连接级会话所有权）。
type OwnedSessionTracker = session.OwnedTracker

func NewOwnedSessionTracker() *OwnedSessionTracker { return session.NewOwnedTracker() }

// 以下薄包装保留旧方法名，避免 40+ 处调用点在本次重构中同时改动；
// 真正的状态与并发控制都在 session.Hub 内。

func (b *Bridge) SessionCount() int { return b.Count() }

func (b *Bridge) SessionKeyList(limit int) []string { return b.Keys(limit) }

func (b *Bridge) ListSessions() []models.BridgeSessionInfo { return b.List() }

func (b *Bridge) ResolveSession(msg models.InMessage) (*sshx.SSHSession, string) {
	return b.Resolve(msg)
}

func (b *Bridge) ResolveAcliSyncSession(msg models.InMessage) (*sshx.SSHSession, string) {
	return b.ResolveAcliSync(msg)
}

func (b *Bridge) ResolveInputSession(msg models.InMessage, activeKey string, ownedSessions *OwnedSessionTracker) (*sshx.SSHSession, string) {
	return b.ResolveInput(msg, activeKey, ownedSessions)
}

type bridgeStatus struct {
	ActiveSessions  int `json:"active_sessions"`
	CachedAuthCases int `json:"cached_auth_cases"`
}

func (b *Bridge) status() bridgeStatus {
	sessions, cached := b.Counts()
	return bridgeStatus{ActiveSessions: sessions, CachedAuthCases: cached}
}

// AutoConnectNode 使用已保存的认证信息自动连接新节点。
// connID/role 由调用方传入并写入 session.created：自动建连同样需要"谁建的"这一事实。
func (b *Bridge) AutoConnectNode(ws *websocket.Conn, msg models.InMessage, ownedSessions *OwnedSessionTracker, connID, role string) *sshx.SSHSession {
	if msg.NodeIP == "" {
		obslog.Emit("INFO", "ssh.auto_connect_skipped", "自动建连跳过：nodeIP 为空", "", msg.CaseID, "", connID, nil)
		return nil
	}

	auth, ok := b.LastAuth(msg.CaseID)
	if !ok {
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

	session, err := sshx.NewSSHSession(connectMsg)
	if err != nil {
		atomic.AddUint64(&PromMetrics.SshConnectionErrors, 1)
		message, detail := sshx.BuildSSHError(err)
		models.SendMsg(ws, models.OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
		return nil
	}

	stdout, err := session.Start()
	if err != nil {
		atomic.AddUint64(&PromMetrics.SshConnectionErrors, 1)
		session.Close()
		message, detail := sshx.BuildSSHError(err)
		models.SendMsg(ws, models.OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
		return nil
	}

	key := SessionKey(msg.CaseID, msg.NodeIP)
	b.Set(key, session)
	ownedSessions.Set(key, session)
	atomic.AddUint64(&PromMetrics.SshConnectionsTotal, 1)
	// 自动连接的节点会话同样要保活：它常被日志采集挑中，长跑期间最容易撞上半开连接。
	session.StartKeepAlive(sshx.SshKeepAliveInterval())
	// 自动建连也必须留痕：现场出现过"会话不是用户点出来的、而是 autoConnect 建的"，
	// 事后无法区分，只能靠 source 字段。
	LogSessionCreated(session, key, connID, role, "auto_connect")

	go terminal.On_output_start(session, ws, stdout, msg.CaseID, func(reason string) {
		if reason == sshx.SessionReasonRemoteEOF {
			b.DestroySession(key, sshx.SessionReasonRemoteEOF, connID)
			ownedSessions.Remove(key)
			return
		}
		b.Remove(key)
		ownedSessions.Remove(key)
	})
	return session
}

// ReconnectDeliveryNode 用已保存的认证信息后台重建到节点的 SSH 会话，不依赖任何浏览器连接。
//
// 为什么需要：交付（pull + 上传到平台）常跑十几分钟，而节点 SSH 存在 10 分钟会话上限
// （remote_eof），或面板 WebSocket 抖动也会导致会话随持有连接一起被释放。会话一旦消失，
// 交付就停在 needs_reauth，只能由用户回面板手动重连再续传——Q2026102701403 现场正是
// 传了 4/15 片后被打断，靠人工续传才收尾。桥本就持有 lastAuth，完全能在会话掉线后
// 自动重连并续传，无需人工介入。
//
// 与 AutoConnectNode 的区别：后者要给终端面板回发 ssh_output / exec_result，必须持 ws；
// 交付是后台作业，没有也不该绑定某个浏览器连接，所以输出监控走 nil ws
// （terminal.On_output_start 已对 nil ws 做保护，只保留 exec 结果回环与 EOF 销毁）。
func (b *Bridge) ReconnectDeliveryNode(caseID, nodeIP, connID string) (*sshx.SSHSession, error) {
	if nodeIP == "" {
		return nil, fmt.Errorf("nodeIP 为空，无法自动重连")
	}
	auth, ok := b.LastAuth(caseID)
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
	key := SessionKey(caseID, nodeIP)
	b.Set(key, session)
	atomic.AddUint64(&PromMetrics.SshConnectionsTotal, 1)
	// 后台重连的会话同样要保活：它最容易在长跑期间撞上半开连接。
	session.StartKeepAlive(sshx.SshKeepAliveInterval())
	LogSessionCreated(session, key, connID, "delivery", "delivery_auto_reconnect")
	obslog.Emit("WARN", "delivery.session_reconnected", "交付期间节点会话掉线，已用已存认证自动重连", "", caseID, nodeIP, "", map[string]any{
		"key":     key,
		"conn_id": connID,
	})
	// 与 AutoConnectNode 同构：远端 EOF 时销毁会话（下一轮 advance 会再次自动重连），
	// 其余原因只摘键不销毁。
	go terminal.On_output_start(session, nil, stdout, caseID, func(reason string) {
		if reason == sshx.SessionReasonRemoteEOF {
			b.DestroySession(key, sshx.SessionReasonRemoteEOF, connID)
		} else {
			b.Remove(key)
		}
	})
	return session, nil
}

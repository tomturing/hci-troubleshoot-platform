package app

// Bridge 是装配壳：会话状态已下沉到 sshx/session.Hub（见该包文档），
// 这里只保留仍需 app 编排的部分——自动建连、事件留痕、SFTP/acli 的过渡状态位。
//
// 为什么不再自持会话表：会话是终端、日志采集、交付、acli、SFTP 共同依赖的
// 核心资源，放在装配层会迫使 11 个业务域反向 import app。下沉后依赖方向变为
// 业务域 → sshx/session → sshx，装配层只在 cmd/bridge 做注入。

import (
	"fmt"
	"golang.org/x/net/websocket"
	"sync"
	"sync/atomic"
	"terminal_bridge/internal/shared/metrics"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/shared/obslog"
	"terminal_bridge/internal/sshx"
	"terminal_bridge/internal/sshx/session"
	"terminal_bridge/internal/terminal"
)

type Bridge struct {
	// *session.Hub 提供 Get/Set/Remove/ResolveSession 等会话能力（方法提升）。
	*session.Hub
	// reconnectMu 串行化"会话缺失 → 自动重连"，避免多条消息同时触发建连：
	// 采集轮询与交付续传可能在同一瞬间都发现会话没了，并发重连会开出多条 SSH。
	reconnectMu sync.Mutex
}

func NewBridge() *Bridge {
	return &Bridge{Hub: session.NewHub()}
}

// SessionKey / SessionKeyCase 保留为 app 包的兼容入口，实现已归 shared/session。
func SessionKey(caseID, nodeIP string) string { return session.SessionKey(caseID, nodeIP) }

func SessionKeyCase(key string) string { return session.SessionKeyCase(key) }

// OwnedSessionTracker 是 session.OwnedTracker 的兼容别名（连接级会话所有权）。
type OwnedSessionTracker = session.OwnedTracker

func NewOwnedSessionTracker() *OwnedSessionTracker { return session.NewOwnedTracker() }

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
		atomic.AddUint64(&metrics.PromMetrics.SshConnectionErrors, 1)
		message, detail := sshx.BuildSSHError(err)
		models.SendMsg(ws, models.OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
		return nil
	}

	stdout, err := session.Start()
	if err != nil {
		atomic.AddUint64(&metrics.PromMetrics.SshConnectionErrors, 1)
		session.Close()
		message, detail := sshx.BuildSSHError(err)
		models.SendMsg(ws, models.OutMessage{Type: "ssh_error", CaseID: msg.CaseID, Message: message, Detail: detail})
		return nil
	}

	key := SessionKey(msg.CaseID, msg.NodeIP)
	b.Set(key, session)
	ownedSessions.Set(key, session)
	atomic.AddUint64(&metrics.PromMetrics.SshConnectionsTotal, 1)
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
	return b.reconnectNode(caseID, nodeIP, connID, "delivery", "delivery_auto_reconnect")
}

// ReconnectSession 是会话自愈的统一入口：任何业务发现"键对不上"时都可能用它。
//
// 第一性原理：长连接必然失效（节点 sshd 重启、网络闪断、bridge 进程重启都会让会话消失），
// 靠"永远不掉"是不成立的。桥已经持有 lastAuth，唯一正确的做法是掉了就用它重建一次，
// 而不是把"请重新登录"甩给用户——日志收集一跑几十分钟，中途关页面是常态。
func (b *Bridge) ReconnectSession(caseID, nodeIP, connID string) (*sshx.SSHSession, error) {
	if caseID == "" || nodeIP == "" {
		return nil, fmt.Errorf("缺少工单号或节点 IP，无法自动重连")
	}
	key := SessionKey(caseID, nodeIP)
	// 双检：等锁期间可能已经有人重连成功，直接复用，不再开第二条 SSH。
	b.reconnectMu.Lock()
	defer b.reconnectMu.Unlock()
	if s := b.Get(key); s != nil {
		return s, nil
	}
	return b.reconnectNode(caseID, nodeIP, connID, "log_collect", "session_auto_reconnect")
}

// reconnectNode 是交付与日志收集共用的重连实现（差异只在归因字段）。
func (b *Bridge) reconnectNode(caseID, nodeIP, connID, role, source string) (*sshx.SSHSession, error) {
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
	atomic.AddUint64(&metrics.PromMetrics.SshConnectionsTotal, 1)
	// 后台重连的会话同样要保活：它最容易在长跑期间撞上半开连接。
	session.StartKeepAlive(sshx.SshKeepAliveInterval())
	LogSessionCreated(session, key, connID, role, source)
	obslog.Emit("WARN", "ssh.session_reconnected", "节点会话掉线，已用已存认证自动重连", "", caseID, nodeIP, "", map[string]any{
		"key": key, "conn_id": connID, "role": role, "source": source,
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

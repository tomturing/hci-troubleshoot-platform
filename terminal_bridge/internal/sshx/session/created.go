package session

import (
	"strings"
	"time"

	"terminal_bridge/internal/shared/obslog"
	"terminal_bridge/internal/sshx"
)

// LogCreated 记录会话建立（含建立它的连接与角色）。
//
// 为什么归会话包而不是装配层：这条事件必须与 session.destroyed 成对出现，
// 只有销毁没有建立时无法区分「会话从没建过」和「建了又被清掉」——两种处置
// 完全不同。事件属于会话生命周期本身，写在会话所有者里才不会漏。
func LogCreated(s *sshx.SSHSession, key, connID, role, source string) {
	if s == nil {
		return
	}
	s.OwnerConn = connID
	s.CreatedAt = time.Now()
	obslog.Emit("INFO", "session.created", "SSH 会话已建立", "", SessionKeyCase(key), NodeFromKey(key), "", map[string]any{
		"key":        key,
		"conn_id":    connID,
		"role":       role,
		"source":     source, // ssh_connect | auto_connect | delivery_auto_reconnect
		"address":    s.Address,
		"simulation": s.Simulation,
	})
}

// NodeFromKey 从会话键里取回节点 IP（`caseID@nodeIP` → `nodeIP`）。
//
// 键不含 `@` 时返回空串：这类会话来自建单集成流程（未带 node_ip，键退化为 caseID），
// 它们的 node_ip 字段只能为空，不能猜测。
func NodeFromKey(key string) string {
	if _, node, ok := strings.Cut(key, "@"); ok {
		return node
	}
	return ""
}

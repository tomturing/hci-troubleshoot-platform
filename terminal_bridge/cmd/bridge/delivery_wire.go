package main

// delivery_wire.go 是交付域的端口接线：delivery 只认识 NodeSession / Reconnector
// 两个端口（不 import sshx 具体类型），把 *sshx.SSHSession 适配成端口是装配层的活。

import (
	"terminal_bridge/internal/app"
	"terminal_bridge/internal/delivery"
	"terminal_bridge/internal/sshx"
)

// deliveryReconnector 把装配层的会话能力适配成交付域端口。
type deliveryReconnector struct {
	b *app.Bridge
}

// Wrap 把注册表里已有的会话适配成节点端口；键不存在时返回 nil（由调用方转 needs_reauth）。
func (a deliveryReconnector) Wrap(key string) delivery.NodeSession {
	sess := a.b.Get(key)
	if sess == nil {
		return nil
	}
	// 已关闭但尚未摘表的会话仍会挂在注册表里；交给交付域之前先判活，
	// 否则每轮 acli 命令都会得到 no-session（Q2026112500592）。
	if !sess.Alive() {
		return nil
	}
	return sshx.NewSSHRunner(sess)
}

// ReconnectDeliveryNode 复用装配层的自动重连（它持有 lastAuth 与建连逻辑）。
func (a deliveryReconnector) ReconnectDeliveryNode(caseID, nodeIP, connID string) (delivery.NodeSession, error) {
	sess, err := a.b.ReconnectDeliveryNode(caseID, nodeIP, connID)
	if err != nil {
		return nil, err
	}
	if sess == nil {
		return nil, nil
	}
	return sshx.NewSSHRunner(sess), nil
}

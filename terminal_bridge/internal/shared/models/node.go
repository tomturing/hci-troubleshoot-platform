package models

// NodeOption 是 acli 上报的集群节点清单项（跨进程契约：桥 → 前端下拉）。
//
// 第一性原理：目标主机必须来自被采集集群的事实，而不是让用户凭记忆手填 IP。
// 放在 models 是因为它同时出现在三个位置：WS 出站消息的 hosts 字段、
// 集群拓扑解析结果（shared/cluster）、以及各域的诊断字段。
type NodeOption struct {
	IP     string `json:"ip"`
	Name   string `json:"name,omitempty"`
	Master bool   `json:"master,omitempty"`
}

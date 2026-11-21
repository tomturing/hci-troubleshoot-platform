// Package cluster 是「一个 HCI 集群由哪些节点组成、如何在节点间执行」的事实层。
//
// 为什么独立成包：这套能力原先叫 Spc*（日志收集场景清单下发的第一步），但实际上
// acli 插件同步、安装后落盘校验同样需要它——它描述的是集群拓扑与跨节点扇出，
// 与"场景清单"没有任何关系。名字错位导致 acli 为了拿节点清单反向依赖 logcollect
// （阶段 2 A3 审查项）。
//
// 分层：本包只认识 remote 端口（Runner/StreamsRunner/PtyRunner）与 models DTO，
// 不认识 sshx 具体类型，因此 acli 与 logcollect 都能依赖它而不产生反向边。
package cluster

import (
	"context"
	"encoding/json"
	"regexp"
	"strings"
	"time"

	"terminal_bridge/internal/shared/aclibin"
	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/shared/remote"
)

// FanoutConcurrency 是跨节点执行的并发上限。
//
// 每台主机要跑 3 次 SSH（探测/备份/写入），全串行排不进 180s 动作预算，
// 全并发又会打满被采节点的 sshd。
const FanoutConcurrency = 4

// CommandTimeout 是单台单步上限：BatchMode + ConnectTimeout=5 之后最坏 5s 建连，
// 30s 能覆盖 find 与回读校验，又能把"某台卡住"限制在局部。
const CommandTimeout = 30 * time.Second

// LogCollectSpcDir 是日志收集插件自部署的场景清单目录。
//
// 它是插件布局事实（acli 装出来的固定路径），不是 logcollect 私有实现：
// acli 侧诊断与 logcollect 侧写入必须指向同一个目录。
const LogCollectSpcDir = "/sf/data/local/acli/plugins/log_collect/tools/lmt_collect_info/support_bundle_cluster/spc"

// ipv4Re 只做粗筛（点分四段），逐段范围与去前导零由 ValidIPv4 判定。
var ipv4Re = regexp.MustCompile(`^(\d{1,3}\.){3}\d{1,3}$`)

// ValidIPv4 与采集参数校验使用同一套判定：每个点分段必须是 0-255，
// 避免把 "1.2.3.999" 这类字符串拼进 ssh 目标。
func ValidIPv4(ip string) bool {
	if !ipv4Re.MatchString(ip) {
		return false
	}
	for _, part := range strings.Split(ip, ".") {
		if len(part) > 1 && part[0] == '0' {
			return false
		}
		value := 0
		for _, ch := range part {
			if ch < '0' || ch > '9' {
				return false
			}
			value = value*10 + int(ch-'0')
		}
		if value > 255 {
			return false
		}
	}
	return true
}

// nodeListPayload 是 `platform node list` 的 JSON 结构。
type nodeListPayload struct {
	Data []struct {
		IP     string `json:"ip"`
		Name   string `json:"name"`
		Master int    `json:"master"`
	} `json:"data"`
}

// findNodeListPayload 从脏输出中找出节点清单 JSON 对象。
//
// 对抗性审查：不能再用"首个 `{` 到末个 `}`"截取。acli 会在 stdout 里先打一行
// `{"@timestamp":...,"level":"error",...}` 形式的运行日志（2026-10-28 现场：
// open /sys/fs/cgroup/cpu/cpuset.cpus 失败），首个 `{` 命中日志行、末个 `}` 命中
// 节点 JSON，拼起来必然 Unmarshal 失败——即使节点 JSON 本身完好，清单也会是空的。
//
// 改为从每个 `{` 起点解码一个 JSON 值：json.Decoder 解析完对象即停，尾部日志
// 不参与解析，只接受含 data 数组的对象。
func findNodeListPayload(raw string) *nodeListPayload {
	for offset := 0; offset < len(raw); {
		idx := strings.IndexByte(raw[offset:], '{')
		if idx < 0 {
			return nil
		}
		start := offset + idx
		var payload nodeListPayload
		if err := json.NewDecoder(strings.NewReader(raw[start:])).Decode(&payload); err == nil && payload.Data != nil {
			return &payload
		}
		offset = start + 1
	}
	return nil
}

// ParseNodeList 解析 `acli --formatter json platform node list` 的输出。
// PTY 可能混入提示符、告警或日志行；失败返回 nil（前端降级为手填）。
func ParseNodeList(raw string) []models.NodeOption {
	payload := findNodeListPayload(raw)
	if payload == nil {
		return nil
	}

	seen := make(map[string]bool, len(payload.Data))
	hosts := make([]models.NodeOption, 0, len(payload.Data))
	for _, node := range payload.Data {
		ip := strings.TrimSpace(node.IP)
		if !ipv4Re.MatchString(ip) || seen[ip] {
			continue
		}
		seen[ip] = true
		hosts = append(hosts, models.NodeOption{
			IP:     ip,
			Name:   strings.TrimSpace(node.Name),
			Master: node.Master == 1,
		})
	}
	return hosts
}

// localAddressCommand 探测本机 IPv4。
//
// 为什么必须两种方式都试：HCI 节点上 hostname 的 -I 参数依赖 coreutils/busybox 版本，
// 部分裁剪系统直接报错；ip 命令则可能没装。任一成功即可，都失败就退化为"全部走 ssh"。
const localAddressCommand = "hostname -I 2>/dev/null; ip -4 -o addr show 2>/dev/null | awk '{print $4}'"

// ParseLocalAddresses 归一化本机地址探测结果。
// 命中本机的主机直接在当前会话执行，绕开"节点能否 ssh 自己"这个额外前提。
func ParseLocalAddresses(output string) map[string]bool {
	out := map[string]bool{}
	for _, field := range strings.Fields(strings.ReplaceAll(output, "\r", "\n")) {
		ip := field
		if index := strings.Index(ip, "/"); index > 0 {
			ip = ip[:index]
		}
		if ValidIPv4(ip) {
			out[ip] = true
		}
	}
	return out
}

// LocalAddresses 探测本机 IPv4；探测失败返回空集合（等价于全部走 ssh）。
func LocalAddresses(r remote.QuickRunner, ctx context.Context) map[string]bool {
	if r == nil {
		return map[string]bool{}
	}
	probeCtx, cancel := context.WithTimeout(ctx, CommandTimeout)
	defer cancel()
	output, _, err := r.RunQuickCommand(probeCtx, localAddressCommand)
	if err != nil {
		return map[string]bool{}
	}
	return ParseLocalAddresses(output)
}

// NodeList 取集群节点清单（与采集探测同源命令），用于把 all 解析成具体主机集合。
func NodeList(r remote.PtyRunner, ctx context.Context) []models.NodeOption {
	if r == nil {
		return nil
	}
	probeCtx, cancel := context.WithTimeout(ctx, CommandTimeout)
	defer cancel()
	output, _, err := r.RunCommandPty(probeCtx, aclibin.Invoke("--formatter json platform node list"), CommandTimeout)
	if err != nil {
		return nil
	}
	return ParseNodeList(output)
}

// BuildSSHCommand 拼出"从发起节点跳到目标节点"的命令。
//
// 关键约束：
//   - BatchMode=yes：没有免密就立刻失败，绝不能让 ssh 在节点上等密码输入；
//   - ConnectTimeout=5：不可达节点必须在 5s 内失败，否则并发窗口会被一台死机拖满；
//   - root@：bridge 登录用户已在装配层归一为 root，写插件目录依赖这一点。
func BuildSSHCommand(ip, script string) string {
	return "ssh -o BatchMode=yes -o ConnectTimeout=5 root@" + ip + " " + remote.ShellQuote(script)
}

// RunOnHostStreams 在目标主机上执行一段脚本：本机直连当前会话，其余节点走一次 ssh。
//
// 为什么 stdout 与 stderr 必须分开返回：
//  1. stderr 是「真实原因」的唯一来源（read-only file system、permission denied、
//     cp: cannot stat）。旧实现走 runQuickCommand，它只在 stdout 为空时才拿 stderr
//     兜底——脚本先打赢了 marker 再失败时，原因就被静默丢弃，界面只剩一句
//     「场景清单写入失败」（2026-11-12 Q2026110302155 现场）。
//  2. stderr 不能直接拼进 stdout：目录探测把任何以 "/" 开头的行当候选目录，
//     "/bin/sh: line 1: ..." 这类行会被算成第二个目录，凭空报「目录不唯一」。
func RunOnHostStreams(r remote.QuickStreamsRunner, ctx context.Context, ip string, local bool, script string) (stdout, stderr string, exitCode int, err error) {
	if r == nil {
		return "", "no-session", 0, context.Canceled
	}
	command := script
	if !local {
		command = BuildSSHCommand(ip, script)
	}
	hostCtx, cancel := context.WithTimeout(ctx, CommandTimeout)
	defer cancel()
	return r.RunQuickCommandStreams(hostCtx, command)
}

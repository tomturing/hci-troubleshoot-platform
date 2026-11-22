package app

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	bruntime "terminal_bridge/internal/shared/runtime"
	"time"
)

const (
	DefaultWSPort        = 9999
	BackupWSPort         = 47324            // 主端口被陌生进程占用时的固定回退端口；与前端探测的已知端口一致
	WebsocketPingInteval = 30 * time.Second // WebSocket 心跳间隔：每 30 秒发送 ping，防止空闲超时断开
	// 输入被丢弃时的提示节流窗口已归 wsctx.InputRejectNoticeInterval：
	// 它约束的是连接级状态 ctx.LastInputRejectAt，由域 handler 直接使用。
)

const portBindRetryAttempts = 3

type runtimeConfig struct {
	Mode              string
	ListenAddress     string
	Port              int
	AllowedOrigins    []string
	AllowedOriginsRaw string
}

// BridgeSelfPort 记录启动时最终生效的监听端口，供对端不可达辅助探测回查 bridge 自身 /health/live（修复 E）。
var BridgeSelfPort int

func NormalizeRuntimeConfig(mode, listenAddress string, port int, allowedOrigins string) (runtimeConfig, error) {
	mode = strings.ToLower(strings.TrimSpace(mode))
	if mode == "" {
		mode = bruntime.DesktopMode
	}
	if mode != bruntime.DesktopMode && mode != bruntime.ClusterMode {
		return runtimeConfig{}, fmt.Errorf("不支持的运行模式 %q，仅支持 %s 或 %s", mode, bruntime.DesktopMode, bruntime.ClusterMode)
	}

	listenAddress = strings.TrimSpace(listenAddress)
	if listenAddress == "" {
		if mode == bruntime.ClusterMode {
			listenAddress = "0.0.0.0"
		} else {
			listenAddress = "127.0.0.1"
		}
	}
	if port < 1 || port > 65535 {
		return runtimeConfig{}, fmt.Errorf("监听端口必须在 1-65535 范围内，当前值为 %d", port)
	}

	allowedOrigins = strings.TrimSpace(allowedOrigins)
	if allowedOrigins == "" {
		if mode == bruntime.ClusterMode {
			allowedOrigins = "same-origin"
		} else {
			allowedOrigins = "*"
		}
	}
	origins := make([]string, 0, 4)
	for _, item := range strings.Split(allowedOrigins, ",") {
		item = strings.TrimRight(strings.TrimSpace(item), "/")
		if item != "" {
			origins = append(origins, item)
		}
	}
	if len(origins) == 0 {
		return runtimeConfig{}, fmt.Errorf("至少需要配置一个允许的 Origin")
	}

	return runtimeConfig{
		Mode:              mode,
		ListenAddress:     listenAddress,
		Port:              port,
		AllowedOrigins:    origins,
		AllowedOriginsRaw: allowedOrigins,
	}, nil
}

func (c runtimeConfig) Address() string {
	return net.JoinHostPort(c.ListenAddress, strconv.Itoa(c.Port))
}

// isAddressInUse 判断 bind 错误是否为"该地址当前不可绑定"，这是端口自愈
// （存活 Bridge 探测 → 备用端口回退 → fail-fast）的唯一入口判据。
//
// 判定顺序按可靠度排：先取 socket 层 errno，再退回错误文案匹配。
// 只按文案判定是有前车之鉴的：Windows 的 bind 文案由 FormatMessage 本地化，
// 中文系统上是"通常每个套接字地址(协议/网络地址/端口)只允许使用一次。"，
// 英文子串永远匹配不到——历史实现在中文客户机上等于自愈从未生效。
// 更关键的是 WSAEACCES(10013)："An attempt was made to access a socket in a way
// forbidden by its access permissions."，它同样表示"这个地址绑不上"，但文案里
// 没有 "address already in use"，旧逻辑会把它当成不可恢复错误直接 log.Fatalf
// （2026-11-19 客户机 127.0.0.1:9999 被 PID 占用即此形态，表现为双击闪退）。
func isAddressInUse(err error) bool {
	if err == nil {
		return false
	}
	if isPortBindUnavailableErrno(err) {
		return true
	}
	message := strings.ToLower(err.Error())
	return strings.Contains(message, "address already in use") ||
		strings.Contains(message, "only one usage of each socket address") ||
		strings.Contains(message, "forbidden by its access permissions")
}

// listenWithRetry 建立 TCP 监听，对可恢复的"端口占用"做有限次指数退避重试，
// 抹平崩溃重启后的 TIME_WAIT 短暂占用；非占用类错误立即返回不重试。
func listenWithRetry(network, address string, attempts int) (net.Listener, error) {
	var lastErr error
	delay := 200 * time.Millisecond
	for attempt := 0; attempt < attempts; attempt++ {
		listener, err := net.Listen(network, address)
		if err == nil {
			return listener, nil
		}
		lastErr = err
		if !isAddressInUse(err) {
			return nil, err
		}
		if attempt < attempts-1 {
			time.Sleep(delay)
			delay *= 2
		}
	}
	return nil, lastErr
}

// IsLiveBridge 探测给定地址上是否已有一个存活的 terminal_bridge。
// 通过 Bridge 自带的 /health/live 端点判定，用于把"重复启动/上一个没退净"
// 与"陌生进程占用端口"区分开。任何探测失败都安全地视为"非存活 Bridge"。
func IsLiveBridge(address string) bool {
	client := &http.Client{Timeout: 800 * time.Millisecond}
	resp, err := client.Get("http://" + address + "/health/live")
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return false
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 4096))
	if err != nil {
		return false
	}
	var payload map[string]any
	if err := json.Unmarshal(body, &payload); err != nil {
		return false
	}
	return fmt.Sprint(payload["status"]) == "ok"
}

// FindPortHolders 尽力探测占用某 TCP 端口的进程 PID，仅用于 fail-fast 诊断提示。
// 跨平台 best-effort：任何失败都返回空切片，绝不阻断启动主流程。
func FindPortHolders(port int) []string {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "windows":
		cmd = exec.Command("netstat", "-ano", "-p", "tcp")
	default:
		cmd = exec.Command("sh", "-c", "lsof -nP -iTCP:"+strconv.Itoa(port)+" -sTCP:LISTEN")
	}
	output, err := cmd.Output()
	if err != nil {
		return nil
	}
	portSuffix := ":" + strconv.Itoa(port)
	seen := make(map[string]struct{})
	holders := make([]string, 0, 4)
	for _, line := range strings.Split(string(output), "\n") {
		fields := strings.Fields(line)
		if len(fields) == 0 {
			continue
		}
		matched := strings.Contains(line, portSuffix)
		if !matched {
			continue
		}
		var pid string
		if runtime.GOOS == "windows" {
			// netstat: Proto Local Foreign State PID —— PID 取行末字段
			if !strings.Contains(strings.ToUpper(line), "LISTENING") {
				continue
			}
			pid = fields[len(fields)-1]
		} else {
			// lsof: COMMAND PID USER ... —— PID 取第二字段
			if len(fields) < 2 {
				continue
			}
			pid = fields[1]
		}
		if pid == "" || pid == "PID" {
			continue
		}
		if _, dup := seen[pid]; !dup {
			seen[pid] = struct{}{}
			holders = append(holders, pid)
		}
	}
	return holders
}

// AcquireListener 按"主端口 → 存活检测幂等 → 备用端口回退 → fail-fast"策略确定监听器。
// allowBackup 为真（默认端口的桌面模式）时，主端口被陌生进程占用则回退到 BackupWSPort；
// 否则或主备皆占时返回错误，由调用方输出诊断并退出。返回值 resolved 为最终生效配置（端口
// 可能已切到备用），alreadyRunning 表示同机已有存活 Bridge（应幂等退出而非报错）。
func AcquireListener(config runtimeConfig, allowBackup bool) (listener net.Listener, resolved runtimeConfig, alreadyRunning bool, err error) {
	resolved = config
	listener, err = listenWithRetry("tcp", config.Address(), portBindRetryAttempts)
	if err == nil {
		return listener, resolved, false, nil
	}
	if !isAddressInUse(err) {
		return nil, resolved, false, err
	}

	// 主端口不可绑定：先判断占用者是否为存活的 Bridge（重复启动场景）。
	if IsLiveBridge(config.Address()) {
		return nil, resolved, true, nil
	}

	if allowBackup && config.Port != BackupWSPort {
		backupConfig := config
		backupConfig.Port = BackupWSPort
		backupListener, backupErr := listenWithRetry("tcp", backupConfig.Address(), portBindRetryAttempts)
		if backupErr == nil {
			return backupListener, backupConfig, false, nil
		}
		if isAddressInUse(backupErr) {
			// 文案用"不可绑定"而非"被占用"：WSAEACCES(10013) 的成因可能是端口落在系统/WinNAT
			// 保留段，此时 netstat 里根本看不到占用者，写"被占用"会把现场引向错误方向。
			return nil, resolved, false, fmt.Errorf("主端口 %d 与备用端口 %d 均不可绑定（被占用或被系统保留）: %w", config.Port, BackupWSPort, backupErr)
		}
		return nil, resolved, false, backupErr
	}

	return nil, resolved, false, fmt.Errorf("端口 %d 不可绑定（被占用或被系统保留）: %w", config.Port, err)
}

func (c runtimeConfig) OriginAllowed(origin, requestHost string) bool {
	if origin == "" {
		// 非浏览器客户端可能不发送 Origin；鉴权和访问控制由上层网络边界负责。
		return true
	}
	normalizedOrigin := strings.TrimRight(strings.TrimSpace(origin), "/")
	for _, allowed := range c.AllowedOrigins {
		switch allowed {
		case "*":
			return true
		case "same-origin":
			parsed, err := url.Parse(normalizedOrigin)
			if err == nil && strings.EqualFold(parsed.Host, requestHost) {
				return true
			}
		default:
			if strings.EqualFold(normalizedOrigin, allowed) {
				return true
			}
		}
	}
	return false
}

package main

// acli 插件期望态清单（acli_sync 第二阶段的事实源）。
//
// 第一性原理：桥跑在客户机上，是**不受信进程**。让它读平台数据面的唯一方式是
// 给它一张短期、只读、绑定工单的票据——不是管理令牌，不是客户身份。因此这里
// 只出现两个凭据相关入参：platform_url + plugin_ticket，其余一律由平台侧裁决。
//
// 契约冻结：WS 消息只增这两个可选字段。旧桥不认识 → 本文件的所有代码路径都不
// 会被触发（阶段二不执行），阶段一与终端/日志业务不受影响。

import (
	"terminal_bridge/internal/shared/uplink"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

// 设备侧固定路径：与 case-service 的 spec 域层同源（改一处必须同步改另一处）。
const (
	acliPluginBaseDir      = "/sf/data/local/acli/plugins"
	acliPluginRegisterPath = acliPluginBaseDir + "/plugin_register.json"
	acliPluginLockPath     = acliPluginBaseDir + "/plugin_register.lock"
	acliPluginCompletion   = "/sf/cfg/acli/etc/completion/bash_completion.conf"
	acliPluginTmpDir       = "/sf/data/local/acli/tmp"
	acliPluginBin          = "/sf/bin/acli"
)

const (
	// acliPluginFetchTimeout 只覆盖两次 GET（manifest 与包），不含设备侧执行。
	acliPluginFetchTimeout = 60 * time.Second
	// acliPluginManifestMaxBytes 是清单上限：内联文件通道的物理约束。
	// 平台侧规格已限单文件与总量，这里再兜一层，防上游被改写或版本错配。
	acliPluginManifestMaxBytes = 16 << 20
	// acliPluginMaxFiles / acliPluginMaxFileBytes 与 case-service 的规格校验同口径。
	acliPluginMaxFiles     = 64
	acliPluginMaxFileBytes = 512 << 10
	// acliPluginHostProbeTimeout 单台一次探测：注册表 + 逐文件摘要，都是本地小命令。
	acliPluginHostProbeTimeout = 45 * time.Second
	// acliPluginHostDeployTimeout 单台一次部署：备份→写盘→jq 注册→plugin list→冒烟。
	// 比探测长一个量级是必须的，冒烟命令会真的跑插件。
	acliPluginHostDeployTimeout = 8 * time.Minute
	// acliPluginStageBudget 是整个第二阶段的总预算。它与阶段一的 720s 会话预算
	// 互相独立（阶段二自己拿一份 context），但必须**小于**前端 acli_sync 等待器的
	// 总预算（ACLI_SYNC_TIMEOUT_MS）：前端先超时的话，桥还在装插件，界面已经报超时，
	// 用户重点一次就会叠出第二份并发部署（阶段一 92s×3 的同族现场）。
	acliPluginStageBudget = 30 * time.Minute
	// acliPluginMaxPlugins 防呆：清单里插件数上限。
	acliPluginMaxPlugins = 32
)

var (
	acliPluginNameRe    = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)
	acliPluginHexRe     = regexp.MustCompile(`^[0-9a-f]{64}$`)
	acliPluginModeRe    = regexp.MustCompile(`^[0-7]{4}$`)
	acliPluginSmokeRe   = regexp.MustCompile(`^[A-Za-z0-9_.=:/@, -]*$`)
	acliPluginRelPathRe = regexp.MustCompile(`^[A-Za-z0-9._/-]{1,255}$`)
)

// acliPluginFile 是期望落盘的一个文件：内容 + 权限 + 摘要。
// 摘要由平台在保存规格时算定并固化，桥只复核、不重算。
type acliPluginFile struct {
	Path    string `json:"path"`
	Mode    string `json:"mode"`
	SHA256  string `json:"sha256"`
	Content string `json:"content"`
}

// acliPluginDesired 是一个插件的期望态（平台 manifest 的一项）。
type acliPluginDesired struct {
	Name             string           `json:"name"`
	Version          string           `json:"version"`
	SpecHash         string           `json:"spec_hash"`
	UpdateMode       string           `json:"update_mode"`
	BuiltIn          bool             `json:"built_in"`
	SmokeArgs        string           `json:"smoke_args"`
	EnableCompletion bool             `json:"enable_completion"`
	Size             int64            `json:"size"`
	PackageRef       string           `json:"package_ref"`
	SHA256           string           `json:"sha256"`
	Files            []acliPluginFile `json:"files"`
	RegisterItem     map[string]any   `json:"register_item"`
}

// manifestResponse 与 case-service GET /internal/acli-plugins/manifest 的响应同构。
type acliPluginManifestResponse struct {
	ManifestVersion string              `json:"manifest_version"`
	CaseID          string              `json:"case_id"`
	Plugins         []acliPluginDesired `json:"plugins"`
}

// acliPluginError 是阶段二的原因码载体：回执里必须能回答"到底哪一步、为什么"，
// 而不是只剩一句"插件同步失败"。
type acliPluginError struct {
	Code   string
	Detail string
}

func (e *acliPluginError) Error() string {
	if e.Detail == "" {
		return e.Code
	}
	return e.Code + ": " + e.Detail
}

func newAcliPluginError(code, detail string) error {
	return &acliPluginError{Code: code, Detail: detail}
}

// acliPluginErrorCode 取原因码；非结构化错误统一归到 stage2_unknown。
func acliPluginErrorCode(err error) string {
	var typed *acliPluginError
	if asAcliPluginError(err, &typed) {
		return typed.Code
	}
	return "stage2_unknown"
}

func asAcliPluginError(err error, target **acliPluginError) bool {
	typed, ok := err.(*acliPluginError)
	if ok {
		*target = typed
	}
	return ok
}

// validatePluginPlatformURL 校验平台根地址。
//
// 只允许 https：票据虽然只读，明文 HTTP 下一样能被中间人拿去拉 manifest 与包。
// 唯一例外是回环地址——本地联调时平台确实跑在 http://127.0.0.1:xxxx，这条
// 不存在公网中间人风险；其他 host 一律拒绝（收口设计 §3.3「http:// 一律拒绝」）。
func validatePluginPlatformURL(raw string) (string, error) {
	trimmed := strings.TrimRight(strings.TrimSpace(raw), "/")
	if trimmed == "" {
		return "", newAcliPluginError("platform_url_missing", "platform_url 为空")
	}
	parsed, err := url.Parse(trimmed)
	if err != nil {
		return "", newAcliPluginError("platform_url_invalid", err.Error())
	}
	if parsed.Host == "" {
		return "", newAcliPluginError("platform_url_invalid", "缺少主机名")
	}
	switch parsed.Scheme {
	case "https":
	case "http":
		host := parsed.Hostname()
		ip := net.ParseIP(host)
		loopback := ip != nil && ip.IsLoopback() || host == "localhost"
		if !loopback {
			return "", newAcliPluginError("platform_url_insecure",
				fmt.Sprintf("仅允许 https 平台地址（当前 %s）", trimmed))
		}
	default:
		return "", newAcliPluginError("platform_url_invalid",
			fmt.Sprintf("不支持的协议 %q", parsed.Scheme))
	}
	return trimmed, nil
}

// fetchAcliPluginManifest 用票据拉取本工单的插件期望态。
//
// 失败一律回原因码，绝不回空清单当"没有插件要装"：那会把一次网络故障
// 洗成一次成功的同步（B6 同族错误）。
func (b *Bridge) fetchAcliPluginManifest(
	ctx context.Context,
	platformURL, ticket, caseID string,
	insecure bool,
) (*acliPluginManifestResponse, error) {
	base, err := validatePluginPlatformURL(platformURL)
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(ticket) == "" {
		return nil, newAcliPluginError("plugin_ticket_missing", "缺少 plugin_ticket")
	}
	if strings.TrimSpace(caseID) == "" {
		return nil, newAcliPluginError("case_id_missing", "acli_sync 未带 case_id")
	}
	endpoint := base + "/api/acli-plugins/manifest?case_id=" + url.QueryEscape(strings.TrimSpace(caseID))
	reqCtx, cancel := context.WithTimeout(ctx, acliPluginFetchTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, newAcliPluginError("manifest_request_failed", err.Error())
	}
	req.Header.Set("X-Plugin-Ticket", ticket)
	req.Header.Set("Accept", "application/json")
	resp, err := uplink.HttpClientFor(insecure, acliPluginFetchTimeout).Do(req)
	if err != nil {
		return nil, newAcliPluginError("manifest_unreachable", err.Error())
	}
	defer resp.Body.Close()
	switch resp.StatusCode {
	case http.StatusOK:
	case http.StatusUnauthorized:
		return nil, newAcliPluginError("plugin_ticket_rejected", "平台返回 401：票据缺失/伪造/过期")
	default:
		return nil, newAcliPluginError("manifest_http_error", fmt.Sprintf("平台返回 HTTP %d", resp.StatusCode))
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, acliPluginManifestMaxBytes+1))
	if err != nil {
		return nil, newAcliPluginError("manifest_read_failed", err.Error())
	}
	if int64(len(body)) > acliPluginManifestMaxBytes {
		return nil, newAcliPluginError("manifest_too_large",
			fmt.Sprintf("清单超过 %d 字节上限", acliPluginManifestMaxBytes))
	}
	return parseAcliPluginManifest(body, strings.TrimSpace(caseID))
}

// parseAcliPluginManifest 解析并逐项校验清单。
//
// 桥必须二次校验（前端与平台之间隔着不受信链路，清单内容最终会拼进 shell 命令）：
// 插件名、相对路径、摘要格式、权限、冒烟参数任何一项不合规，整个插件直接拒收，
// 不做"能跑多少算多少"的降级。
func parseAcliPluginManifest(body []byte, caseID string) (*acliPluginManifestResponse, error) {
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	var payload acliPluginManifestResponse
	if err := decoder.Decode(&payload); err != nil {
		return nil, newAcliPluginError("manifest_invalid_json", err.Error())
	}
	// 票据只绑定一个工单：若上游把 case_id 回填错了（或拿错了缓存），就是拿别人的
	// 期望态改本机。宁可拒收，不能“看起来拉到清单”就装。
	if got := strings.TrimSpace(payload.CaseID); caseID != "" && got != "" && got != caseID {
		return nil, newAcliPluginError("manifest_case_mismatch",
			fmt.Sprintf("清单工单 %s 与本次同步 %s 不一致", got, caseID))
	}
	if len(payload.Plugins) > acliPluginMaxPlugins {
		return nil, newAcliPluginError("manifest_too_many_plugins",
			fmt.Sprintf("插件数 %d 超过上限 %d", len(payload.Plugins), acliPluginMaxPlugins))
	}
	seen := map[string]bool{}
	plugins := make([]acliPluginDesired, 0, len(payload.Plugins))
	for index := range payload.Plugins {
		item := payload.Plugins[index]
		if err := validateAcliPluginDesired(&item); err != nil {
			return nil, err
		}
		if seen[item.Name] {
			return nil, newAcliPluginError("manifest_duplicate_plugin",
				fmt.Sprintf("插件 %s 在清单里出现多次", item.Name))
		}
		seen[item.Name] = true
		plugins = append(plugins, item)
	}
	payload.Plugins = plugins
	return &payload, nil
}

// validateAcliPluginDesired 校验单个期望态条目；错误信息带上插件名便于定位。
func validateAcliPluginDesired(item *acliPluginDesired) error {
	if !acliPluginNameRe.MatchString(item.Name) {
		return newAcliPluginError("manifest_invalid_plugin_name",
			fmt.Sprintf("插件名不合规：%q", item.Name))
	}
	if !acliPluginHexRe.MatchString(item.SpecHash) {
		return newAcliPluginError("manifest_invalid_spec_hash",
			fmt.Sprintf("插件 %s 的 spec_hash 不是 64 位小写十六进制", item.Name))
	}
	// 包通道现状：平台已给出 package_ref/sha256，但本增量内**逐文件内联**是唯一
	// 可用收敛通道（HCI 侧无可靠的拆包工具链）。因此：
	//   - 声明了包却拿不到摘要 → 拒收（装了也无法证明完整性）；
	//   - 只有包、没有内联文件 → 明确报 package_channel_unsupported，
	//     绝不退化成“没文件可装 = 成功”（B6 同族错误）。
	if item.PackageRef != "" && !acliPluginHexRe.MatchString(item.SHA256) {
		return newAcliPluginError("manifest_package_without_digest",
			fmt.Sprintf("插件 %s 声明了包但没有 sha256", item.Name))
	}
	if !acliPluginSmokeRe.MatchString(item.SmokeArgs) {
		return newAcliPluginError("manifest_invalid_smoke_args",
			fmt.Sprintf("插件 %s 的冒烟参数含 shell 元字符", item.Name))
	}
	if len(item.Files) == 0 {
		if item.PackageRef != "" {
			return newAcliPluginError("manifest_package_channel_unsupported",
				fmt.Sprintf("插件 %s 只给了包 %s，未内联文件清单（桥当前不拆包）", item.Name, item.PackageRef))
		}
		return newAcliPluginError("manifest_empty_files",
			fmt.Sprintf("插件 %s 的文件清单为空", item.Name))
	}
	if len(item.Files) > acliPluginMaxFiles {
		return newAcliPluginError("manifest_too_many_files",
			fmt.Sprintf("插件 %s 的文件数 %d 超过上限 %d", item.Name, len(item.Files), acliPluginMaxFiles))
	}
	prefix := item.Name + "/"
	total := int64(0)
	for index := range item.Files {
		file := &item.Files[index]
		if !strings.HasPrefix(file.Path, prefix) || strings.HasPrefix(file.Path, "/") ||
			strings.Contains(file.Path, "..") || !acliPluginRelPathRe.MatchString(file.Path) {
			return newAcliPluginError("manifest_invalid_file_path",
				fmt.Sprintf("插件 %s 的文件路径不合规：%q", item.Name, file.Path))
		}
		if !acliPluginModeRe.MatchString(file.Mode) {
			return newAcliPluginError("manifest_invalid_mode",
				fmt.Sprintf("插件 %s 的文件 %s 权限不合规：%q", item.Name, file.Path, file.Mode))
		}
		if !acliPluginHexRe.MatchString(file.SHA256) {
			return newAcliPluginError("manifest_invalid_file_digest",
				fmt.Sprintf("插件 %s 的文件 %s 摘要不合规", item.Name, file.Path))
		}
		rel := strings.TrimPrefix(file.Path, prefix)
		if rel == "plugin" && !item.BuiltIn && file.Mode != "0755" {
			return newAcliPluginError("manifest_invalid_wrapper_mode",
				fmt.Sprintf("插件 %s 的入口脚本必须是 0755", item.Name))
		}
		if len(file.Content) > acliPluginMaxFileBytes {
			return newAcliPluginError("manifest_file_too_large",
				fmt.Sprintf("插件 %s 的文件 %s 超过单文件上限", item.Name, file.Path))
		}
		total += int64(len(file.Content))
		if sha256Hex([]byte(file.Content)) != file.SHA256 {
			return newAcliPluginError("manifest_file_digest_mismatch",
				fmt.Sprintf("插件 %s 的文件 %s 内容与摘要不符", item.Name, file.Path))
		}
	}
	if item.RegisterItem == nil || len(item.RegisterItem) == 0 {
		return newAcliPluginError("manifest_missing_register_item",
			fmt.Sprintf("插件 %s 缺少注册项", item.Name))
	}
	if name, _ := item.RegisterItem["name"].(string); name != item.Name {
		return newAcliPluginError("manifest_register_name_mismatch",
			fmt.Sprintf("插件 %s 的注册项 name 不一致", item.Name))
	}
	if item.Size > 0 && total != item.Size {
		return newAcliPluginError("manifest_size_mismatch",
			fmt.Sprintf("插件 %s 的声明体积 %d 与实际 %d 不符", item.Name, item.Size, total))
	}
	return nil
}

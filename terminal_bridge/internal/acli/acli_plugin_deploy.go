package acli

// acli 插件设备侧执行层：探测节点事实 → 差量三态判定 → 渲染并执行部署脚本 → 解析自检回执。
//
// 这里是旧 case-service build_deploy_script/parse_deploy_result 的 Go 移植，
// 语义必须逐条对齐（收口设计 §3.3 / 权威规格 S4c）：
//
//   - 注册锁 flock -w 30 与 acli plugin install/uninstall/sync 共用同一把锁；
//   - 先备份、后写入；写盘用 printf '%b'（HCI 无 base64/python3，24 环境实测）；
//   - 逐文件 sha256sum -c 校验，失败即回滚，绝不留半份；
//   - 注册表用 jq 写临时文件再 mv 原子替换；
//   - 结论只认设备侧 ACP_RESULT 自检，不接受任何"命令退出码 0 就算成功"；
//   - **无备份不删**（B1 构造性不变量）：目录删除以「有备份 ∨ 有 .old.$DEPLOY_ID」为前提。

import (
	"bytes"
	"encoding/json"
	"fmt"
	"path"
	"sort"
	"strings"
	"terminal_bridge/internal/logcollect"
	"terminal_bridge/internal/shared/remote"
	"time"
)

// 三态动作（与回执 Evidence 计数一一对应）。
const (
	acliPluginActionInstall     = "install"   // 新增：目录与注册项都不在
	acliPluginActionOverwrite   = "overwrite" // 有修改：覆盖收敛
	acliPluginActionSkip        = "skip"      // 无修改
	acliPluginActionFailed      = "failed"    // 执行失败（含设备侧自检不通过）
	acliPluginActionUnreachable = "unreachable"
)

// ACP 标记行（解析契约，改动需同步单测）。
const (
	acpStateBeginMark    = "ACP_STATE_BEGIN"
	acpStateEndMark      = "ACP_STATE_END"
	acpResultBeginMark   = "ACP_RESULT_BEGIN"
	acpResultEndMark     = "ACP_RESULT_END"
	acpPluginListBegin   = "ACP_PLUGIN_LIST_BEGIN"
	acpPluginListEnd     = "ACP_PLUGIN_LIST_END"
	acpSmokeBegin        = "ACP_SMOKE_BEGIN"
	acpSmokeEnd          = "ACP_SMOKE_END"
	acpRollbackSkipMark  = "ACP_ROLLBACK_SKIPPED="
	acliPluginRegMissing = "MISSING"
	acliPluginRegNoJQ    = "NO_JQ"
	acliPluginRegNoFile  = "NO_REG"
	acliPluginRegBad     = "BAD"
)

// acliPluginFileState 是节点上某个期望文件的实测状态。
type acliPluginFileState struct {
	Path   string `json:"path"`
	State  string `json:"state"` // ok | missing | read_failed | no_sha256sum
	Digest string `json:"digest,omitempty"`
}

// acliPluginHostState 是一台节点对某个插件的实测事实。
type acliPluginHostState struct {
	IP               string                `json:"ip"`
	Register         map[string]any        `json:"register,omitempty"`
	RegisterRaw      string                `json:"register_raw,omitempty"`
	Files            []acliPluginFileState `json:"files,omitempty"`
	DigestComparable bool                  `json:"digest_comparable"`
	ProbeErr         string                `json:"probe_error,omitempty"`
}

// acliPluginDeployResult 是设备侧自检结论。
type acliPluginDeployResult struct {
	Status      string `json:"status"` // success | rolled_back | failed | 空=没解析到
	Error       string `json:"error,omitempty"`
	SpecHash    string `json:"spec_hash,omitempty"`
	Version     string `json:"version,omitempty"`
	PluginList  string `json:"plugin_list,omitempty"`
	SmokeOutput string `json:"smoke_output,omitempty"`
	ExitCode    int    `json:"exit_code"`
	OK          bool   `json:"ok"`
}

// printfPercentBArg 把任意文本编码成 “printf '%b'“ 的单个安全 argv。
//
// 与 case-service 的 _printf_arg 逐字等价：先转义反斜杠与常见控制字符（因为
// %b 会解释反斜杠转义），单引号与其余 shell 元字符交给 ShellQuote。
func printfPercentBArg(text string) string {
	escaped := strings.ReplaceAll(text, `\`, `\\`)
	escaped = strings.ReplaceAll(escaped, "\n", `\n`)
	escaped = strings.ReplaceAll(escaped, "\r", `\r`)
	escaped = strings.ReplaceAll(escaped, "\t", `\t`)
	return remote.ShellQuote(escaped)
}

// canonicalJSONBytes 生成与 Python “_canonical“ 同构的紧凑 JSON（键升序、不转义 HTML）。
//
// 只用于注册项落盘文本：jq 之后会重排，但同一份期望态必须每次产出同一串字节，
// 否则 spec_hash 无法核对。
func canonicalJSONBytes(payload any) ([]byte, error) {
	var buf bytes.Buffer
	encoder := json.NewEncoder(&buf)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(payload); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

// buildAcliPluginProbeCommand 生成"一次会话读全事实"的探测脚本。
//
// 探测必须同时看两处：注册表条目和磁盘文件。只看注册表会重演 B4——acli 重装
// 清掉插件目录但注册项可能仍在，于是永远判出"无需修改"。
// 工具缺失（jq / sha256sum）如实标记，绝不回"读不到=一致"。
func buildAcliPluginProbeCommand(desired acliPluginDesired) string {
	name := desired.Name
	relPaths := make([]string, 0, len(desired.Files))
	for _, file := range desired.Files {
		rel := strings.TrimPrefix(file.Path, name+"/")
		// 覆盖内置插件时 manifest.json 由设备侧重算，不参与比较。
		if rel == "manifest.json" && desired.BuiltIn {
			continue
		}
		relPaths = append(relPaths, rel)
	}
	var builder strings.Builder
	builder.WriteString("NAME=" + remote.ShellQuote(name) + "; ")
	builder.WriteString("BASE=" + remote.ShellQuote(acliPluginBaseDir) + "; ")
	builder.WriteString("REG=" + remote.ShellQuote(acliPluginRegisterPath) + "; ")
	builder.WriteString("printf '" + acpStateBeginMark + `\n'` + "; ")
	// 注册项：先判文件存在、再判 jq 可用，最后才取条目。
	// MISSING（合法数组里没这条）与 BAD（jq 非零退出）必须区分：前者是新增，
	// 后者是读不到——把后者当前者会凭空重装一轮并报告“新增”。
	builder.WriteString(`printf 'REG` + "\t" + `'; `)
	builder.WriteString(`if [ ! -f "$REG" ]; then printf '` + acliPluginRegNoFile + `\n'; `)
	builder.WriteString(`elif ! command -v jq >/dev/null 2>&1; then printf '` + acliPluginRegNoJQ + `\n'; `)
	builder.WriteString(`else REGJSON=$(jq -c --arg n "$NAME" '(. // []) | map(select(.name==$n)) | .[0] // empty' "$REG" 2>/dev/null) || REGJSON=` + acliPluginRegBad + `; `)
	// 行首的 REG\t 前缀已在上面统一打印，这里只补值；再打一遍前缀会让解析端
	// 把第二个 "REG" 当注册项 JSON，恒判 register_json_unparsable → 永远覆盖、永不跳过。
	builder.WriteString(`[ -z "$REGJSON" ] && REGJSON=` + acliPluginRegMissing + `; printf '%s\n' "$REGJSON"; fi; `)
	// 逐文件：missing / no_sha256sum / read_failed / ok+摘要。
	for _, rel := range relPaths {
		builder.WriteString(`printf 'F` + "\t" + rel + `	'; `)
		builder.WriteString("p=\"$BASE/$NAME/" + rel + "\"; ")
		builder.WriteString(`if [ ! -f "$p" ]; then printf 'missing\n'; ` +
			`elif ! command -v sha256sum >/dev/null 2>&1; then printf 'no_sha256sum\n'; ` +
			`else d=$(sha256sum "$p" 2>/dev/null | awk '{print $1}'); ` +
			`if [ -n "$d" ]; then printf 'ok` + "\t" + `%s\n' "$d"; else printf 'read_failed\n'; fi; fi; `)
	}
	builder.WriteString(`printf '` + acpStateEndMark + `\n'`)
	return builder.String()
}

// parseAcliPluginProbeOutput 解析探测输出。解析不出任何标记视为探测失败（不可比）。
func parseAcliPluginProbeOutput(ip, stdout string, exitCode int, err error) acliPluginHostState {
	state := acliPluginHostState{IP: ip, DigestComparable: true}
	if err != nil || exitCode != 0 {
		state.ProbeErr = fmt.Sprintf("exit=%d err=%s", exitCode, logcollect.ErrString(err))
		state.DigestComparable = false
		return state
	}
	body := extractAcliPluginBlock(stdout, acpStateBeginMark, acpStateEndMark)
	if body == "" {
		state.ProbeErr = "state_block_missing"
		state.DigestComparable = false
		return state
	}
	for _, line := range strings.Split(strings.ReplaceAll(body, "\r", ""), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		fields := strings.Split(line, "\t")
		switch fields[0] {
		case "REG":
			raw := ""
			if len(fields) > 1 {
				raw = strings.TrimSpace(fields[1])
			}
			state.RegisterRaw = raw
			switch raw {
			case acliPluginRegNoFile, acliPluginRegMissing:
				// 注册表不存在 / 条目不存在：都是「新增」的合法事实，不记错。
			case acliPluginRegNoJQ:
				// 没有 jq 就无法核对注册项，等价于“判定不了”。
				state.DigestComparable = false
			case acliPluginRegBad:
				// jq 非零退出：注册表可能已损坏，必须留痕而不是当成“没装过”。
				state.ProbeErr = "register_read_failed"
			default:
				var parsed map[string]any
				decoder := json.NewDecoder(strings.NewReader(raw))
				decoder.UseNumber()
				if decodeErr := decoder.Decode(&parsed); decodeErr != nil {
					state.ProbeErr = "register_json_unparsable"
				}
				state.Register = parsed
			}
		case "F":
			fileState := acliPluginFileState{Path: "", State: "read_failed"}
			if len(fields) > 1 {
				fileState.Path = fields[1]
			}
			if len(fields) > 2 {
				fileState.State = fields[2]
			}
			if len(fields) > 3 {
				fileState.Digest = fields[3]
			}
			if fileState.State == "no_sha256sum" {
				state.DigestComparable = false
			}
			state.Files = append(state.Files, fileState)
		}
	}
	return state
}

// compareAcliPluginState 给出单台节点对该插件的三态判定。
//
// 判定顺序即优先级：注册项缺失=新增；任何一处不一致=有修改；
// 只有在"注册项一致 + 每个文件摘要都对得上"时才允许 Skip。
// 无法比较（探测失败/缺 jq/缺 sha256sum）一律按有修改处理并留痕，
// 绝不把"读不到"洗成"已是最新"（B6 同族）。
func compareAcliPluginState(desired acliPluginDesired, actual acliPluginHostState) (action string, reason string) {
	if actual.ProbeErr != "" {
		return acliPluginActionOverwrite, "probe_failed:" + actual.ProbeErr
	}
	if !actual.DigestComparable {
		return acliPluginActionOverwrite, "probe_degraded"
	}
	if len(actual.Register) == 0 {
		return acliPluginActionInstall, "register_entry_missing"
	}
	if changed := registerItemDiff(desired.RegisterItem, actual.Register); changed != "" {
		return acliPluginActionOverwrite, "register_changed:" + changed
	}
	name := desired.Name
	for _, file := range desired.Files {
		rel := strings.TrimPrefix(file.Path, name+"/")
		if rel == "manifest.json" && desired.BuiltIn {
			continue // 设备侧重算，不参与比较
		}
		observed, found := "", false
		for _, item := range actual.Files {
			if item.Path == rel {
				observed, found = item.State+"|"+item.Digest, true
				break
			}
		}
		if !found {
			return acliPluginActionOverwrite, "file_unprobed:" + rel
		}
		if !strings.HasPrefix(observed, "ok|") {
			return acliPluginActionOverwrite, "file_" + strings.SplitN(observed, "|", 2)[0] + ":" + rel
		}
		if !strings.HasSuffix(observed, file.SHA256) {
			return acliPluginActionOverwrite, "file_digest_mismatch:" + rel
		}
	}
	return acliPluginActionSkip, "in_sync"
}

// registerItemDiff 比较注册项的语义字段（忽略 last_updated_at：它是构建时刻，不是内容）。
func registerItemDiff(desired, actual map[string]any) string {
	for _, key := range []string{"id", "name", "version", "status", "source", "built_in"} {
		want, ok := desired[key]
		if !ok {
			continue
		}
		got, has := actual[key]
		if !has {
			return "missing:" + key
		}
		if !sameJSONScalar(want, got) {
			return "value:" + key
		}
	}
	return ""
}

// sameJSONScalar 比较标量：数字统一按字符串比（一边 int、一边 json.Number 不算差异）。
func sameJSONScalar(a, b any) bool {
	if aNumber, ok := a.(json.Number); ok {
		a = aNumber.String()
	}
	if bNumber, ok := b.(json.Number); ok {
		b = bNumber.String()
	}
	if fmt.Sprintf("%v", a) == fmt.Sprintf("%v", b) {
		return true
	}
	// bool：设备侧可能写 1/0，期望侧是 true/false。
	return fmt.Sprintf("%v", a) == jsonBool(b) || jsonBool(a) == fmt.Sprintf("%v", b)
}

func jsonBool(value any) string {
	switch typed := value.(type) {
	case json.Number:
		if typed.String() == "1" {
			return "true"
		}
		return "false"
	case float64:
		if typed == 1 {
			return "true"
		}
		return "false"
	}
	return ""
}

// renderAcliPluginDeployScript 渲染一次性原子部署脚本（备份→写盘→校验→注册→冒烟→自检）。
//
// 与 Python 原件的三处刻意差异：
//  1. 脚本自身由桥经 stdin 通道写到节点，不再按 48KB 分片塞进 argv（MAX_ARG_STRLEN）；
//  2. deploy_id 用「spec_hash 前 12 位 + 随机后缀」，既能在节点上追认是哪次同步，
//     又保证同一次同步的临时目录唯一；
//  3. 只接受已解析过的期望态，路径/权限/冒烟参数都已在清单校验里过一遍。
func renderAcliPluginDeployScript(desired acliPluginDesired, deployID string) (string, error) {
	registerJSON, err := canonicalJSONBytes(desired.RegisterItem)
	if err != nil {
		return "", newAcliPluginError("register_item_encode_failed", err.Error())
	}
	name := desired.Name
	tmp := acliPluginTmpDir + "/" + deployID
	quote := "\""
	lines := []string{
		"#!/bin/bash",
		"# HTP acli plugin deploy " + deployID + " — rendered by terminal_bridge (stage 2)",
		"set -u",
		"BASE=" + remote.ShellQuote(acliPluginBaseDir),
		"REG=" + remote.ShellQuote(acliPluginRegisterPath),
		"COMPL=" + remote.ShellQuote(acliPluginCompletion),
		"TMP=" + remote.ShellQuote(tmp),
		"NAME=" + remote.ShellQuote(name),
		"DEPLOY_ID=" + remote.ShellQuote(deployID),
		"VERSION=" + remote.ShellQuote(desired.Version),
		"SPEC_HASH=" + remote.ShellQuote(desired.SpecHash),
		"ENABLE_COMPLETION=" + remote.ShellQuote(boolFlag(desired.EnableCompletion)),
		"SMOKE_ARGS=" + remote.ShellQuote(desired.SmokeArgs),
		"BUILTIN=" + remote.ShellQuote(boolFlag(desired.BuiltIn)),
		"STATUS=failed",
		"ERROR=",
		"rollback() {",
		`  [ -f "$TMP/backup/plugin_register.json" ] && cp -a "$TMP/backup/plugin_register.json" "$REG"`,
		`  [ -f "$TMP/backup/bash_completion.conf" ] && cp -a "$TMP/backup/bash_completion.conf" "$COMPL"`,
		// 目录删除以「有备份 ∨ 有 .old」为前提：锁超时/备份失败都发生在目录备份之前，
		// 此时 "$BASE/$NAME" 是现场唯一副本，删了就再也回不来（B1 构造性不变量）。
		`  if [ -d "$TMP/backup/$NAME" ] || [ -d "$BASE/$NAME.old.$DEPLOY_ID" ]; then`,
		`    if [ -d "$BASE/$NAME" ]; then rm -rf "$BASE/$NAME"; fi`,
		`    if [ -d "$TMP/backup/$NAME" ]; then cp -a "$TMP/backup/$NAME" "$BASE/$NAME"; fi`,
		`    if [ -d "$BASE/$NAME.old.$DEPLOY_ID" ]; then mv "$BASE/$NAME.old.$DEPLOY_ID" "$BASE/$NAME"; fi`,
		"  else",
		`    echo "` + acpRollbackSkipMark + `no_backup" >&2`,
		"  fi",
		"  return 0",
		"}",
		"finish() {",
		`  echo "` + acpResultBeginMark + `"`,
		`  printf '{"status":"%s","plugin_name":"%s","version":"%s","spec_hash":"%s","deploy_id":"%s","error":"%s"}\n' "$STATUS" "$NAME" "$VERSION" "$SPEC_HASH" "$DEPLOY_ID" "$ERROR"`,
		`  echo "` + acpResultEndMark + `"`,
		`  if [ -f "$TMP/plugin_list.json" ]; then`,
		`    echo "` + acpPluginListBegin + `"`,
		`    cat "$TMP/plugin_list.json"`,
		`    echo "` + acpPluginListEnd + `"`,
		"  fi",
		`  if [ -f "$TMP/smoke.out" ]; then`,
		`    echo "` + acpSmokeBegin + `"`,
		`    cat "$TMP/smoke.out"`,
		`    echo "` + acpSmokeEnd + `"`,
		"  fi",
		"  exit 0",
		"}",
		"fail() {",
		"  trap - INT TERM HUP",
		`  ERROR="$1"`,
		`  echo "ACP_ERROR=$1" >&2`,
		"  rollback",
		`  STATUS="rolled_back"`,
		"  finish",
		"}",
		// 脚本被 SIGTERM/SIGKILL 打断时停在"旧目录已挪走、新目录未就位"之间；
		// INT/TERM/HUP 走 fail 收敛到回滚，SIGKILL 由下次同步前的自愈逻辑兜底。
		"trap 'fail interrupted' INT TERM HUP",
	}
	if desired.BuiltIn {
		lines = append(lines, `mkdir -p "$TMP/backup" "$TMP/stage/$NAME" || { echo "ACP_ERROR=mkdir_failed" >&2; exit 1; }`)
	} else {
		lines = append(lines, `mkdir -p "$TMP/backup" "$TMP/stage/$NAME/bin" || { echo "ACP_ERROR=mkdir_failed" >&2; exit 1; }`)
	}
	lines = append(lines,
		"exec 9>"+remote.ShellQuote(acliPluginLockPath)+" || fail \"lock_open_failed\"",
		"flock -w 30 9 || fail \"lock_timeout\"",
		`cp -a "$REG" "$TMP/backup/plugin_register.json" || fail 'register_backup_failed'`,
		`[ -f "$COMPL" ] && cp -a "$COMPL" "$TMP/backup/bash_completion.conf"`,
		`if [ "$BUILTIN" = "1" ]; then`,
		`  [ -d "$BASE/$NAME" ] || fail 'builtin_plugin_missing'`,
		`  cp -a "$BASE/$NAME" "$TMP/backup/" || fail 'builtin_backup_failed'`,
		"else",
		`  if [ -d "$BASE/$NAME" ]; then cp -a "$BASE/$NAME" "$TMP/backup/" || fail 'plugin_backup_failed'; fi`,
		"fi",
	)
	if desired.BuiltIn {
		// 覆盖内置插件：先复制完整文件树（含二进制 .mo），只覆盖规格声明的文件，
		// 随后由设备侧重算 manifest.hash，保证厂商文件与规格文件共存。
		lines = append(lines, `if [ "$BUILTIN" = "1" ]; then`,
			`  cp -a "$BASE/$NAME/." "$TMP/stage/$NAME/" || fail 'builtin_stage_copy_failed'`,
			"fi")
	}
	for _, file := range desired.Files {
		rel := strings.TrimPrefix(file.Path, name+"/")
		if desired.BuiltIn && rel == "plugin" {
			continue // 内置入口脚本保留厂商原件，只覆盖其附属文件
		}
		// rel 已过清单校验（^[A-Za-z0-9._/-]{1,255}$ 且不含 ..），因此可以直接双引号内
		// 展开——与 Python 原件逐字一致；任何 shell 元字符都进不了这里。
		target := `$TMP/stage/$NAME/` + rel
		lines = append(lines,
			`mkdir -p "$(dirname `+quote+target+quote+`)" || fail 'stage_mkdir_failed'`,
			"printf '%b' "+printfPercentBArg(file.Content)+` > `+quote+target+quote+` || fail 'file_write_failed'`,
			`printf '%s  %s\n' `+remote.ShellQuote(file.SHA256)+` `+quote+target+quote+` | sha256sum -c - >/dev/null || fail 'hash_mismatch'`,
		)
	}
	if desired.BuiltIn {
		lines = append(lines,
			`cd "$TMP/stage/$NAME" || fail 'stage_cd_failed'`,
			`HASH_JSON=$(find . -type f ! -name manifest.json -print0 | sort -z | while IFS= read -r -d '' f; do`,
			`  sum=$(sha256sum "$f" | cut -d' ' -f1); rel=${f#./};`,
			`  printf '"%s":"%s",' "$rel" "$sum";`,
			"done)",
			`HASH_JSON="{${HASH_JSON%,}}"`,
			`jq --argjson h "$HASH_JSON" '.hash = $h' manifest.json > manifest.new || fail 'manifest_hash_rebuild_failed'`,
			`mv manifest.new manifest.json || fail 'manifest_replace_failed'`,
			"cd - >/dev/null || true",
		)
	}
	lines = append(lines,
		`chmod 0755 "$TMP/stage/$NAME/plugin" || fail 'chmod_failed'`,
		"printf '%b' "+printfPercentBArg(string(registerJSON)+"\n")+" > \"$TMP/register_item.json\" || fail 'register_item_write_failed'",
		`if [ -d "$BASE/$NAME" ]; then mv "$BASE/$NAME" "$BASE/$NAME.old.$DEPLOY_ID" || fail "old_dir_move_failed"; fi`,
		`mv "$TMP/stage/$NAME" "$BASE/$NAME" || fail "stage_move_failed"`,
		`jq --slurpfile item "$TMP/register_item.json" '(. // []) | del(.[] | select(.name == $item[0].name)) + [$item[0]]' "$REG" > "$TMP/plugin_register.json" || fail "jq_register_failed"`,
		`jq -e 'type == "array"' "$TMP/plugin_register.json" >/dev/null || fail "register_json_invalid"`,
		`mv "$TMP/plugin_register.json" "$REG" || fail "register_replace_failed"`,
		`if [ "$ENABLE_COMPLETION" = "1" ] && [ -f "$COMPL" ]; then grep -qx "$NAME" "$COMPL" || printf "\n%s\n" "$NAME" >> "$COMPL"; fi`,
		acliPluginBin+` --formatter json plugin list > "$TMP/plugin_list.json" 2>&1 || fail 'plugin_list_failed'`,
		`grep -q "\"Name\": \"$NAME\"" "$TMP/plugin_list.json" || fail "plugin_not_registered"`,
		acliPluginBin+` --timeout 60 "$NAME" --help > "$TMP/help.out" 2>&1 || fail 'plugin_help_failed'`,
		acliPluginBin+` --timeout 60 "$NAME" $SMOKE_ARGS > "$TMP/smoke.out" 2>&1 || fail "plugin_smoke_failed"`,
		`[ -s "$TMP/smoke.out" ] || fail "plugin_smoke_empty"`,
		`rm -rf "$BASE/$NAME.old.$DEPLOY_ID"`,
		`STATUS="success"`,
		"finish",
		"",
	)
	return strings.Join(lines, "\n"), nil
}

// boolFlag 把布尔量编码成设备侧脚本里的 "1"/"0"（与原件一致）。
func boolFlag(value bool) string {
	if value {
		return "1"
	}
	return "0"
}

// extractAcliPluginBlock 取 begin/end 之间的正文；缺一侧标记即返回空串。
func extractAcliPluginBlock(stdout, begin, end string) string {
	if !strings.Contains(stdout, begin) || !strings.Contains(stdout, end) {
		return ""
	}
	head := strings.SplitN(stdout, begin, 2)[1]
	return strings.TrimSpace(strings.SplitN(head, end, 2)[0])
}

// parseAcliPluginDeployResult 解析设备侧自检：只有 success + 退出码 0 + 冒烟非空才算生效。
//
// 这三条缺一不可：脚本任何分支都以 0 结束（finish 里 exit 0），所以退出码本身
// 不构成结论；冒烟输出为空说明插件"跑起来了但什么都没打印"，同样不算成功。
func parseAcliPluginDeployResult(stdout string, exitCode int) acliPluginDeployResult {
	result := acliPluginDeployResult{ExitCode: exitCode}
	raw := extractAcliPluginBlock(stdout, acpResultBeginMark, acpResultEndMark)
	if raw != "" {
		var parsed map[string]any
		decoder := json.NewDecoder(strings.NewReader(raw))
		decoder.UseNumber()
		if err := decoder.Decode(&parsed); err == nil {
			result.Status = stringField(parsed, "status")
			result.Error = stringField(parsed, "error")
			result.SpecHash = stringField(parsed, "spec_hash")
			result.Version = stringField(parsed, "version")
		}
	}
	result.SmokeOutput = extractAcliPluginBlock(stdout, acpSmokeBegin, acpSmokeEnd)
	result.PluginList = extractAcliPluginBlock(stdout, acpPluginListBegin, acpPluginListEnd)
	result.OK = result.Status == "success" && exitCode == 0 && result.SmokeOutput != ""
	if !result.OK && result.Status == "" {
		result.Error = "device_result_missing"
	}
	return result
}

func stringField(payload map[string]any, key string) string {
	if value, ok := payload[key]; ok {
		return fmt.Sprintf("%v", value)
	}
	return ""
}

// acliPluginDeployID 生成一次部署的任务键：spec_hash 前 12 位可追认是哪份规格，
// 随机后缀保证同一份规格并发重跑也不会共用同一个临时目录。
func acliPluginDeployID(desired acliPluginDesired) string {
	hash := desired.SpecHash
	if len(hash) > 12 {
		hash = hash[:12]
	}
	return fmt.Sprintf("htp-plugin-%s-%s-%d", desired.Name, hash, time.Now().UnixNano()%1e9)
}

// acliPluginScriptPath 是部署脚本在节点上的落地路径（平台自有固定目录）。
func acliPluginScriptPath(deployID string) string {
	return path.Join(acliPluginTmpDir, deployID+".sh")
}

// sortedPluginNames 让清单顺序稳定，日志与回执可复现。
func sortedPluginNames(items []acliPluginDesired) []string {
	names := make([]string, 0, len(items))
	for _, item := range items {
		names = append(names, item.Name)
	}
	sort.Strings(names)
	return names
}

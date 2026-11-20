package logcollect

// 收集脚本覆写：把平台「收集脚本」库的最新版本写到每台目标节点的 script/ 目录，
// 任务终态再还原成插件原件。
//
// 第一性原理与 spc 完全相同：脚本由每台节点的 acli 插件在采集时现场读取
// （插件按工作目录下的脚本子目录拼路径），
// 只写发起节点会让其余节点继续跑旧脚本，而且不会有任何报错。
//
// 与 spc 的三处差异（这是独立文件而不是复用 slot 抽象的原因）：
//  1. 目录不同：script/ 与 spc/ 并列，绝不互相覆盖；
//  2. 文件名不同：spc 是 <slot>.json，脚本是平台给的带扩展名文件名（code）；
//  3. 体积不同：脚本最大 4MB，超过 argv 单参数 128KB 上限，必须分块追加写入。

import (
	"fmt"
	"path"
	"regexp"
	"strings"
	"unicode/utf8"

	"terminal_bridge/internal/shared/models"
	"terminal_bridge/internal/shared/remote"
)

const (
	// logCollectScriptMaxBytes 与服务端上限同口径。
	logCollectScriptMaxBytes = 4 << 20
	// logCollectScriptChunkBytes 是单次 argv 追加的**原文**字节上限。
	// MAX_ARG_STRLEN 是 128KB；单引号转义最坏膨胀 4 倍（' → '\''），
	// 16KB × 4 = 64KB，加上命令外壳仍安全。不要凭"一般脚本没有单引号"调大。
	logCollectScriptChunkBytes = 16 << 10
)

// scriptDeployDir 与 SpcDeployDir 位于同一插件目录树，由后者派生。
// 为什么不重复写字面量：路径只应在 spc 常量处出现一次，否则两处漂移会导致
// 「清单写进去了、脚本写到另一个目录」这种极难定位的故障。
var scriptDeployDir = path.Dir(SpcDeployDir) + "/script"

// scriptCodeRe 与服务端 CODE_RE 同源：相对 script/ 的路径，最多三层。
//
// 为什么允许子目录：插件自带脚本不全在顶层（内置采集脚本在 vs_script/ 等子目录），
// 只认单层文件名等于"这些脚本永远无法被平台覆写"。
// 每段必须以字母/数字开头，天然排除绝对路径、空段（a//b）与 ".."；
// 尾随斜杠与路径越界另由 scriptFilePath 的 path.Clean 比对兜住。
var scriptCodeRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*){0,2}$`)

// scriptFile 是归一化后的一份待写脚本。
type scriptFile struct {
	Code        string
	Content     string
	SHA256      string
	Interpreter string
	Revision    int
}

// scriptFilePath 拼出目标文件路径；目录与相对路径都必须先过白名单。
//
// path.Clean 比对是第二道闸：即使正则将来被放宽，a//b、a/./b、a/b/ 这类
// 规范化后会改变路径语义的写法也会在这里被拒，而不是靠调用方自觉。
func scriptFilePath(dir, code string) (string, error) {
	if !strings.HasPrefix(dir, "/") || strings.Contains(dir, "..") {
		return "", fmt.Errorf("脚本目录非法：%s", dir)
	}
	if !scriptCodeRe.MatchString(code) {
		return "", fmt.Errorf("脚本相对路径非法：%s", code)
	}
	joined := dir + "/" + code
	if path.Clean(joined) != joined || !strings.HasPrefix(joined, dir+"/") {
		return "", fmt.Errorf("脚本相对路径越界：%s", code)
	}
	return joined, nil
}

// scriptBackupPath 备份必须按任务隔离：固定文件名会让两次任务互相覆盖。
func scriptBackupPath(path, taskID string) (string, error) {
	if !spcTaskIDRe.MatchString(strings.TrimSpace(taskID)) {
		return "", fmt.Errorf("任务 ID 非法，无法确定备份路径")
	}
	return path + ".platform." + taskID + ".bak", nil
}

// buildScriptDirProbe 输出固定目录的存在性检查：缺失即创建。
// acli 插件重装会清空整个插件目录（含 script），写前必须能把它重建出来。
func buildScriptDirProbe() string {
	q := ShellQuote(scriptDeployDir)
	return fmt.Sprintf("[ -d %s ] || mkdir -p %s; [ -d %s ] && printf '%%s\\n' %s", q, q, q, q)
}

// scriptResolveDir 只认固定路径：命中即成功，否则一律视为插件目录缺失。
func scriptResolveDir(output string) (string, string) {
	for _, line := range strings.Split(output, "\n") {
		dir := strings.TrimSpace(strings.ReplaceAll(line, "\r", ""))
		if dir == scriptDeployDir {
			return dir, ""
		}
	}
	return "", "script_dir_missing"
}

// buildScriptPrepareCommand 完成「目录重建 + 写前自愈 + 备份 + 备份保真校验」。
//
// 与 spc 的 prepare 逐字同构，只有回显标记换成 SCRIPT_ 前缀：
//   - 先恢复历史备份（原件）再备份，否则本次备份存的是上一次的平台内容；
//   - 本次任务的备份已存在时不再覆盖，重复下发不会把原件换成平台内容；
//   - 备份后 cmp 校验，保证还原源就是写入前的原件。
//
// 脚本与 spc 的关键差异：节点上可能原本没有这个文件（平台新增脚本）。
// 此时不能备份，也不能在还原时"什么都不做"——必须凭 created 标记把新建文件删掉，
// 否则节点会长期残留平台脚本。标记内容是目标路径，写前自愈可据此清理上次残留。
func buildScriptPrepareCommand(target, backup, marker string) string {
	dir := target
	if index := strings.LastIndex(target, "/"); index > 0 {
		dir = target[:index]
	}
	ensureDir := fmt.Sprintf("mkdir -p %s", ShellQuote(dir))
	cleanTmp := fmt.Sprintf("rm -f %s", ShellQuote(target+".tmp"))
	glob := fmt.Sprintf("%s.platform.*.bak", ShellQuote(target))
	prev := fmt.Sprintf("__prev=$(ls -1t %s 2>/dev/null | grep -v -F %s | head -n 1)", glob, ShellQuote(backup))
	selfHeal := fmt.Sprintf("if [ -n \"$__prev\" ]; then cp -p \"$__prev\" %s 2>/dev/null; fi", ShellQuote(target))
	// 上次任务新建但没还原的文件：标记里存着目标路径，凭它删除（不误删用户手工文件）。
	healCreated := fmt.Sprintf(
		"for __f in %s.platform.*.created; do [ -f \"$__f\" ] || continue; __p=$(cat \"$__f\" 2>/dev/null); [ -n \"$__p\" ] && rm -f \"$__p\"; rm -f \"$__f\"; done",
		ShellQuote(target))
	backupCmd := fmt.Sprintf("if [ -f %s ]; then "+
		"if [ ! -f %s ]; then cp -p %s %s 2>/dev/null || { printf 'SCRIPT_PREPARE_FAIL reason=backup_failed\\n'; exit 0; }; "+
		"cmp -s %s %s || { printf 'SCRIPT_PREPARE_FAIL reason=backup_mismatch\\n'; exit 0; }; fi; "+
		"__orig=$(sha256sum %s 2>/dev/null | awk '{print $1}'); printf 'SCRIPT_PREPARE_OK orig=%%s\\n' \"$__orig\"; "+
		"else printf '%%s' %s > %s; printf 'SCRIPT_PREPARE_OK orig= created=1\\n'; fi",
		ShellQuote(target), ShellQuote(backup), ShellQuote(target), ShellQuote(backup),
		ShellQuote(target), ShellQuote(backup), ShellQuote(backup), ShellQuote(target), ShellQuote(marker))
	return fmt.Sprintf("%s; %s; %s; %s; %s; %s", ensureDir, cleanTmp, prev, selfHeal, healCreated, backupCmd)
}

// buildScriptRestoreCommand 还原：有备份就放回去；没有备份但有 created 标记就删掉；
// 两者都没有说明本次没写过，返回 skipped（幂等重放不误报）。
func buildScriptRestoreCommand(target, backup, marker string) string {
	return fmt.Sprintf(
		"if [ -f %s ]; then %s && cmp -s %s %s && printf 'SCRIPT_RESTORE_OK\\n' || printf 'SCRIPT_RESTORE_FAIL reason=verify_mismatch\\n'; "+
			"elif [ -f %s ]; then rm -f %s %s && printf 'SCRIPT_RESTORE_OK reason=removed_created\\n'; "+
			"else printf 'SCRIPT_RESTORE_SKIP reason=no_backup\\n'; fi",
		ShellQuote(backup), buildSpcRestoreCommand(backup, target), ShellQuote(backup), ShellQuote(target),
		ShellQuote(marker), ShellQuote(target), ShellQuote(marker))
}

// buildScriptWriteCommands 生成写入命令序列（分块追加 + 校验 + 原子替换）。
//
// 每条命令都包在子 shell 里并带同一套回滚：任一块失败立刻用备份还原，
// 绝不让"写到一半的脚本"留在节点上（acli 会把它当正常脚本执行）。
func buildScriptWriteCommands(target, backup, marker, content string) []string {
	spec := remote.WriteSpec{
		Path:    target,
		TmpPath: target + ".tmp",
		Data:    []byte(content),
		Size:    int64(len(content)),
		SHA256:  spcSHA256(content),
		// 脚本必须可执行：spc 的 0644 是清单文件语义，不能照抄。
		Mode: "0755",
	}
	raw := remote.BuildChunkedArgvWriteCommands(spec, logCollectScriptChunkBytes)
	// 回滚走脚本自己的还原语义：节点原本没有该文件（created 场景）时，
	// 备份不存在，必须删掉可能已经 mv 出来的新文件，而不是"什么都不做"。
	rollback := fmt.Sprintf("%s 2>/dev/null", buildScriptRestoreCommand(target, backup, marker))
	commands := make([]string, 0, len(raw))
	for index, command := range raw {
		step := "write"
		if index == len(raw)-1 {
			step = "verify"
		}
		commands = append(commands, fmt.Sprintf(
			"( %s ) || { %s; printf 'SCRIPT_WRITE_FAIL reason=%s_failed\\n'; exit 0; }", command, rollback, step))
	}
	return commands
}

// validateScriptContent 二级校验平台下发的脚本。
// 平台已校验过，但原文经前端透传（不可信链路），桥必须独立证明。
func validateScriptContent(content string) error {
	if strings.TrimSpace(content) == "" {
		return fmt.Errorf("脚本内容为空")
	}
	if len(content) > logCollectScriptMaxBytes {
		return fmt.Errorf("脚本过大：%d 字节（上限 %d）", len(content), logCollectScriptMaxBytes)
	}
	if !utf8.ValidString(content) {
		return fmt.Errorf("脚本不是合法的 UTF-8 文本")
	}
	if strings.ContainsRune(content, 0) {
		return fmt.Errorf("脚本包含空字符")
	}
	return nil
}

// parseScriptFiles 校验平台签发的脚本集合：文件名、大小、UTF-8、摘要自洽。
// 任何一项不合规都拒绝下发，绝不带着半份脚本去改节点。
func parseScriptFiles(msg models.InMessage) ([]scriptFile, *spcRejection) {
	if len(msg.ScriptFiles) == 0 {
		return nil, nil
	}
	seen := map[string]bool{}
	files := make([]scriptFile, 0, len(msg.ScriptFiles))
	for _, raw := range msg.ScriptFiles {
		code := strings.TrimSpace(raw.Code)
		if !scriptCodeRe.MatchString(code) {
			return nil, &spcRejection{ErrType: "invalid_script", Message: fmt.Sprintf("非法的脚本相对路径：%s", code)}
		}
		if seen[code] {
			return nil, &spcRejection{ErrType: "invalid_script", Message: fmt.Sprintf("脚本重复：%s", code)}
		}
		seen[code] = true
		if err := validateScriptContent(raw.Content); err != nil {
			return nil, &spcRejection{ErrType: "invalid_script", Message: fmt.Sprintf("脚本 %s 非法：%v", code, err)}
		}
		digest := spcSHA256(raw.Content)
		if declared := strings.ToLower(strings.TrimSpace(raw.SHA256)); declared != "" && declared != digest {
			return nil, &spcRejection{
				ErrType: "script_digest_mismatch",
				Message: fmt.Sprintf("脚本 %s 的摘要与内容不一致（传输可能被截断或改写）", code),
			}
		}
		files = append(files, scriptFile{
			Code: code, Content: raw.Content, SHA256: digest,
			Interpreter: strings.TrimSpace(raw.Interpreter), Revision: raw.Revision,
		})
	}
	return files, nil
}

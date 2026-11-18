package main

// acli 安装包全链路完整性：下载 → 上传落盘 → 安装。
//
// 第一性原理：要回答"上传到 HCI 的包对不对"，必须有源端产生的锚点，而且锚点
// 必须落在「字节真正落地的那一跳」上。官方源是 HTTP 明文、无签名，能拿到的
// 权威锚点只有两个：
//  1. 响应头 Content-Length —— 服务端声明的字节数；
//  2. 读取 body 时自己算出的 SHA-256 / MD5 —— 本地客观事实。
// 因此校验只可能发生在两处：下载读完（已收字节 == Content-Length），以及
// 上传落盘（远端 size/digest == 本地 size/digest）。
//
// 对抗性审查（为什么不是"上传完再 stat 一下"）：
//  1. 远端写入是 `cat > tmp && mv -f tmp dest`。只在上传后 stat，半包早已被
//     mv 上线，stat 只能证明"盘上有个文件"，证明不了"它就是我们要的包"。
//     所以校验必须嵌在 mv **之前**，失败就删 tmp —— 让"坏包不可见"成为结构性事实，
//     而不是靠调用方记得清理。
//  2. http.Client.Timeout 覆盖的是 body 读完为止。历史上把 dlResp.Body 直接当
//     SSH stdin，等于让 90s 下载预算去承担上传时长，于是"链路慢"必然升级为
//     "上传失败"（2026-10-29 Q2026102901049 三次失败都精确卡在 92s）。
//     所以必须先读完、再以独立预算上传。
//  3. 远端 digest 工具可能缺失（安装场景下 acli 恰恰还没装）。此时降级为
//     size-only，但必须在结果里显式标注 integrity=size_only —— 静默降级等于
//     把"没校验"伪装成"校验通过"。

import (
	"bytes"
	"context"
	"crypto/md5"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"time"

	"terminal_bridge/internal/shared/remote"
)

const (
	// 三个阶段各自独立预算：合成一个总预算时，任何一段变慢都会表现成"另一段超时"，
	// 排障只能靠猜（Q2026102901049 现场正是这样）。
	acliDownloadTimeout = 120 * time.Second
	acliUploadTimeout   = 300 * time.Second
	acliInstallTimeout  = 240 * time.Second
	// 注意：上面三个常量之和必须小于 main.go 中 acli_sync 会话的总预算，
	// 否则外层先超时，内层的分段诊断结论永远拿不到。
	acliVerifyTimeout = 30 * time.Second

	// 安装包实测 18~31MB；上限只用于防呆（源站返回错误页/超大文件时不至于打爆内存）。
	acliMaxPackageBytes = 256 << 20

	// 上传进度上报粒度：过小会刷屏，过大则长链路没有中间态。
	// 唯一实现在 remote/fs.go，此处只是链路内引用名。
	acliUploadProgressStep = remote.DefaultProgressStep

	// 自动重试次数（含首次）。只有"链路类"错误才值得重跑：
	// 超时、连接被重置、远端命令异常退出、以及校验不符（可能是中转缓存污染，
	// 重下一份通常就好了）。安装失败不自动重试——那属于产品侧结论。
	acliMaxAttempts = 3

	// 已校验安装包的内存缓存时长：支撑前端"只重试上传"而不必重新下载。
	acliPackageCacheTTL = 10 * time.Minute

	// 远端写入脚本的输出标记（解析契约，改动需同步单测）。
	// 字节形态被 golden 测试逐字锁定，因此收口后**不改一个字符**：
	// 唯一实现在 remote/fs.go，这里只是 acli 链路内的引用名。
	acliWriteMark     = remote.WriteMark
	acliWriteOKMark   = remote.WriteOKMark
	acliWriteFailMark = remote.WriteFailMark
)

// acliSyncError 携带「哪一跳失败 / 是否能重试 / 给用户看的细节」。
// 前端据此决定是否渲染重试按钮，而不是把所有 error 一律当成不可恢复。
type acliSyncError struct {
	Code      string // LC-INSTALL-xxx
	Stage     string // download | upload | verify | install
	Retryable bool
	Detail    string
	Err       error
}

func (e *acliSyncError) Error() string {
	if e.Detail == "" {
		return e.Err.Error()
	}
	if e.Err == nil {
		return e.Detail
	}
	return e.Detail + ": " + e.Err.Error()
}

func (e *acliSyncError) Unwrap() error { return e.Err }

func newAcliSyncError(code, stage string, retryable bool, detail string, err error) *acliSyncError {
	return &acliSyncError{Code: code, Stage: stage, Retryable: retryable, Detail: detail, Err: err}
}

// acliSyncErrorCode 从错误链里取规范错误码；取不到时回退到调用方给的兜底码，
// 保证"每次失败都有码"，不会出现前端拿到裸 500。
func acliSyncErrorCode(err error, fallback string) (code string, detail string, retryable bool) {
	var syncErr *acliSyncError
	if errors.As(err, &syncErr) {
		code = firstNonEmpty(syncErr.Code, fallback)
		return code, syncErr.Error(), syncErr.Retryable
	}
	return fallback, errString(err), true
}

// acliPackage 是"已读完且已算出摘要"的安装包，是上传前唯一的事实来源。
//
// 为什么不把 body 流式传给 SSH：见文件头第 2 条。20~30MB 放进内存是可接受的
// 代价，换来的是"下载超时"与"上传超时"彻底解耦。
type acliPackage struct {
	Data        []byte
	Size        int64
	SHA256      string
	MD5         string
	Filename    string
	DownloadURL string
	RemoteBuild string
	DeclaredLen int64 // 服务端声明的 Content-Length；-1 表示未知
	FetchedAt   time.Time
}

// ── 下载 ──────────────────────────────────────────────────────────────────────

// downloadAcliPackage 完整读取安装包并计算双摘要。
//
// 与历史实现的关键差异：读完 body 后才返回，因此 Content-Length 可以被真正
// 用于校验（历史实现只把它写进日志），并且连接会立刻释放，不再与上传共享超时。
func downloadAcliPackage(ctx context.Context, rawURL, filename, remoteBuild string) (*acliPackage, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, newAcliSyncError(ErrInstallReqFailed, "download", true, "创建安装包下载请求失败", err)
	}

	client := &http.Client{Timeout: acliDownloadTimeout}
	resp, err := client.Do(req)
	if err != nil {
		return nil, newAcliSyncError(ErrInstallDownload, "download", true, "从官方源下载 acli 安装包失败", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusPartialContent {
		return nil, newAcliSyncError(ErrInstallBadStatus, "download", true,
			fmt.Sprintf("下载源返回 HTTP %d", resp.StatusCode), nil)
	}

	declared := resp.ContentLength
	sha := sha256.New()
	md5Hasher := md5.New()
	var buf bytes.Buffer

	// 多读 1 字节用于识别"超过上限"，避免把 256MB 以上的响应整个吞进内存。
	written, err := io.Copy(io.MultiWriter(&buf, sha, md5Hasher), io.LimitReader(resp.Body, acliMaxPackageBytes+1))
	if err != nil {
		return nil, newAcliSyncError(ErrInstallDownload, "download", true, "读取安装包响应体失败", err)
	}
	if written > acliMaxPackageBytes {
		return nil, newAcliSyncError(ErrInstallDownload, "download", false,
			fmt.Sprintf("安装包超过 %d MiB 上限，已中止", acliMaxPackageBytes>>20), nil)
	}
	if written == 0 {
		return nil, newAcliSyncError(ErrInstallDownload, "download", true, "下载到的安装包为 0 字节（源站可能返回了错误页）", nil)
	}
	// 声明长度与实际字节必须一致：这是唯一一个由服务端产生、又与内容绑定的锚点。
	if declared >= 0 && written != declared {
		return nil, newAcliSyncError(ErrInstallTruncated, "download", true,
			fmt.Sprintf("安装包下载不完整：服务端声明 %d 字节，实际收到 %d 字节", declared, written), nil)
	}

	return &acliPackage{
		Data:        buf.Bytes(),
		Size:        written,
		SHA256:      hex.EncodeToString(sha.Sum(nil)),
		MD5:         hex.EncodeToString(md5Hasher.Sum(nil)),
		Filename:    sanitizeAcliFilename(filename, ""),
		DownloadURL: rawURL,
		RemoteBuild: remoteBuild,
		DeclaredLen: declared,
		FetchedAt:   time.Now(),
	}, nil
}

// acliFilenameRe 只接受官方包名形态的字符集。
var acliFilenameRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*\.acli$`)

// sanitizeAcliFilename 收敛远端给的 Content-Disposition 文件名。
//
// 对抗性审查：filename 来自**远端响应头**，却被直接拼进 /sf/data/local/<name>
// 并进入 shell 命令。不收敛就等于把"任意路径写入"交给对端（`../../etc/passwd`）。
// 只允许官方包名形态（字母数字开头 + 可含 ._- + .acli 结尾），其余一律回退默认名。
func sanitizeAcliFilename(name, arch string) string {
	base := strings.TrimSpace(name)
	if idx := strings.LastIndexAny(base, `/\`); idx >= 0 {
		base = base[idx+1:]
	}
	if acliFilenameRe.MatchString(base) && !strings.Contains(base, "..") {
		return base
	}
	if arch == "" {
		arch = "x86_64"
	}
	return fmt.Sprintf("acli-installer_%s.acli", arch)
}

// ── 上传（远端原子写 + 落盘校验）────────────────────────────────────────────
//
// S1 收口：命令构造、回显解析、校验比对、进度读取、tmp 清理五件事的唯一实现
// 已迁到 internal/shared/remote/fs.go。本文件只保留 acli 链路的**外壳语义**：
// LC-* 错误码、中文文案、三段独立预算。命令字节与旧实现逐字一致（golden
// 测试锁定），因此收口不改变任何远端行为——这是"抽取+委托"而非"重写"。

// buildAcliRemoteWriteCommand 构造"写入 → 校验 → 原子替换"的单条远端命令。
//
// 结构必须满足三点，缺一不可（论证见 remote/fs.go 顶部）：
//  1. **先校验再 mv**：校验失败只删 tmp，正式路径上的文件永远只会是完整包；
//  2. **以退出码表达结论**：任何一个分支都不允许"静默成功"；
//  3. **回显 size/algo/digest**：把远端事实写进日志，事后可复核。
func buildAcliRemoteWriteCommand(tmpPath, destPath string, pkg *acliPackage, mode string) string {
	return remote.BuildWriteCommand(remote.WriteSpec{
		Path:    destPath,
		TmpPath: tmpPath,
		Size:    pkg.Size,
		SHA256:  pkg.SHA256,
		MD5:     pkg.MD5,
		Mode:    mode,
	})
}

// acliWriteResult 是远端脚本回显的结构化事实。
//
// 类型别名而非新结构：字段名与旧实现完全一致，既有单测的字面量零改动；
// remote 侧多出的 Integrity/TmpPath 两个字段是收口新增的自证信息。
type acliWriteResult = remote.WriteResult

// parseAcliWriteOutput 从远端输出里提取校验回显（取最后一条有效行）。
func parseAcliWriteOutput(output string) acliWriteResult {
	return remote.ParseWriteOutput(output)
}

// verifyAcliRemoteWrite 比对"本地事实"与"远端事实"。
//
// 比对逻辑唯一实现在 remote.VerifyWrite；本函数只做**错误码与文案映射**：
// LC-* 与中文提示是 acli_sync 的 UI 契约（前端按码决定重试按钮），
// 不属于通用原语，因此留在链路侧。失败时 integrity 必须为空。
func verifyAcliRemoteWrite(pkg *acliPackage, r acliWriteResult) (string, error) {
	integrity, err := remote.VerifyWrite(remote.WriteSpec{
		Size:   pkg.Size,
		SHA256: pkg.SHA256,
		MD5:    pkg.MD5,
	}, r)
	if err != nil {
		return "", acliWriteErrorToSync(err, pkg, r)
	}
	return integrity, nil
}

// acliWriteErrorToSync 把 remote 的机器可判 Reason 映射成 LC-* 码与中文文案。
//
// 文案逐字保留收口前的形态：它们已经进了工单与用户截图，改字等于让历史
// 排障记录对不上号。Reason 未收录时按"通道异常"兜底，不静默当成成功。
func acliWriteErrorToSync(err error, pkg *acliPackage, r acliWriteResult) error {
	var writeErr *remote.WriteError
	if !errors.As(err, &writeErr) {
		return err
	}
	switch writeErr.Reason {
	case "size_mismatch":
		return newAcliSyncError(ErrInstallChecksum, "verify", true, fmt.Sprintf(
			"上传字节数不符：本地 %d 字节，远端 %d 字节（已丢弃远端残包）", pkg.Size, r.RemoteSize), nil)
	case "digest_mismatch":
		return newAcliSyncError(ErrInstallChecksum, "verify", true, fmt.Sprintf(
			"上传内容校验不符：本地 %s=%s，远端 %s=%s（已丢弃远端残包）",
			firstNonEmpty(pkg.SHA256, "-"), pkg.SHA256, r.Algo, r.Digest), nil)
	case "cat_failed":
		return newAcliSyncError(ErrInstallUpload, "upload", true, "远端写入失败（cat 未正常结束，常见于 SSH 通道中断）", nil)
	case "move_failed":
		return newAcliSyncError(ErrInstallUpload, "upload", true,
			"远端文件替换失败（mv/chmod 未成功，可能与残留文件权限有关）", nil)
	case "size_after_move":
		return newAcliSyncError(ErrInstallChecksum, "verify", true, fmt.Sprintf(
			"落盘大小不符：本地 %d 字节，远端 %d 字节", pkg.Size, r.RemoteSize), nil)
	case "sha256_after_move":
		return newAcliSyncError(ErrInstallChecksum, "verify", true, fmt.Sprintf(
			"落盘 SHA-256 不符：本地 %s，远端 %s", pkg.SHA256, r.Digest), nil)
	case "md5_after_move":
		return newAcliSyncError(ErrInstallChecksum, "verify", true, fmt.Sprintf(
			"落盘 MD5 不符：本地 %s，远端 %s", pkg.MD5, r.Digest), nil)
	case "timeout":
		return newAcliSyncError(ErrInstallUpload, "upload", true,
			fmt.Sprintf("上传超时（预算 %s）", acliUploadTimeout), writeErr.Err)
	case "channel_broken":
		return newAcliSyncError(ErrInstallUpload, "upload", true,
			"上传命令异常结束（SSH 通道中断或远端 shell 退出）", writeErr.Err)
	default:
		return newAcliSyncError(ErrInstallUpload, "upload", true,
			"远端未回传校验结论（命令被中断或 shell 提前退出）", nil)
	}
}

// acliProgressReader 统计已写入 SSH 通道的字节数，用于进度上报。
// 唯一实现在 remote/runner.go（回调在锁外执行，避免持锁发 WS 消息）。
type acliProgressReader = remote.ProgressReader

func newAcliProgressReader(data []byte, step int64, onStep func(sent, total int64)) *acliProgressReader {
	return remote.NewProgressReader(data, step, onStep)
}

// uploadAcliPackage 把已校验的字节写入节点，并在 mv 之前完成落盘校验。
//
// 与历史 uploadStream（已删除的死代码）的差异：
//   - stdout 被捕获（历史实现丢弃，导致远端校验结论无处可取）；
//   - 超时由调用方按 acliUploadTimeout 独立控制；
//   - 传输中断时主动清理 tmp，不留残包。
func uploadAcliPackage(
	ctx context.Context,
	s *SSHSession,
	pkg *acliPackage,
	destPath string,
	onProgress func(sent, total int64),
) (acliWriteResult, error) {
	if s == nil {
		return acliWriteResult{}, newAcliSyncError(ErrInstallUpload, "upload", true, "创建上传通道失败", errors.New("SSH 会话不存在"))
	}
	spec := remote.WriteSpec{
		Path:         destPath,
		TmpPath:      fmt.Sprintf("%s.tmp.%d", destPath, time.Now().UnixNano()),
		Data:         pkg.Data,
		Size:         pkg.Size,
		SHA256:       pkg.SHA256,
		MD5:          pkg.MD5,
		Mode:         "0755",
		OnProgress:   onProgress,
		ProgressStep: acliUploadProgressStep,
	}
	result, err := remote.WriteFile(ctx, newSSHRunner(s), spec)
	if err != nil {
		return result, acliWriteErrorToSync(err, pkg, result)
	}
	return result, nil
}

// cleanupAcliTmp 尽力删除远端临时文件。失败不改变主结论，只写日志。
func cleanupAcliTmp(ctx context.Context, s *SSHSession, tmpPath string) {
	remote.CleanupTmp(ctx, newSSHRunner(s), tmpPath)
}

// ── 安装 ──────────────────────────────────────────────────────────────────────

// buildAcliInstallCommand 构造安装命令。
//
// PATH 前置是历史踩坑修复：非交互 shell 默认 PATH 里没有 /sf/bin、/sf/sbin，
// 安装器内部会调用 acli 自身完成集群分发。
func buildAcliInstallCommand(destPath string) string {
	return `export PATH="$PATH:/sf/bin:/sf/sbin:/usr/sbin:/usr/local/bin"; ` +
		shellQuote(destPath) + " --install --force"
}

// acliInstallOutcome 汇总安装命令的完整事实，供日志与 UI 共用。
type acliInstallOutcome struct {
	Command    string
	ExitCode   int
	Stdout     string
	Stderr     string
	DurationMS int64
	Err        error
}

// installAcliPackage 执行安装并保留**完整**输出（不止预览）。
//
// 为什么必须分离 stdout/stderr：安装器的进度条走 stdout、真正的失败原因常常
// 只出现在 stderr；合并成一个字符串后，"退出码非零但看起来全是进度条"就无法解释。
func installAcliPackage(ctx context.Context, s *SSHSession, destPath string) acliInstallOutcome {
	outcome := acliInstallOutcome{Command: buildAcliInstallCommand(destPath)}
	start := time.Now()
	stdout, stderr, code, err := s.runQuickCommandStreams(ctx, outcome.Command)
	outcome.DurationMS = time.Since(start).Milliseconds()
	outcome.Stdout = stdout
	outcome.Stderr = stderr
	outcome.ExitCode = code
	outcome.Err = err
	return outcome
}

// acliOutputForLog 把安装输出裁剪成可落库的形态：头尾各留一段，
// 中间省略的字节数显式写出来，避免"日志里看不到结论"或"日志被进度条刷爆"。
func acliOutputForLog(outcome acliInstallOutcome) (preview string, bytesStdout, bytesStderr int) {
	bytesStdout = len(outcome.Stdout)
	bytesStderr = len(outcome.Stderr)
	combined := outcome.Stdout
	if strings.TrimSpace(outcome.Stderr) != "" {
		combined += "\n[stderr]\n" + outcome.Stderr
	}
	return formatOutputPreview(combined, 2048, 2048), bytesStdout, bytesStderr
}

// ── 安装后逐节点插件落盘校验 ─────────────────────────────────────────────────

// logCollectPluginBinary 是日志收集插件的可执行入口：安装器声称"集群分发成功"
// 之后，唯一能证明插件真的落到每台节点上的事实就是它存在且可执行。
const logCollectPluginBinary = "/sf/data/local/acli/plugins/log_collect/plugin"

// buildPluginProbeCommand 探测插件是否落盘且可执行。
// 只判 -x 不判 -f：空目录/半包文件可能留下不可执行的残骸，-x 才能证明可用。
func buildPluginProbeCommand() string {
	return fmt.Sprintf("[ -x %s ] && printf 'OK\\n'", shellQuote(logCollectPluginBinary))
}

// parsePluginProbeResult 判定探测输出：出现 OK 标记行即落盘成功。
func parsePluginProbeResult(output string) bool {
	for _, line := range strings.Split(strings.ReplaceAll(output, "\r", ""), "\n") {
		if strings.TrimSpace(line) == "OK" {
			return true
		}
	}
	return false
}

// nodePluginState 记录单节点的插件落盘事实，供日志与结果文案共用。
type nodePluginState struct {
	IP      string `json:"ip"`
	Present bool   `json:"present"`
	Detail  string `json:"detail,omitempty"`
}

// verifyClusterPluginLanding 安装后逐节点校验 log_collect 插件是否落盘。
//
// 为什么必须逐节点：安装器退出码 0 只代表发起节点装好了；集群分发可能静默
// 留下空目录（Q2026111700614 现场 47 个空目录被"安装成功"掩盖）。复用 spc
// 扇出的主机执行原语：本机直连当前会话，其余节点走 BatchMode ssh。
func (b *Bridge) verifyClusterPluginLanding(ctx context.Context, s *SSHSession) []nodePluginState {
	hosts := s.spcNodeList(ctx)
	if len(hosts) == 0 {
		return nil
	}
	localIPs := s.spcLocalAddresses(ctx)
	states := make([]nodePluginState, len(hosts))
	sem := make(chan struct{}, spcFanoutConcurrency)
	var wg sync.WaitGroup
	for index, host := range hosts {
		wg.Add(1)
		go func(index int, ip string) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			stdout, stderr, _, err := s.runSpcOnHostStreams(ctx, ip, localIPs[ip], buildPluginProbeCommand())
			state := nodePluginState{IP: ip, Present: parsePluginProbeResult(stdout)}
			if !state.Present {
				state.Detail = strings.TrimSpace(stderr)
				if err != nil {
					state.Detail = err.Error()
				}
			}
			states[index] = state
		}(index, host.IP)
	}
	wg.Wait()
	return states
}

// missingPluginNodes 汇总未落盘节点 IP，供如实警示文案使用。
func missingPluginNodes(states []nodePluginState) []string {
	var missing []string
	for _, state := range states {
		if !state.Present {
			missing = append(missing, state.IP)
		}
	}
	return missing
}

// ── 缓存（支撑"只重试上传"）──────────────────────────────────────────────────

type acliPackageCacheEntry struct {
	pkg      *acliPackage
	cachedAt time.Time
}

// acliPackageCache 按会话键缓存"已下载且摘要已算出"的安装包。
//
// 存在的唯一理由：前端提供「重试上传」时，用户要的是"别再去官网下一遍"。
// 缓存必须带 TTL —— 官方 latest 可能已经换了包，长期缓存会让重试装回旧版本。
type acliPackageCache struct {
	mu    sync.Mutex
	items map[string]acliPackageCacheEntry
}

func newAcliPackageCache() *acliPackageCache {
	return &acliPackageCache{items: make(map[string]acliPackageCacheEntry)}
}

func (c *acliPackageCache) put(key string, pkg *acliPackage) {
	if c == nil || key == "" || pkg == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.items[key] = acliPackageCacheEntry{pkg: pkg, cachedAt: time.Now()}
}

func (c *acliPackageCache) get(key string) *acliPackage {
	if c == nil || key == "" {
		return nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	entry, ok := c.items[key]
	if !ok {
		return nil
	}
	if time.Since(entry.cachedAt) > acliPackageCacheTTL {
		delete(c.items, key)
		return nil
	}
	return entry.pkg
}

func (c *acliPackageCache) drop(key string) {
	if c == nil || key == "" {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.items, key)
}

// ── 日志字段取值（允许 pkg 为空：install-only 复用时不重新下载）────────────────

func pkgSize(pkg *acliPackage) int64 {
	if pkg == nil {
		return 0
	}
	return pkg.Size
}

func pkgSHA(pkg *acliPackage) string {
	if pkg == nil {
		return ""
	}
	return pkg.SHA256
}

func pkgMD(pkg *acliPackage) string {
	if pkg == nil {
		return ""
	}
	return pkg.MD5
}

// ── 远端已有包探测（install-only 重试用）──────────────────────────────────────

// acliProbeMissingMark 表示节点上不存在可用安装包（唯一实现在 remote/probe.go）。
const acliProbeMissingMark = remote.ProbeMissingMark

// buildAcliRemoteProbeCommand 只读地报告远端文件的 size/digest。
//
// 为什么 install-only 必须先探测：直接重跑安装命令等于无条件信任盘上那份文件；
// 而我们上一轮正是因为「盘上那份包不对」才失败的。探测让「复用」变成可验证的决定。
func buildAcliRemoteProbeCommand(path string) string {
	return remote.BuildProbeCommand(path)
}

// parseAcliProbeOutput 解析探测结果；missing 为真表示节点上没有可用包。
//
// 四元组返回值是 acli 链路的既有签名（调用方与单测均按此解构），
// 因此保留外壳，内部委托 remote.ParseProbeOutput。
func parseAcliProbeOutput(output string) (size int64, algo, digest string, missing bool) {
	result := remote.ParseProbeOutput(output)
	return result.Size, result.Algo, result.Digest, result.Missing
}

// probeRemoteAcliPackage 读取远端安装包事实（size + 可用时的 digest）。
//
// 探测失败与探测到缺失是两种结论：前者返回 err（不猜"都存在"也不猜
// "都不存在"），后者 missing=true。混为一谈会让重试逻辑走错分支。
func probeRemoteAcliPackage(ctx context.Context, s *SSHSession, path string) (size int64, algo, digest string, missing bool, err error) {
	if s == nil {
		return 0, "", "", true, errors.New("SSH 会话不存在")
	}
	result, err := remote.ProbeFile(ctx, newSSHRunner(s), path)
	if err != nil {
		return 0, "", "", false, err
	}
	return result.Size, result.Algo, result.Digest, result.Missing, nil
}

// findRemoteAcliPackage 在节点上找已存在的 acli 安装包，供「重试安装」兜底。
//
// 为什么需要它：重试安装的唯一前提是“包已经在节点上”，而路径里的 filename
// 来自 probeAcliLatest —— 官网一旦不可达就拿不到文件名，路径就拼错，重试必然
// 退化成“重新下载失败”。先在节点上找实存的包，兜底才真的能兜住。
// 返回空串表示“没找到”（调用方继续走完整传输），不抛错。
func findRemoteAcliPackage(ctx context.Context, s *SSHSession) string {
	if s == nil {
		return ""
	}
	// -t 按 mtime 降序：多份残包时优先取最新的那份。
	out, code, err := s.runQuickCommand(ctx, "ls -1t /sf/data/local/*.acli 2>/dev/null | head -1")
	if err != nil || code != 0 {
		return ""
	}
	path := strings.TrimSpace(out)
	if path == "" || strings.ContainsAny(path, "\n\r") || !strings.HasPrefix(path, "/sf/data/local/") {
		return ""
	}
	base := path[strings.LastIndex(path, "/")+1:]
	// 先验形状再回传：路径会进入 shell 命令与 destPath，不能把远端输出当可信输入。
	if !acliFilenameRe.MatchString(base) || strings.Contains(base, "..") {
		return ""
	}
	return "/sf/data/local/" + base
}

// verifyReusedRemotePackage 判定「盘上那份包」是否可以复用。
//
// 只接受与本地事实一致的包：size 必须相等，digest 在远端可得时也必须相等。
// 远端只有 size_only 时，要求本地 pkg 存在（否则没有基线）——没有基线就不复用。
func verifyReusedRemotePackage(pkg *acliPackage, size int64, algo, digest string) error {
	if pkg == nil {
		return newAcliSyncError(ErrInstallChecksum, "verify", false,
			"节点上已有同名安装包，但本地没有可比对的校验基线，为避免装到旧包/残包，需要重新上传", nil)
	}
	if size != pkg.Size {
		return newAcliSyncError(ErrInstallChecksum, "verify", true, fmt.Sprintf(
			"节点上的安装包大小与本地基线不符：本地 %d 字节，节点 %d 字节", pkg.Size, size), nil)
	}
	switch algo {
	case "sha256":
		if !strings.EqualFold(digest, pkg.SHA256) {
			return newAcliSyncError(ErrInstallChecksum, "verify", true, fmt.Sprintf(
				"节点上的安装包 SHA-256 与本地基线不符：本地 %s，节点 %s", pkg.SHA256, digest), nil)
		}
	case "md5":
		if !strings.EqualFold(digest, pkg.MD5) {
			return newAcliSyncError(ErrInstallChecksum, "verify", true, fmt.Sprintf(
				"节点上的安装包 MD5 与本地基线不符：本地 %s，节点 %s", pkg.MD5, digest), nil)
		}
	}
	return nil
}

// ── 传输互斥 ────────────────────────────────────────────────────────────────

// acquireAcliTransfer 抢占同一会话的安装包传输权。
//
// 返回的 release 必须 defer 调用；ok=false 表示已有同会话传输在进行，
// 调用方应直接回 LC-INSTALL-009（可重试），而不是排队——排队会让用户
// 连点变成串行重传，同一 destPath 仍可能被后来的旧字节覆盖。
func (b *Bridge) acquireAcliTransfer(key string) (release func(), ok bool) {
	b.acliMu.Lock()
	defer b.acliMu.Unlock()
	if b.acliBusy == nil {
		b.acliBusy = make(map[string]bool)
	}
	if b.acliBusy[key] {
		return nil, false
	}
	b.acliBusy[key] = true
	return func() {
		b.acliMu.Lock()
		delete(b.acliBusy, key)
		b.acliMu.Unlock()
	}, true
}

// packageCache 惰性初始化缓存（Bridge 可能在测试中零值构造）。
func (b *Bridge) packageCache() *acliPackageCache {
	b.acliMu.Lock()
	defer b.acliMu.Unlock()
	if b.acliCache == nil {
		b.acliCache = newAcliPackageCache()
	}
	return b.acliCache
}

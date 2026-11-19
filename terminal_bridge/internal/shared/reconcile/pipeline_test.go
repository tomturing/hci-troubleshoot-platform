package reconcile

import (
	"bytes"
	"context"
	"crypto/md5"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"regexp"
	"strconv"
	"strings"
	"testing"

	"terminal_bridge/internal/shared/remote"
)

// nodeSim 是 HCI 节点的内存模拟器：它真的按写入协议落盘/校验/替换，
// 因此"断链后有没有半截文件""磁盘满会不会写坏正式路径"这类行为级验收
// 可以在单测里证明，而不是靠断言命令字符串长得像。
//
// 为什么不用字符串断言代替：命令字符串正确 ≠ 行为正确。收口设计 §六 S1
// 验收第 2 条明确要求 config/spc 走行为级验收（改 tmp+mv 必然改字节）。
type nodeSim struct {
	files map[string][]byte
	dirs  map[string]bool
	cmds  []string

	hasSHA256 bool
	hasMD5    bool
	failCat   bool // 模拟 stdin 中断（cat 非零退出）
	failMV    bool // 模拟目标目录只读 / 残留文件权限
	diskFull  bool // 模拟写入只落一半字节
	bitFlip   bool // 模拟保长大小的内容损坏（size_only 拦不住，只有摘要/cmp 能拦）
	noProbe   bool // 模拟探测命令跑不起来（不可达 / 命令缺失）

	// written 记录正式路径上真正出现过的内容，供"无半截文件"断言。
	written map[string][]byte
}

func newNodeSim() *nodeSim {
	return &nodeSim{
		files: map[string][]byte{}, dirs: map[string]bool{},
		hasSHA256: true, written: map[string][]byte{},
	}
}

func (n *nodeSim) digest(data []byte) (string, string) {
	switch {
	case n.hasSHA256:
		sum := sha256.Sum256(data)
		return "sha256", hex.EncodeToString(sum[:])
	case n.hasMD5:
		sum := md5.Sum(data)
		return "md5", hex.EncodeToString(sum[:])
	default:
		return "size_only", ""
	}
}

var (
	simCatRe  = regexp.MustCompile(`cat > '([^']*)'`)
	simMoveRe = regexp.MustCompile(`mv -f '([^']*)' '([^']*)'`)
	simCopyRe = regexp.MustCompile(`cp -p '([^']*)' '([^']*)'`)
	simSizeRe = regexp.MustCompile(`\[ "\$SIZE" != "([0-9]+)" \]`)
	simSHARes = regexp.MustCompile(`\[ "\$ALGO" = "sha256" \] && \[ "\$DIG" != "([0-9a-f]*)" \]`)
	simMD5Re  = regexp.MustCompile(`\[ "\$ALGO" = "md5" \] && \[ "\$DIG" != "([0-9a-f]*)" \]`)
	simProbe  = regexp.MustCompile(`\[ ! -s '([^']*)' \]`)
	simCmpRe  = regexp.MustCompile(`(?s)^printf '%s' (.*) \| cmp -s - '([^']*)'$`)
	simMkdir  = regexp.MustCompile(`^mkdir -p '([^']*)'$`)
	simRm     = regexp.MustCompile(`^rm -f '([^']*)'$`)
)

func (n *nodeSim) run(cmd string, stdin []byte) (string, int, error) {
	n.cmds = append(n.cmds, cmd)
	switch {
	case simMkdir.MatchString(cmd):
		dir := simMkdir.FindStringSubmatch(cmd)[1]
		if n.diskFull {
			return "mkdir: cannot create directory: No space left on device", 1, nil
		}
		n.dirs[dir] = true
		return "", 0, nil

	case simRm.MatchString(cmd):
		delete(n.files, simRm.FindStringSubmatch(cmd)[1])
		return "", 0, nil

	case strings.Contains(cmd, "cat > ") || strings.Contains(cmd, "umask 077; if cp -p "):
		return n.transfer(cmd, stdin)

	case strings.Contains(cmd, remote.ProbeMissingMark):
		if n.noProbe {
			return "", -1, fmt.Errorf("ssh: connection refused")
		}
		path := simProbe.FindStringSubmatch(cmd)[1]
		data, ok := n.files[path]
		if !ok || len(data) == 0 {
			return remote.ProbeMissingMark + "\n", 0, nil
		}
		algo, dig := n.digest(data)
		return fmt.Sprintf("size=%d algo=%s digest=%s\n", len(data), algo, dig), 0, nil

	case simCmpRe.MatchString(cmd):
		m := simCmpRe.FindStringSubmatch(cmd)
		payload := []byte(unquoteSim(m[1]))
		data, ok := n.files[m[2]]
		if !ok || !bytes.Equal(payload, data) {
			return "", 1, nil
		}
		return "", 0, nil

	default:
		return "sh: command not found", 127, nil
	}
}

// transfer 按写入协议执行：填充 tmp → 量大小/算摘要 → 比对 → mv → chmod。
func (n *nodeSim) transfer(cmd string, stdin []byte) (string, int, error) {
	var tmp, dest string
	var data []byte
	if m := simCatRe.FindStringSubmatch(cmd); m != nil {
		tmp = m[1]
		if n.failCat {
			return remote.WriteFailMark + "cat_failed\n", 20, nil
		}
		data = stdin
		if n.diskFull {
			data = stdin[:len(stdin)/2] // 只落一半：磁盘满的真实形态
		}
	} else if m := simCopyRe.FindStringSubmatch(cmd); m != nil {
		src, ok := n.files[m[1]]
		if !ok {
			return remote.WriteFailMark + "cat_failed\n", 20, nil
		}
		tmp, data = m[2], src
	} else {
		return "sh: parse error", 127, nil
	}
	if m := simMoveRe.FindStringSubmatch(cmd); m != nil {
		dest = m[2]
	}
	n.files[tmp] = data
	if n.bitFlip && len(data) > 0 {
		// 保长损坏：大小校验完全看不出来，这正是"只比大小"不够的原因。
		corrupt := append([]byte(nil), data...)
		corrupt[len(corrupt)/2] ^= 0xFF
		n.files[tmp] = corrupt
		data = corrupt
	}

	algo, dig := n.digest(data)
	var out strings.Builder
	fmt.Fprintf(&out, "%ssize=%d algo=%s digest=%s\n", remote.WriteMark, len(data), algo, dig)

	wantSize, _ := strconv.ParseInt(simSizeRe.FindStringSubmatch(cmd)[1], 10, 64)
	if int64(len(data)) != wantSize {
		fmt.Fprintf(&out, "%ssize_mismatch\n", remote.WriteFailMark)
		delete(n.files, tmp)
		return out.String(), 21, nil
	}
	if algo == "sha256" {
		if m := simSHARes.FindStringSubmatch(cmd); m != nil && m[1] != dig {
			fmt.Fprintf(&out, "%sdigest_mismatch\n", remote.WriteFailMark)
			delete(n.files, tmp)
			return out.String(), 22, nil
		}
	}
	if algo == "md5" {
		if m := simMD5Re.FindStringSubmatch(cmd); m != nil && m[1] != dig {
			fmt.Fprintf(&out, "%sdigest_mismatch\n", remote.WriteFailMark)
			delete(n.files, tmp)
			return out.String(), 22, nil
		}
	}
	if n.failMV {
		fmt.Fprintf(&out, "%smove_failed\n", remote.WriteFailMark)
		delete(n.files, tmp)
		return out.String(), 23, nil
	}
	n.files[dest] = data
	n.written[dest] = data
	delete(n.files, tmp)
	out.WriteString(remote.WriteOKMark + "\n")
	return out.String(), 0, nil
}

// unquoteSim 按 POSIX 单引号规则反解析，用于从命令里取回原文。
func unquoteSim(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); {
		switch s[i] {
		case '\'':
			i++
			for i < len(s) && s[i] != '\'' {
				b.WriteByte(s[i])
				i++
			}
			i++
		case '\\':
			i++
			if i < len(s) {
				b.WriteByte(s[i])
				i++
			}
		default:
			b.WriteByte(s[i])
			i++
		}
	}
	return b.String()
}

// simRunner 把 nodeSim 适配成 remote.Runner。
type simRunner struct{ node *nodeSim }

func (r simRunner) RunQuickCommand(ctx context.Context, command string) (string, int, error) {
	if err := ctx.Err(); err != nil {
		return "", -1, err
	}
	return r.node.run(command, nil)
}

func (r simRunner) WriteStdin(ctx context.Context, command string, reader io.Reader) (string, int, error) {
	if err := ctx.Err(); err != nil {
		return "", -1, err
	}
	data, err := io.ReadAll(reader)
	if err != nil {
		return "", -1, err
	}
	return r.node.run(command, data)
}

func configDesired(t *testing.T, raw string) DesiredState {
	t.Helper()
	p := InlineMessage{
		Validate: func(string) error { return nil },
		Smoke:    SmokeSpec{Kind: SmokeSHA256},
		Version:  VersionSpec{Mode: VersionNone},
	}
	list, err := p.Desired(context.Background(), SyncRequest{
		Kind: KindConfigFile, NodeIP: "172.28.24.1", TaskID: "task-1",
		Raw: raw, Path: "/sf/data/local/acli/tmp/platform/log_collect/cfg-task-1.json",
		Dir: "/sf/data/local/acli/tmp/platform/log_collect", Mode: "0644",
	})
	if err != nil {
		t.Fatalf("构造期望态失败：%v", err)
	}
	d, _, err := Normalize(list[0])
	if err != nil {
		t.Fatalf("归一化失败：%v", err)
	}
	return d
}

const configRaw = `{"paths":"/sf/data/logs/a,/sf/data/logs/b"}`

func TestPipelineConfigHappyPath(t *testing.T) {
	node := newNodeSim()
	d := configDesired(t, configRaw)
	res := Run(context.Background(), Deps{Runner: simRunner{node}}, d)
	if res.Status != StatusOK {
		t.Fatalf("期望 ok，实际 %s", res.Summary())
	}
	if res.Degraded {
		t.Fatalf("sha256 档位不应标降级：%s", res.Summary())
	}
	got := string(node.written[d.Files[0].Path])
	if got != configRaw {
		t.Fatalf("正式路径上的内容不是平台原文：%q", got)
	}
	if !node.dirs[d.Files[0].Dir] {
		t.Fatal("写前没有重建目录")
	}
	if res.Field("integrity") != "sha256" {
		t.Fatalf("校验档位应为 sha256，实际 %q", res.Field("integrity"))
	}
	// 正式路径只被 mv 写过一次，tmp 不残留。
	for path := range node.files {
		if strings.Contains(path, ".tmp.") {
			t.Fatalf("临时文件残留：%s", path)
		}
	}
}

// 断链（cat 非零退出）后正式路径必须不存在，且 tmp 被清掉。
// 这是 config 直写时代的核心事故：截断配置 + acli 静默跳过 = 任务报成功数据没采到。
func TestPipelineConfigBrokenChannelLeavesNoHalfFile(t *testing.T) {
	node := newNodeSim()
	node.failCat = true
	d := configDesired(t, configRaw)
	res := Run(context.Background(), Deps{Runner: simRunner{node}}, d)
	if res.Status != StatusError {
		t.Fatalf("断链必须失败，实际 %s", res.Summary())
	}
	if res.Stage != StageWrite {
		t.Fatalf("失败段应为 write，实际 %q", res.Stage)
	}
	if _, ok := node.written[d.Files[0].Path]; ok {
		t.Fatal("断链后正式路径上出现了文件（半截配置）")
	}
	for path := range node.files {
		if strings.Contains(path, ".tmp.") {
			t.Fatalf("断链后 tmp 残留：%s", path)
		}
	}
}

// 磁盘满：只落一半字节 → 大小校验拦住 → 正式路径不被污染。
func TestPipelineConfigDiskFullLeavesNoHalfFile(t *testing.T) {
	node := newNodeSim()
	node.diskFull = true
	d := configDesired(t, configRaw)
	res := Run(context.Background(), Deps{Runner: simRunner{node}}, d)
	if res.Status != StatusError {
		t.Fatalf("磁盘满必须失败，实际 %s", res.Summary())
	}
	if _, ok := node.written[d.Files[0].Path]; ok {
		t.Fatal("磁盘满后正式路径上出现了半截配置")
	}
	for path := range node.files {
		if strings.Contains(path, ".tmp.") {
			t.Fatalf("磁盘满后 tmp 残留：%s", path)
		}
	}
}

// mv 失败（目标只读 / 残留文件权限）同样不得留半截文件。
func TestPipelineConfigMoveFailedLeavesNoHalfFile(t *testing.T) {
	node := newNodeSim()
	node.failMV = true
	d := configDesired(t, configRaw)
	res := Run(context.Background(), Deps{Runner: simRunner{node}}, d)
	if res.Status != StatusError || res.Stage != StageWrite {
		t.Fatalf("mv 失败应回 write 段错误，实际 %s", res.Summary())
	}
	if _, ok := node.written[d.Files[0].Path]; ok {
		t.Fatal("mv 失败后正式路径上出现了文件")
	}
}

// 宿主机没有 sha256sum/md5sum（极简系统）：降级为 printf 重传 + cmp 逐字节比对，
// 且必须显式标降级——静默降级等于把"没校验"伪装成"校验通过"。
func TestPipelineConfigFallsBackToByteCompare(t *testing.T) {
	node := newNodeSim()
	node.hasSHA256, node.hasMD5 = false, false
	d := configDesired(t, configRaw)
	res := Run(context.Background(), Deps{Runner: simRunner{node}}, d)
	if res.Status != StatusOK {
		t.Fatalf("cmp 兜底应成功，实际 %s", res.Summary())
	}
	if !res.Degraded {
		t.Fatal("size_only 档位必须标降级")
	}
	if res.Field("smoke") != "cmp" {
		t.Fatalf("应走 cmp 逐字节比对，实际 %q", res.Field("smoke"))
	}
	if string(node.written[d.Files[0].Path]) != configRaw {
		t.Fatal("cmp 兜底后内容不是平台原文")
	}
}

// cmp 兜底必须能抓住"保长内容损坏"：size_only 档位下大小校验完全看不出来，
// 只有逐字节比对能拦。这是"探测不了就跳过"会放过的真实事故形态。
func TestPipelineConfigByteCompareCatchesSilentCorruption(t *testing.T) {
	node := newNodeSim()
	node.hasSHA256, node.hasMD5 = false, false
	node.bitFlip = true
	d := configDesired(t, configRaw)
	res := Run(context.Background(), Deps{Runner: simRunner{node}}, d)
	if res.Status != StatusError || res.Stage != StageVerify {
		t.Fatalf("保长损坏必须被 cmp 拦在 verify 段，实际 %s", res.Summary())
	}
	if res.Field("smoke") != "cmp" {
		t.Fatalf("应走 cmp 逐字节比对，实际 %q", res.Field("smoke"))
	}
}

// 有 sha256sum 时，保长损坏必须在 mv **之前**就被拦住（先校验再替换）。
func TestPipelineConfigDigestCatchesCorruptionBeforeMove(t *testing.T) {
	node := newNodeSim()
	node.bitFlip = true
	d := configDesired(t, configRaw)
	res := Run(context.Background(), Deps{Runner: simRunner{node}}, d)
	if res.Status != StatusError {
		t.Fatalf("摘要不符必须失败：%s", res.Summary())
	}
	if _, ok := node.written[d.Files[0].Path]; ok {
		t.Fatal("校验失败后正式路径被污染（应先校验再 mv）")
	}
}

// ctx 取消：写入已开工后取消，必须主动下发 rm -f 清理 tmp，
// 不留残包——否则下次续传会把半包当成"已传部分"。
func TestPipelineConfigCancelCleansTmp(t *testing.T) {
	node := newNodeSim()
	d := configDesired(t, configRaw)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	runner := cancelAfterWrite{inner: simRunner{node}, cancel: cancel}
	res := Run(ctx, Deps{Runner: runner}, d)
	if res.Status != StatusError {
		t.Fatalf("取消必须失败，实际 %s", res.Summary())
	}
	cleaned := false
	for _, c := range node.cmds {
		if strings.HasPrefix(c, "rm -f ") {
			cleaned = true
		}
	}
	if !cleaned {
		t.Fatal("取消后没有下发 tmp 清理命令")
	}
	for path := range node.files {
		if strings.Contains(path, ".tmp.") {
			t.Fatalf("取消后 tmp 残留：%s", path)
		}
	}
}

// cancelAfterWrite 在写入命令返回后立刻取消 ctx，模拟"传到一半用户断开"。
type cancelAfterWrite struct {
	inner  remote.Runner
	cancel context.CancelFunc
}

func (c cancelAfterWrite) RunQuickCommand(ctx context.Context, command string) (string, int, error) {
	return c.inner.RunQuickCommand(ctx, command)
}

func (c cancelAfterWrite) WriteStdin(ctx context.Context, command string, r io.Reader) (string, int, error) {
	out, code, err := c.inner.WriteStdin(ctx, command, r)
	c.cancel()
	return out, code, err
}

// package 来源没有下载器 → fail-closed 回 package_source_undefined，不静默截断。
func TestPipelinePackageSourceUndefined(t *testing.T) {
	node := newNodeSim()
	d := DesiredState{
		Kind: KindPluginBundle, NodeIP: "172.28.24.1",
		Files: []FileSpec{{Path: "/sf/plugin.tar.gz", Size: 10,
			Source: Source{Kind: SourcePackage, URL: "https://platform/internal/pkg"}}},
		Version: VersionSpec{Mode: VersionNone},
	}
	res := Run(context.Background(), Deps{Runner: simRunner{node}}, d)
	if res.ErrorCode != CodePackageSourceUndef {
		t.Fatalf("期望 %s，实际 %s", CodePackageSourceUndef, res.Summary())
	}
	if len(node.written) != 0 {
		t.Fatal("来源未配置时不应写任何文件")
	}
}

// 产品特有安装动作未登记渲染器 → fail-closed，不假装装好了。
func TestPipelineInstallRendererRequired(t *testing.T) {
	node := newNodeSim()
	d := configDesired(t, configRaw)
	d.Install = InstallSpec{Kind: InstallAcli, Args: []string{"/sf/data/local/acli.acli"}}
	res := Run(context.Background(), Deps{Runner: simRunner{node}}, d)
	if res.Status != StatusError || res.ErrorCode != CodeInstallUnsupported {
		t.Fatalf("未登记渲染器应 fail-closed，实际 %s", res.Summary())
	}
}

// 安装失败必须停在 install 段，不继续冒烟、不落账本。
func TestPipelineInstallFailureStopsBeforeLedger(t *testing.T) {
	node := newNodeSim()
	store := NewStore(t.TempDir())
	d := configDesired(t, configRaw)
	d.Install = InstallSpec{Kind: InstallAcli}
	res := Run(context.Background(), Deps{
		Runner: simRunner{node}, State: store,
		InstallFunc: func(ctx context.Context, r remote.Runner, dd DesiredState) (remote.ExecResult, error) {
			return remote.ExecResult{ExitCode: 1, Stdout: "install boom"}, nil
		},
	}, d)
	if res.Status != StatusError || res.Stage != StageInstall {
		t.Fatalf("安装失败应停在 install 段，实际 %s", res.Summary())
	}
	if st, _ := store.Load(d.Kind, d.NodeIP); st != nil {
		t.Fatal("安装失败不得落账本（否则下次会凭账本误判已同步）")
	}
}

// 成功后落账本，且第二次同内容下发能凭"账本 + 节点事实双对齐"Skip。
func TestPipelineLedgerEnablesSkipOnSecondRun(t *testing.T) {
	node := newNodeSim()
	store := NewStore(t.TempDir())
	d := configDesired(t, configRaw)
	d.Version = VersionSpec{Mode: VersionDigest}
	deps := Deps{Runner: simRunner{node}, State: store}

	first := Run(context.Background(), deps, d)
	if first.Status != StatusOK {
		t.Fatalf("首次收敛应成功：%s", first.Summary())
	}
	second := Run(context.Background(), deps, d)
	if second.Status != StatusSkipped || second.Degraded {
		t.Fatalf("双对齐后应 Skip 且不降级，实际 %s", second.Summary())
	}
	writes := 0
	for _, c := range node.cmds {
		if strings.Contains(c, "cat > ") {
			writes++
		}
	}
	if writes != 1 {
		t.Fatalf("Skip 后不应再写盘，实际写入 %d 次", writes)
	}
}

// B4：节点被第三方清空后，即使账本对齐也必须重新 Install。
func TestPipelineNodeWipedIgnoresLedger(t *testing.T) {
	node := newNodeSim()
	store := NewStore(t.TempDir())
	d := configDesired(t, configRaw)
	d.Version = VersionSpec{Mode: VersionDigest}
	deps := Deps{Runner: simRunner{node}, State: store}
	if res := Run(context.Background(), deps, d); res.Status != StatusOK {
		t.Fatalf("首次收敛应成功：%s", res.Summary())
	}
	delete(node.files, d.Files[0].Path) // acli 重装清空插件目录
	res := Run(context.Background(), deps, d)
	if res.Status != StatusOK {
		t.Fatalf("节点被清空后必须重新写入，实际 %s", res.Summary())
	}
	if string(node.written[d.Files[0].Path]) != configRaw {
		t.Fatal("清空后没有把期望态写回去")
	}
}

// B6：探测失败 + 账本对齐 → 允许 Skip，但必须 degraded=true / stage=probe。
func TestPipelineProbeFailureSkipIsDegraded(t *testing.T) {
	node := newNodeSim()
	store := NewStore(t.TempDir())
	d := configDesired(t, configRaw)
	d.Version = VersionSpec{Mode: VersionDigest}
	deps := Deps{Runner: simRunner{node}, State: store}
	if res := Run(context.Background(), deps, d); res.Status != StatusOK {
		t.Fatalf("首次收敛应成功：%s", res.Summary())
	}
	node.noProbe = true
	res := Run(context.Background(), deps, d)
	if res.Status != StatusSkipped {
		t.Fatalf("探测失败且账本对齐应 Skip，实际 %s", res.Summary())
	}
	if !res.Degraded || res.Stage != StageProbe {
		t.Fatalf("降级 Skip 必须带 degraded=true 与 stage=probe，实际 %s", res.Summary())
	}
}

// 探测失败且无账本 → 不得 Skip（没凭据证明已同步）。
func TestPipelineProbeFailureWithoutLedgerApplies(t *testing.T) {
	node := newNodeSim()
	node.noProbe = true
	d := configDesired(t, configRaw)
	d.Version = VersionSpec{Mode: VersionDigest}
	res := Run(context.Background(), Deps{Runner: simRunner{node}}, d)
	if res.Status == StatusSkipped {
		t.Fatalf("无账本时探测失败不得 Skip：%s", res.Summary())
	}
}

// SourceRemote：节点内搬迁同样先校验再 mv，源文件缺失不得污染目标。
func TestPipelineRemoteSourceCopy(t *testing.T) {
	node := newNodeSim()
	src := "/sf/backup/core.json"
	node.files[src] = []byte(configRaw)
	sum := sha256.Sum256([]byte(configRaw))
	d := DesiredState{
		Kind: KindSpcSlot, NodeIP: "172.28.24.1",
		Files: []FileSpec{{Path: "/sf/spc/core.json", Size: int64(len(configRaw)),
			SHA256: hex.EncodeToString(sum[:]), Source: Source{Kind: SourceRemote, Path: src}}},
		Version: VersionSpec{Mode: VersionNone},
	}
	res := Run(context.Background(), Deps{Runner: simRunner{node}}, d)
	if res.Status != StatusOK {
		t.Fatalf("节点内搬迁应成功：%s", res.Summary())
	}
	if string(node.written["/sf/spc/core.json"]) != configRaw {
		t.Fatal("搬迁后内容不符")
	}

	node2 := newNodeSim()
	res2 := Run(context.Background(), Deps{Runner: simRunner{node2}}, d)
	if res2.Status != StatusError {
		t.Fatalf("源缺失必须失败：%s", res2.Summary())
	}
	if len(node2.written) != 0 {
		t.Fatal("源缺失时目标路径被污染")
	}
}

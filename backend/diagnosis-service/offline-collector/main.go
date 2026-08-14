package main

import (
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

type runnerError struct {
	code    int
	message string
}

func (err *runnerError) Error() string { return err.message }

type cliOptions struct {
	fingerprint    string
	bundleDir      string
	outputDir      string
	outputPath     string
	source         string
	timezone       string
	clockOffsetMS  int
	bundleType     string
	parentBundleID string
	cleanup        bool
	yes            bool
}

func parseOptions(arguments []string) (*cliOptions, error) {
	executable, _ := os.Executable()
	defaults := filepath.Dir(executable)
	options := &cliOptions{}
	flags := flag.NewFlagSet("hci-collect-linux-amd64", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	flags.StringVar(&options.fingerprint, "expected-root-fingerprint", "", "可信第二通道提供的 Ed25519 公钥 SHA-256 指纹（必填）")
	flags.StringVar(&options.bundleDir, "bundle-dir", defaults, "解压后的 Verification Bundle 目录")
	flags.StringVar(&options.outputDir, "output-dir", "./diagnostic-output", "明文采集输出目录")
	flags.StringVar(&options.outputPath, "output", "", "加密证据包输出路径")
	flags.StringVar(&options.source, "source", "", "采集来源标识，默认本机主机名")
	flags.StringVar(&options.timezone, "timezone", "", "采集时区，默认取 case.json")
	flags.IntVar(&options.clockOffsetMS, "clock-offset-ms", 0, "客户机时钟偏移毫秒数")
	flags.StringVar(&options.bundleType, "bundle-type", "initial", "证据包类型：initial 或 supplement")
	flags.StringVar(&options.parentBundleID, "parent-bundle-id", "", "补采证据包父包 ID")
	flags.BoolVar(&options.cleanup, "cleanup-plaintext", false, "加密成功后清理清单声明的明文")
	flags.BoolVar(&options.yes, "yes", false, "跳过采集范围人工确认")
	if err := flags.Parse(arguments); err != nil {
		return nil, &runnerError{code: exitUsage, message: err.Error()}
	}
	options.fingerprint = strings.TrimSpace(options.fingerprint)
	if options.fingerprint == "" {
		return nil, &runnerError{code: exitUsage, message: "缺少 --expected-root-fingerprint：必须从独立可信渠道取得并显式传入"}
	}
	if options.bundleType != "initial" && options.bundleType != "supplement" {
		return nil, &runnerError{code: exitUsage, message: "--bundle-type 只能是 initial 或 supplement"}
	}
	if options.bundleType == "supplement" && options.parentBundleID == "" {
		return nil, &runnerError{code: exitUsage, message: "--bundle-type supplement 必须同时提供 --parent-bundle-id"}
	}
	if len(options.source) > 255 {
		return nil, &runnerError{code: exitUsage, message: "--source 长度不能超过 255 字符"}
	}
	return options, nil
}

func preflight(bundleDir string) error {
	if runtime.GOOS != "linux" || runtime.GOARCH != "amd64" {
		return fmt.Errorf("当前运行时仅支持 Linux x86_64，实际为 %s/%s", runtime.GOOS, runtime.GOARCH)
	}
	info, err := os.Stat(bundleDir)
	if err != nil || !info.IsDir() {
		return fmt.Errorf("验证包目录不存在：%s", bundleDir)
	}
	for _, name := range []string{"artifact-manifest.json", "runtime-manifest.json", "trust-store.json", "revocations.json", "case.json"} {
		if info, err := os.Stat(filepath.Join(bundleDir, name)); err != nil || !info.Mode().IsRegular() {
			return fmt.Errorf("验证包缺少必要文件：%s", name)
		}
	}
	return nil
}

func run(arguments []string) error {
	options, err := parseOptions(arguments)
	if err != nil {
		return err
	}
	bundleDir, err := filepath.Abs(options.bundleDir)
	if err != nil {
		return &runnerError{code: exitPreflight, message: err.Error()}
	}
	if err := preflight(bundleDir); err != nil {
		return &runnerError{code: exitPreflight, message: err.Error()}
	}
	fmt.Println("[1/4] 信任链验证 …")
	artifact, execution, caseData, err := verifyBundle(bundleDir, options.fingerprint)
	if err != nil {
		return &runnerError{code: exitVerify, message: "信任链验证失败：" + err.Error()}
	}
	fmt.Println("  验证通过：Go 运行时、Manifest、采集制品和吊销快照均可信。")
	renderScopeSummary(artifact, caseData)
	if err := confirmScope(options.yes); err != nil {
		return err
	}
	outputDir, err := filepath.Abs(options.outputDir)
	if err != nil {
		return &runnerError{code: exitCollection, message: err.Error()}
	}
	if err := runCollection(execution, outputDir); err != nil {
		return &runnerError{code: exitCollection, message: err.Error()}
	}
	rows, stats, err := loadExecutionRows(outputDir)
	if err != nil {
		return &runnerError{code: exitCollection, message: err.Error()}
	}
	source := options.source
	if source == "" {
		source, _ = os.Hostname()
	}
	timezone := options.timezone
	if timezone == "" {
		timezone = caseData.IncidentTimezone
	}
	if timezone == "" {
		zone, _ := time.Now().Zone()
		timezone = zone
	}
	outputPath := options.outputPath
	if outputPath == "" {
		outputPath = fmt.Sprintf("%s_%s.hci-eb", caseData.CaseID, time.Now().Format("20060102150405"))
	}
	result, err := packageEvidence(packageOptions{
		outputDir: outputDir, outputPath: outputPath, source: source, timezone: timezone,
		clockOffsetMS: options.clockOffsetMS, bundleType: options.bundleType,
		parentBundleID: options.parentBundleID, cleanup: options.cleanup,
	}, bundleDir, artifact, caseData, rows)
	if err != nil {
		return &runnerError{code: exitPackaging, message: err.Error()}
	}
	fmt.Println("\n==== 完成 ====")
	fmt.Printf("采集：成功 %d / 失败 %d / 人工附件 %d\n", stats.Success, stats.Failed, stats.Manual)
	fmt.Printf("证据包：%s  大小=%d  SHA-256=%s\n", result.Path, result.SizeBytes, result.SHA256)
	if options.cleanup {
		fmt.Printf("明文清理：已清理 %d 个文件\n", result.RemovedFiles)
	} else {
		fmt.Println("明文清理：未请求；确认上传成功后可手工删除，或重跑时增加 --cleanup-plaintext。")
	}
	fmt.Println("下一步：通过客户界面「证据上传」提交该 .hci-eb。")
	return nil
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		var typed *runnerError
		if errors.As(err, &typed) {
			fmt.Fprintln(os.Stderr, "错误："+typed.message)
			os.Exit(typed.code)
		}
		fmt.Fprintln(os.Stderr, "未预期内部错误："+err.Error())
		os.Exit(exitInternal)
	}
}

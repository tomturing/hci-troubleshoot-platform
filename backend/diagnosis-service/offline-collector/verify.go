package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

var fingerprintPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)
var itemIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

type trustedKey struct {
	Algorithm            string `json:"algorithm"`
	KeyID                string `json:"key_id"`
	PublicKeyBase64      string `json:"public_key_base64"`
	PublicKeyFingerprint string `json:"public_key_fingerprint"`
}

type trustStore struct {
	Keys []trustedKey `json:"keys"`
}

func canonicalJSON(value any) ([]byte, error) {
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(buffer.Bytes(), []byte("\n")), nil
}

func decodeObject(content []byte) (map[string]any, error) {
	decoder := json.NewDecoder(bytes.NewReader(content))
	decoder.UseNumber()
	var value map[string]any
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	if value == nil {
		return nil, fmt.Errorf("JSON 根节点必须是对象")
	}
	return value, nil
}

func verifySignedDocument(content []byte, publicKey ed25519.PublicKey, keyID, label string) (map[string]any, error) {
	document, err := decodeObject(content)
	if err != nil {
		return nil, fmt.Errorf("%s 不是合法 JSON：%w", label, err)
	}
	signatureValue, ok := document["document_signature"]
	if !ok {
		return nil, fmt.Errorf("%s 缺少 document_signature", label)
	}
	signature, ok := signatureValue.(map[string]any)
	if !ok || signature["algorithm"] != "Ed25519" || signature["key_id"] != keyID {
		return nil, fmt.Errorf("%s 的签名元数据不可信", label)
	}
	fingerprint := sha256.Sum256(publicKey)
	if signature["public_key_fingerprint"] != hex.EncodeToString(fingerprint[:]) {
		return nil, fmt.Errorf("%s 的公钥指纹不一致", label)
	}
	signatureBase64, ok := signature["signature_base64"].(string)
	if !ok {
		return nil, fmt.Errorf("%s 的签名不是 Base64 字符串", label)
	}
	signatureBytes, err := base64.StdEncoding.Strict().DecodeString(signatureBase64)
	if err != nil || len(signatureBytes) != ed25519.SignatureSize {
		return nil, fmt.Errorf("%s 的 Ed25519 签名格式无效", label)
	}
	delete(document, "document_signature")
	payload, err := canonicalJSON(document)
	if err != nil {
		return nil, fmt.Errorf("%s 无法规范化：%w", label, err)
	}
	if !ed25519.Verify(publicKey, payload, signatureBytes) {
		return nil, fmt.Errorf("%s 的 Ed25519 签名无效", label)
	}
	return document, nil
}

func establishTrust(bundleDir, expectedFingerprint string) (ed25519.PublicKey, string, error) {
	normalized := strings.ToLower(strings.TrimSpace(expectedFingerprint))
	if !fingerprintPattern.MatchString(normalized) {
		return nil, "", fmt.Errorf("受信根指纹必须是 64 位 SHA-256")
	}
	content, err := os.ReadFile(filepath.Join(bundleDir, "trust-store.json"))
	if err != nil {
		return nil, "", fmt.Errorf("无法读取 trust-store.json：%w", err)
	}
	var store trustStore
	if err := json.Unmarshal(content, &store); err != nil || len(store.Keys) != 1 {
		return nil, "", fmt.Errorf("P0 trust-store 必须且只能包含一个受信密钥")
	}
	key := store.Keys[0]
	if key.Algorithm != "Ed25519" || key.KeyID == "" {
		return nil, "", fmt.Errorf("trust-store 密钥元数据不合法")
	}
	publicBytes, err := base64.StdEncoding.Strict().DecodeString(key.PublicKeyBase64)
	if err != nil || len(publicBytes) != ed25519.PublicKeySize {
		return nil, "", fmt.Errorf("trust-store Ed25519 公钥格式无效")
	}
	fingerprint := sha256.Sum256(publicBytes)
	actual := hex.EncodeToString(fingerprint[:])
	if actual != key.PublicKeyFingerprint || actual != normalized {
		return nil, "", fmt.Errorf("trust-store 公钥指纹与可信第二通道不一致")
	}
	return ed25519.PublicKey(publicBytes), key.KeyID, nil
}

func verifyRuntime(bundleDir string, publicKey ed25519.PublicKey, keyID string) error {
	content, err := os.ReadFile(filepath.Join(bundleDir, "runtime-manifest.json"))
	if err != nil {
		return fmt.Errorf("无法读取 runtime-manifest.json：%w", err)
	}
	document, err := verifySignedDocument(content, publicKey, keyID, "runtime-manifest")
	if err != nil {
		return err
	}
	expected, ok := document["sha256"].(string)
	if !ok || !fingerprintPattern.MatchString(expected) {
		return fmt.Errorf("runtime-manifest 缺少合法 SHA-256")
	}
	if document["schema_version"] != "1.0" || document["file_name"] != "hci-collect-linux-amd64" || document["os"] != "linux" || document["arch"] != "amd64" {
		return fmt.Errorf("runtime-manifest 平台或版本声明不合法")
	}
	executable, err := os.Executable()
	if err != nil {
		return fmt.Errorf("无法定位当前 Go 运行时：%w", err)
	}
	actual, actualSize, err := hashFile(executable)
	if err != nil {
		return fmt.Errorf("无法计算 Go 运行时摘要：%w", err)
	}
	if actual != expected {
		return fmt.Errorf("Go 离线运行时 SHA-256 不一致，拒绝执行")
	}
	expectedSize, ok := document["size_bytes"].(json.Number)
	if !ok || expectedSize.String() != fmt.Sprintf("%d", actualSize) {
		return fmt.Errorf("Go 离线运行时文件大小不一致，拒绝执行")
	}
	return nil
}

func verifyBundle(bundleDir, expectedFingerprint string) (*artifactManifest, *collectorArtifact, *caseDocument, error) {
	publicKey, keyID, err := establishTrust(bundleDir, expectedFingerprint)
	if err != nil {
		return nil, nil, nil, err
	}
	if err := verifyRuntime(bundleDir, publicKey, keyID); err != nil {
		return nil, nil, nil, err
	}
	manifestContent, err := os.ReadFile(filepath.Join(bundleDir, "artifact-manifest.json"))
	if err != nil {
		return nil, nil, nil, fmt.Errorf("无法读取 artifact-manifest.json：%w", err)
	}
	if _, err := verifySignedDocument(manifestContent, publicKey, keyID, "artifact-manifest"); err != nil {
		return nil, nil, nil, err
	}
	var artifact artifactManifest
	if err := json.Unmarshal(manifestContent, &artifact); err != nil {
		return nil, nil, nil, fmt.Errorf("artifact-manifest 契约不合法：%w", err)
	}
	if artifact.SchemaVersion != "1.2" || artifact.ArtifactType != "structured_collector" ||
		artifact.FileName == "" || filepath.Base(artifact.FileName) != artifact.FileName {
		return nil, nil, nil, fmt.Errorf("artifact-manifest 版本或文件名不合法")
	}
	if artifact.BundleEncryption == nil || artifact.BundleEncryption.Algorithm != "AES-256-GCM" ||
		artifact.BundleEncryption.KeyWrapAlgorithm != "RSA-OAEP-SHA256" ||
		(artifact.BundleEncryption.Format != "HCIEB1" && artifact.BundleEncryption.Format != "HCIEB2") ||
		artifact.BundleEncryption.KeyID == "" || artifact.BundleEncryption.PublicKeyPEMBase64 == "" {
		return nil, nil, nil, fmt.Errorf("采集器制品缺少合法的证据包加密公钥，请重新生成制品")
	}
	artifactPath := filepath.Join(bundleDir, artifact.FileName)
	artifactHash, _, err := hashFile(artifactPath)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("无法读取采集器制品：%w", err)
	}
	if artifactHash != artifact.ArtifactSHA256 {
		return nil, nil, nil, fmt.Errorf("采集器制品 SHA-256 不一致")
	}
	artifactBytes, err := os.ReadFile(artifactPath)
	if err != nil {
		return nil, nil, nil, err
	}
	signatureBytes, err := base64.StdEncoding.Strict().DecodeString(artifact.Signature.SignatureBase64)
	publicFingerprint := sha256.Sum256(publicKey)
	if err != nil || artifact.Signature.Algorithm != "Ed25519" || artifact.Signature.KeyID != keyID ||
		artifact.Signature.PublicKeyFingerprint != hex.EncodeToString(publicFingerprint[:]) ||
		artifact.Signature.PublicKeyBase64 != base64.StdEncoding.EncodeToString(publicKey) ||
		!ed25519.Verify(publicKey, artifactBytes, signatureBytes) {
		return nil, nil, nil, fmt.Errorf("采集器制品的 Ed25519 签名无效")
	}
	expiresAt, err := time.Parse(time.RFC3339Nano, artifact.Signature.ExpiresAt)
	if err != nil || !time.Now().Before(expiresAt) {
		return nil, nil, nil, fmt.Errorf("采集器制品已过期")
	}
	var execution collectorArtifact
	if err := json.Unmarshal(artifactBytes, &execution); err != nil {
		return nil, nil, nil, fmt.Errorf("结构化采集制品不是合法 JSON：%w", err)
	}
	if err := validateCollectorArtifact(&artifact, &execution); err != nil {
		return nil, nil, nil, err
	}
	revocationContent, err := os.ReadFile(filepath.Join(bundleDir, "revocations.json"))
	if err != nil {
		return nil, nil, nil, fmt.Errorf("无法读取 revocations.json：%w", err)
	}
	revocations, err := verifySignedDocument(revocationContent, publicKey, keyID, "revocations")
	if err != nil {
		return nil, nil, nil, err
	}
	nextUpdate, ok := revocations["next_update_at"].(string)
	nextUpdateAt, parseErr := time.Parse(time.RFC3339Nano, nextUpdate)
	if !ok || parseErr != nil || !time.Now().Before(nextUpdateAt) {
		return nil, nil, nil, fmt.Errorf("吊销清单已过期，必须重新下载验证包")
	}
	if rows, ok := revocations["revoked_artifacts"].([]any); ok {
		for _, row := range rows {
			if item, ok := row.(map[string]any); ok && item["artifact_id"] == artifact.ArtifactID {
				return nil, nil, nil, fmt.Errorf("采集器制品已被吊销")
			}
		}
	}
	caseContent, err := os.ReadFile(filepath.Join(bundleDir, "case.json"))
	if err != nil {
		return nil, nil, nil, fmt.Errorf("无法读取 case.json：%w", err)
	}
	if _, err := verifySignedDocument(caseContent, publicKey, keyID, "case"); err != nil {
		return nil, nil, nil, err
	}
	var caseData caseDocument
	if err := json.Unmarshal(caseContent, &caseData); err != nil || caseData.CaseID == "" || caseData.SessionID != artifact.SessionID || len(caseData.Targets) == 0 {
		return nil, nil, nil, fmt.Errorf("case.json 契约不合法")
	}
	return &artifact, &execution, &caseData, nil
}

func validateCollectorArtifact(manifest *artifactManifest, artifact *collectorArtifact) error {
	if artifact.SchemaVersion != "1.0" || artifact.ArtifactID != manifest.ArtifactID || artifact.SessionID != manifest.SessionID ||
		artifact.CollectionPlanID != manifest.CollectionPlanID || artifact.TargetKey != manifest.TargetKey ||
		len(artifact.ExecutionItems) != len(manifest.CollectionItems) {
		return fmt.Errorf("结构化采集制品与 Manifest 身份不一致")
	}
	for index, item := range artifact.ExecutionItems {
		declared := manifest.CollectionItems[index]
		if !itemIDPattern.MatchString(item.ItemID) || item.ItemID != declared.PlanItemID || item.CollectorID != declared.CollectorID ||
			item.CollectorRevision != declared.CollectorRevision || item.CollectorChecksum != declared.CollectorChecksum ||
			item.Executor != declared.Executor ||
			item.TimeoutSeconds < 1 || item.TimeoutSeconds > 3600 || item.MaxOutputBytes < 1 || item.MaxOutputBytes > 4*1024*1024 {
			return fmt.Errorf("结构化采集项 %d 契约不合法", index+1)
		}
		seenSourceRefs := make(map[string]bool, len(item.SourceSignalRefs))
		for _, source := range item.SourceSignalRefs {
			key := source.SupportID + "\x00" + source.SignalID
			if source.SupportID == "" || len(source.SupportID) > 128 || source.SignalID == "" || len(source.SignalID) > 128 ||
				strings.ContainsRune(source.SupportID, '\x00') || strings.ContainsRune(source.SignalID, '\x00') || seenSourceRefs[key] {
				return fmt.Errorf("结构化采集项 %d 来源 Signal 契约不合法", index+1)
			}
			seenSourceRefs[key] = true
		}
		switch item.Executor {
		case "command":
			if len(item.Argv) == 0 || item.Argv[0] == "" || len(item.Argv) > 256 {
				return fmt.Errorf("直接命令采集项缺少合法 argv")
			}
			for _, value := range item.Argv {
				if strings.ContainsRune(value, '\x00') {
					return fmt.Errorf("直接命令 argv 包含 NUL 字符")
				}
			}
			executable := filepath.Base(item.Argv[0])
			forbiddenExecutables := map[string]bool{
				"sh": true, "bash": true, "dash": true, "zsh": true,
				"env": true, "xargs": true, "sudo": true,
			}
			if forbiddenExecutables[executable] {
				return fmt.Errorf("结构化制品禁止调用命令解释器或命令转发器：%s", executable)
			}
			if err := validateReadOnlyCommand(executable, item.Argv[1:]); err != nil {
				return err
			}
		case "http":
			if item.Method != "GET" || !strings.HasPrefix(item.Path, "/") || strings.Contains(item.Path, "..") || strings.Contains(item.Path, "://") {
				return fmt.Errorf("HCI API 采集项只允许固定相对 GET 路径")
			}
		case "manual":
			if strings.TrimSpace(item.Guide) == "" || strings.ContainsRune(item.Guide, '\x00') {
				return fmt.Errorf("人工附件采集项缺少合法指引")
			}
		default:
			return fmt.Errorf("结构化采集项执行器不受支持：%s", item.Executor)
		}
	}
	return nil
}

func validateReadOnlyCommand(executable string, arguments []string) error {
	allowed := map[string]bool{
		"acli": true, "date": true, "df": true, "dmidecode": true, "echo": true,
		"ethtool": true, "free": true, "hostname": true, "ip": true, "journalctl": true,
		"kubectl": true, "lscpu": true, "lsblk": true, "nvme": true, "raidcli": true, "smartctl": true,
		"ss": true, "systemctl": true, "task": true, "true": true, "uname": true, "uptime": true,
	}
	if !allowed[executable] {
		return fmt.Errorf("结构化制品可执行程序不在只读白名单中：%s", executable)
	}
	mutating := map[string]bool{
		"add": true, "apply": true, "attach-ns": true, "change": true, "clear": true,
		"connect": true, "create": true, "create-ns": true, "del": true, "delete": true,
		"delete-ns": true, "detach-ns": true, "disable": true, "disconnect": true,
		"enable": true, "exec": true, "flush": true, "format": true, "fw-commit": true,
		"fw-download": true, "install": true, "kill": true, "offline": true, "online": true,
		"patch": true, "poweroff": true, "reboot": true, "remove": true, "replace": true,
		"reset": true, "restart": true, "rm": true, "sanitize": true, "security-send": true,
		"set": true, "set-feature": true, "shutdown": true, "start": true, "stop": true,
		"subsystem-reset": true, "update": true, "upgrade": true, "write": true,
		"write-zeroes": true,
	}
	forbiddenOptions := map[string]map[string]bool{
		"ethtool": {
			"-A": true, "-C": true, "-G": true, "-K": true, "-L": true,
			"-N": true, "-Q": true, "-X": true, "-s": true, "--change": true,
		},
		"journalctl": {"--flush": true, "--relinquish-var": true, "--rotate": true, "--sync": true},
		"smartctl": {
			"-B": true, "-o": true, "-s": true, "-t": true,
			"--offlineauto": true, "--saveauto": true, "--smart": true, "--test": true,
		},
	}
	first := ""
	for _, argument := range arguments {
		normalized := strings.ToLower(argument)
		optionName := strings.SplitN(argument, "=", 2)[0]
		if forbiddenOptions[executable][optionName] ||
			(executable == "journalctl" && strings.HasPrefix(argument, "--vacuum-")) {
			return fmt.Errorf("结构化制品包含变更系统状态的选项：%s", argument)
		}
		if first == "" && !strings.HasPrefix(normalized, "-") {
			first = normalized
		}
		canonical := strings.SplitN(strings.TrimLeft(normalized, "-"), "=", 2)[0]
		if mutating[canonical] {
			return fmt.Errorf("结构化制品包含变更系统状态的参数：%s", argument)
		}
	}
	subcommands := map[string]map[string]bool{
		"kubectl":   {"describe": true, "get": true, "logs": true, "top": true, "version": true},
		"raidcli":   {"show": true},
		"systemctl": {"is-active": true, "is-enabled": true, "list-units": true, "show": true, "status": true},
		"task":      {"get": true, "list": true, "show": true},
	}
	if choices, ok := subcommands[executable]; ok && !choices[first] {
		return fmt.Errorf("结构化制品子命令不在只读白名单中：%s %s", executable, first)
	}
	return nil
}

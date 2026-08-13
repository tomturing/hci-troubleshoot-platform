package main

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/aes"
	"crypto/cipher"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"testing"
	"time"
)

type fixture struct {
	directory   string
	fingerprint string
	artifact    *artifactManifest
	execution   *collectorArtifact
	caseData    *caseDocument
	privateRSA  *rsa.PrivateKey
}

func signedDocument(t *testing.T, document map[string]any, privateKey ed25519.PrivateKey, keyID string) map[string]any {
	t.Helper()
	payload, err := canonicalJSON(document)
	if err != nil {
		t.Fatal(err)
	}
	publicKey := privateKey.Public().(ed25519.PublicKey)
	fingerprint := sha256.Sum256(publicKey)
	copy := make(map[string]any, len(document)+1)
	for key, value := range document {
		copy[key] = value
	}
	copy["document_signature"] = map[string]any{
		"algorithm": "Ed25519", "key_id": keyID,
		"signature_base64":       base64.StdEncoding.EncodeToString(ed25519.Sign(privateKey, payload)),
		"public_key_fingerprint": hex.EncodeToString(fingerprint[:]),
	}
	return copy
}

func writeJSON(t *testing.T, path string, value any) {
	t.Helper()
	content, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, append(content, '\n'), 0o600); err != nil {
		t.Fatal(err)
	}
}

func buildFixture(t *testing.T) fixture {
	t.Helper()
	directory := t.TempDir()
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	fingerprintBytes := sha256.Sum256(publicKey)
	fingerprint := hex.EncodeToString(fingerprintBytes[:])
	keyID := "go-test-root"
	writeJSON(t, filepath.Join(directory, "trust-store.json"), map[string]any{
		"schema_version": "1.0",
		"keys": []map[string]any{{
			"algorithm": "Ed25519", "key_id": keyID, "public_key_base64": base64.StdEncoding.EncodeToString(publicKey),
			"public_key_fingerprint": fingerprint, "status": "trusted",
		}},
	})
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	runtimeHash, runtimeSize, err := hashFile(executable)
	if err != nil {
		t.Fatal(err)
	}
	runtimeDocument := signedDocument(t, map[string]any{
		"schema_version": "1.0", "file_name": "hci-collect-linux-amd64", "os": "linux", "arch": "amd64",
		"sha256": runtimeHash, "size_bytes": runtimeSize, "built_with": "Go standard library",
	}, privateKey, keyID)
	writeJSON(t, filepath.Join(directory, "runtime-manifest.json"), runtimeDocument)

	privateRSA, err := rsa.GenerateKey(rand.Reader, 3072)
	if err != nil {
		t.Fatal(err)
	}
	publicDER, err := x509.MarshalPKIXPublicKey(&privateRSA.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	publicPEM := pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: publicDER})
	artifactDocument := map[string]any{
		"schema_version": "1.0", "artifact_id": "artifact-1", "session_id": "session-1",
		"collection_plan_id": "plan-1", "target_key": "all",
		"execution_items": []map[string]any{{
			"item_id": "item-1", "collector_id": "hostname", "collector_revision": 1,
			"collector_checksum": "fixture", "executor": "command", "timeout_seconds": 30,
			"max_output_bytes": 1048576, "argv": []string{"/bin/echo", "node-1"},
		}},
	}
	artifactContent, err := json.Marshal(artifactDocument)
	if err != nil {
		t.Fatal(err)
	}
	artifactContent = append(artifactContent, '\n')
	artifactName := "collector_fixture.hci-collector.json"
	if err := os.WriteFile(filepath.Join(directory, artifactName), artifactContent, 0o600); err != nil {
		t.Fatal(err)
	}
	artifactHash := sha256.Sum256(artifactContent)
	now := time.Now().UTC()
	manifestUnsigned := map[string]any{
		"schema_version": "1.2", "artifact_type": "structured_collector", "artifact_id": "artifact-1", "session_id": "session-1",
		"collection_plan_id": "plan-1", "target_key": "all", "file_name": artifactName,
		"profile": map[string]any{"name": "fixture", "revision": 1, "version": "1.0.0", "checksum": "fixture"},
		"bundle_encryption": map[string]any{
			"algorithm": "AES-256-GCM", "key_wrap_algorithm": "RSA-OAEP-SHA256", "key_id": "rsa-test",
			"public_key_pem_base64": base64.StdEncoding.EncodeToString(publicPEM), "format": "HCIEB1",
		},
		"artifact_sha256": hex.EncodeToString(artifactHash[:]),
		"signature": map[string]any{
			"algorithm": "Ed25519", "key_id": keyID,
			"signature_base64":  base64.StdEncoding.EncodeToString(ed25519.Sign(privateKey, artifactContent)),
			"public_key_base64": base64.StdEncoding.EncodeToString(publicKey), "public_key_fingerprint": fingerprint,
			"signed_at": now.Format(time.RFC3339Nano), "expires_at": now.Add(time.Hour).Format(time.RFC3339Nano),
		},
		"collection_items": []map[string]any{{
			"plan_item_id": "item-1", "collector_id": "hostname", "collector_revision": 1,
			"collector_checksum": "fixture", "executor": "command",
			"target":          map[string]any{"type": "node", "id": "node-1"},
			"time_window":     map[string]any{"start_time": now.Add(-time.Hour).Format(time.RFC3339Nano), "end_time": now.Format(time.RFC3339Nano)},
			"output_contract": map[string]any{"output_path": "commands/item-1.stdout", "media_type": "text/plain"},
		}},
	}
	writeJSON(t, filepath.Join(directory, "artifact-manifest.json"), signedDocument(t, manifestUnsigned, privateKey, keyID))
	writeJSON(t, filepath.Join(directory, "revocations.json"), signedDocument(t, map[string]any{
		"schema_version": "1.0", "generated_at": now.Format(time.RFC3339Nano),
		"next_update_at": now.Add(time.Hour).Format(time.RFC3339Nano), "revoked_artifacts": []any{},
	}, privateKey, keyID))
	writeJSON(t, filepath.Join(directory, "case.json"), signedDocument(t, map[string]any{
		"case_id": "Q-GO-TEST", "session_id": "session-1", "selected_scenario": "fixture", "incident_timezone": "Asia/Shanghai",
		"incident_window": map[string]string{"start": now.Add(-time.Hour).Format(time.RFC3339Nano), "end": now.Format(time.RFC3339Nano)},
		"targets":         []map[string]any{{"type": "node", "id": "node-1"}},
	}, privateKey, keyID))
	artifact, execution, caseData, err := verifyBundle(directory, fingerprint)
	if err != nil {
		t.Fatal(err)
	}
	return fixture{directory: directory, fingerprint: fingerprint, artifact: artifact, execution: execution, caseData: caseData, privateRSA: privateRSA}
}

func TestVerifyBundleAndRejectTampering(t *testing.T) {
	fixture := buildFixture(t)
	artifactPath := filepath.Join(fixture.directory, fixture.artifact.FileName)
	if err := os.WriteFile(artifactPath, []byte("tampered"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, _, err := verifyBundle(fixture.directory, fixture.fingerprint); err == nil {
		t.Fatal("被篡改的采集制品未被拒绝")
	}
}

func TestVerifyBundleRejectsTamperedCase(t *testing.T) {
	fixture := buildFixture(t)
	casePath := filepath.Join(fixture.directory, "case.json")
	caseContent, err := os.ReadFile(casePath)
	if err != nil {
		t.Fatal(err)
	}
	caseContent = bytes.Replace(caseContent, []byte("Q-GO-TEST"), []byte("Q-TAMPERED"), 1)
	if err := os.WriteFile(casePath, caseContent, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, _, err := verifyBundle(fixture.directory, fixture.fingerprint); err == nil {
		t.Fatal("被篡改的 case.json 未被拒绝")
	}
}

func TestStructuredCollectorExecutesArgvDirectly(t *testing.T) {
	fixture := buildFixture(t)
	outputDirectory := filepath.Join(fixture.directory, "direct-output")
	if err := runCollection(fixture.execution, outputDirectory); err != nil {
		t.Fatal(err)
	}
	rows, stats, err := loadExecutionRows(outputDirectory)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 || stats.Success != 1 || rows[0].OutputTruncated {
		t.Fatalf("直接命令执行结果不合法：%+v %+v", rows, stats)
	}
	content, err := os.ReadFile(filepath.Join(outputDirectory, "commands", "item-1.stdout"))
	if err != nil || string(content) != "node-1\n" {
		t.Fatalf("直接命令输出错误：%q %v", content, err)
	}
}

func TestStructuredCollectorRejectsShellInterpreter(t *testing.T) {
	fixture := buildFixture(t)
	fixture.execution.ExecutionItems[0].Argv = []string{"/bin/sh", "-c", "hostname"}
	if err := validateCollectorArtifact(fixture.artifact, fixture.execution); err == nil {
		t.Fatal("结构化采集制品不应允许 Shell 解释器")
	}
}

func TestStructuredCollectorRejectsManifestBindingMismatch(t *testing.T) {
	fixture := buildFixture(t)
	fixture.execution.ExecutionItems[0].CollectorChecksum = "tampered-checksum"
	if err := validateCollectorArtifact(fixture.artifact, fixture.execution); err == nil {
		t.Fatal("结构化采集制品不应接受与 Manifest 不一致的 Collector 摘要")
	}
}

func TestStructuredCollectorAcceptsSignedSourceSignalRefs(t *testing.T) {
	fixture := buildFixture(t)
	fixture.execution.ExecutionItems[0].SourceSignalRefs = []sourceSignalRef{{
		SupportID: "SAMPLE-SIG-VM",
		SignalID:  "vm_status_must",
	}}
	if err := validateCollectorArtifact(fixture.artifact, fixture.execution); err != nil {
		t.Fatalf("合法来源 Signal 应被接受: %v", err)
	}
	fixture.execution.ExecutionItems[0].SourceSignalRefs = append(
		fixture.execution.ExecutionItems[0].SourceSignalRefs,
		fixture.execution.ExecutionItems[0].SourceSignalRefs[0],
	)
	if err := validateCollectorArtifact(fixture.artifact, fixture.execution); err == nil {
		t.Fatal("重复来源 Signal 应 fail closed")
	}
}

func TestPackageEvidenceProducesDecryptableHCIEB1(t *testing.T) {
	fixture := buildFixture(t)
	outputDirectory := filepath.Join(fixture.directory, "output")
	if err := os.MkdirAll(filepath.Join(outputDirectory, "commands"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(outputDirectory, "commands", "item-1.stdout"), []byte("node-1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	exitCode := 0
	outputPath := filepath.Join(fixture.directory, "evidence.hci-eb")
	result, err := packageEvidence(packageOptions{
		outputDir: outputDirectory, outputPath: outputPath, source: "node-1", timezone: "Asia/Shanghai", bundleType: "initial",
	}, fixture.directory, fixture.artifact, fixture.caseData, []executionRow{{ItemID: "item-1", CollectorID: "hostname", ExitCode: &exitCode}})
	if err != nil {
		t.Fatal(err)
	}
	if result.SizeBytes <= 7 || result.SHA256 == "" {
		t.Fatalf("加密包元数据不合法：%+v", result)
	}
	plaintext := decryptFixtureBundle(t, outputPath, fixture.privateRSA)
	gzipReader, err := gzip.NewReader(bytes.NewReader(plaintext))
	if err != nil {
		t.Fatal(err)
	}
	archive := tar.NewReader(gzipReader)
	names := map[string]bool{}
	for {
		header, err := archive.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		names[header.Name] = true
	}
	for _, expected := range []string{"case.json", "manifest.json", "commands/item-1.stdout"} {
		if !names[expected] {
			t.Fatalf("归档缺少 %s", expected)
		}
	}
}

func TestPackageEvidenceProducesDecryptableHCIEB2(t *testing.T) {
	fixture := buildFixture(t)
	fixture.artifact.BundleEncryption.Format = "HCIEB2"
	outputDirectory := filepath.Join(fixture.directory, "output-v2")
	if err := os.MkdirAll(filepath.Join(outputDirectory, "commands"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(outputDirectory, "commands", "item-1.stdout"), bytes.Repeat([]byte("node-1\n"), 200000), 0o600); err != nil {
		t.Fatal(err)
	}
	exitCode := 0
	outputPath := filepath.Join(fixture.directory, "evidence-v2.hci-eb")
	if _, err := packageEvidence(packageOptions{
		outputDir: outputDirectory, outputPath: outputPath, source: "node-1", timezone: "Asia/Shanghai", bundleType: "initial",
	}, fixture.directory, fixture.artifact, fixture.caseData, []executionRow{{ItemID: "item-1", CollectorID: "hostname", ExitCode: &exitCode}}); err != nil {
		t.Fatal(err)
	}
	plaintext := decryptFixtureBundle(t, outputPath, fixture.privateRSA)
	if len(plaintext) == 0 {
		t.Fatal("HCIEB2 解密结果为空")
	}
}

func decryptFixtureBundle(t *testing.T, path string, privateKey *rsa.PrivateKey) []byte {
	t.Helper()
	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	isV2 := bytes.HasPrefix(content, []byte(magicV2))
	if !isV2 && !bytes.HasPrefix(content, []byte(magicV1)) {
		t.Fatal("HCIEB magic 不合法")
	}
	offset := len(magicV1)
	headerLength := int(binary.BigEndian.Uint32(content[offset : offset+4]))
	offset += 4
	var header map[string]string
	if err := json.Unmarshal(content[offset:offset+headerLength], &header); err != nil {
		t.Fatal(err)
	}
	offset += headerLength
	wrapped, _ := base64.StdEncoding.DecodeString(header["encrypted_data_key"])
	dataKey, err := rsa.DecryptOAEP(sha256.New(), rand.Reader, privateKey, wrapped, nil)
	if err != nil {
		t.Fatal(err)
	}
	block, err := aes.NewCipher(dataKey)
	if err != nil {
		t.Fatal(err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		t.Fatal(err)
	}
	if isV2 {
		noncePrefix, _ := base64.StdEncoding.DecodeString(header["nonce_prefix"])
		plaintext := make([]byte, 0)
		counter := uint64(0)
		for offset < len(content) {
			if offset+4 > len(content) {
				t.Fatal("HCIEB2 分块长度缺失")
			}
			sealedLength := int(binary.BigEndian.Uint32(content[offset : offset+4]))
			offset += 4
			if offset+sealedLength > len(content) {
				t.Fatal("HCIEB2 分块截断")
			}
			nonce := make([]byte, gcm.NonceSize())
			copy(nonce, noncePrefix)
			binary.BigEndian.PutUint64(nonce[4:], counter)
			aad := make([]byte, headerLength+8)
			copy(aad, content[len(magicV2)+4:len(magicV2)+4+headerLength])
			binary.BigEndian.PutUint64(aad[headerLength:], counter)
			chunk, openErr := gcm.Open(nil, nonce, content[offset:offset+sealedLength], aad)
			if openErr != nil {
				t.Fatal(openErr)
			}
			plaintext = append(plaintext, chunk...)
			offset += sealedLength
			counter++
		}
		return plaintext
	}
	nonce, _ := base64.StdEncoding.DecodeString(header["nonce"])
	tag, _ := base64.StdEncoding.DecodeString(header["tag"])
	plaintext, err := gcm.Open(nil, nonce, append(content[offset:], tag...), nil)
	if err != nil {
		t.Fatal(err)
	}
	return plaintext
}

func TestUUIDFormat(t *testing.T) {
	value, err := newUUID()
	if err != nil {
		t.Fatal(err)
	}
	if !regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`).MatchString(value) {
		t.Fatalf("UUID 格式不合法：%s", value)
	}
}

func TestEvidenceManifestIncludesAPIManualAndCleanup(t *testing.T) {
	fixture := buildFixture(t)
	outputDirectory := filepath.Join(fixture.directory, "mixed-output")
	for _, directory := range []string{"commands", "attachments", "manual-guides"} {
		if err := os.MkdirAll(filepath.Join(outputDirectory, directory), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	apiID := "api-item"
	manualID := "manual-item"
	files := map[string][]byte{
		filepath.Join("commands", apiID+".stdout"):      []byte(`{"status":"failed"}`),
		filepath.Join("commands", apiID+".stderr"):      {},
		filepath.Join("attachments", "support.zip"):     []byte("PK\x03\x04support"),
		filepath.Join("manual-guides", manualID+".txt"): []byte("人工导出说明"),
		"execution-manifest.jsonl":                      []byte("fixture"),
		"operator-note.txt":                             []byte("用户自有文件不得删除"),
	}
	for relative, content := range files {
		if err := os.WriteFile(filepath.Join(outputDirectory, relative), content, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	fixture.artifact.CollectionItems = []artifactItem{
		{
			PlanItemID: apiID, CollectorID: "hci_api_tasks", Executor: "http",
			TimeWindow:     timeWindow{StartTime: "2026-07-29T09:00:00+08:00", EndTime: "2026-07-29T10:00:00+08:00"},
			OutputContract: outputContract{MediaType: "application/json", OutputPath: "tasks/api.json"},
		},
		{
			PlanItemID: manualID, CollectorID: "hci_manual_export", Executor: "manual",
			TimeWindow:     timeWindow{StartTime: "2026-07-29T09:00:00+08:00", EndTime: "2026-07-29T10:00:00+08:00"},
			OutputContract: outputContract{MediaType: "application/zip", OutputPath: "attachments/support.zip"},
		},
	}
	zero := 0
	manifest, err := buildEvidenceManifest(
		packageOptions{outputDir: outputDirectory, source: "node-1", timezone: "Asia/Shanghai", bundleType: "initial"},
		fixture.artifact,
		fixture.caseData,
		[]executionRow{
			{ItemID: apiID, CollectorID: "hci_api_tasks", ExitCode: &zero},
			{ItemID: manualID, CollectorID: "hci_manual_export", Status: "awaiting_manual_attachment"},
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	if manifest.CollectionItems[0].Status != "success" || manifest.CollectionItems[1].Status != "success" {
		t.Fatalf("采集项状态错误：%s/%s", manifest.CollectionItems[0].Status, manifest.CollectionItems[1].Status)
	}
	if manifest.CollectionItems[0].Files[0].Path != "commands/api-item.stdout" || manifest.CollectionItems[1].Files[0].Path != "attachments/support.zip" {
		t.Fatalf("证据文件映射错误：%+v", manifest.CollectionItems)
	}
	removed := cleanupPlaintext(outputDirectory, manifest)
	if removed != 5 {
		t.Fatalf("预期清理 5 个受控文件，实际 %d", removed)
	}
	if content, err := os.ReadFile(filepath.Join(outputDirectory, "operator-note.txt")); err != nil || string(content) != "用户自有文件不得删除" {
		t.Fatal("清理过程误删了用户自有文件")
	}
}

func TestEvidenceFileRejectsSymlinkEscape(t *testing.T) {
	root := t.TempDir()
	outside := filepath.Join(t.TempDir(), "outside.txt")
	if err := os.WriteFile(outside, []byte("secret"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(root, "evidence.txt")); err != nil {
		t.Fatal(err)
	}
	if _, err := fileManifest(root, "evidence.txt", "text/plain"); err == nil {
		t.Fatal("符号链接证据未被拒绝")
	}
}

func TestCollectorFailureReasonClassifiesKnownCompatibilityFailures(t *testing.T) {
	outputDirectory := t.TempDir()
	if err := os.MkdirAll(filepath.Join(outputDirectory, "commands"), 0o700); err != nil {
		t.Fatal(err)
	}
	exitCode := 1
	path := filepath.Join(outputDirectory, "commands", "vm-status.stderr")
	if err := os.WriteFile(path, []byte("错误：当前命令仅支持6.12.0及以上版本"), 0o600); err != nil {
		t.Fatal(err)
	}
	if reason := collectorFailureReason(outputDirectory, "vm-status", &exitCode); reason != "collector_product_version_unsupported" {
		t.Fatalf("未识别产品版本不兼容：%s", reason)
	}
	if err := os.WriteFile(path, []byte("Additional property --formatter is not allowed"), 0o600); err != nil {
		t.Fatal(err)
	}
	if reason := collectorFailureReason(outputDirectory, "vm-status", &exitCode); reason != "collector_argument_unsupported" {
		t.Fatalf("未识别参数不兼容：%s", reason)
	}
}

func TestValidateReadOnlyCommandRejectsMutatingForms(t *testing.T) {
	tests := []struct {
		executable string
		arguments  []string
	}{
		{executable: "ip", arguments: []string{"link", "add", "dummy0", "type", "dummy"}},
		{executable: "ethtool", arguments: []string{"-s", "eth0", "speed", "1000"}},
		{executable: "smartctl", arguments: []string{"-t", "long", "/dev/sda"}},
		{executable: "nvme", arguments: []string{"format", "/dev/nvme0"}},
		{executable: "journalctl", arguments: []string{"--vacuum-time=1d"}},
	}
	for _, test := range tests {
		if err := validateReadOnlyCommand(test.executable, test.arguments); err == nil {
			t.Fatalf("应拒绝写操作：%s %v", test.executable, test.arguments)
		}
	}
}

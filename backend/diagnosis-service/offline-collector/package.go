package main

import (
	"archive/tar"
	"compress/gzip"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

const maxPlaintextArchiveBytes = 512 * 1024 * 1024
const encryptionChunkBytes = 1024 * 1024
const maxFailureInspectionBytes = 64 * 1024

type packageOptions struct {
	outputDir      string
	outputPath     string
	source         string
	timezone       string
	clockOffsetMS  int
	bundleType     string
	parentBundleID string
	cleanup        bool
}

type packageResult struct {
	Path         string
	SizeBytes    int64
	SHA256       string
	RemovedFiles int
}

func collectorFailureReason(outputDir, itemID string, exitCode *int) string {
	reason := "collector_exit_unknown"
	if exitCode != nil {
		reason = fmt.Sprintf("collector_exit_%d", *exitCode)
	}
	path := filepath.Join(outputDir, "commands", itemID+".stderr")
	file, err := os.Open(path)
	if err != nil {
		return reason
	}
	defer file.Close()
	content, err := io.ReadAll(io.LimitReader(file, maxFailureInspectionBytes))
	if err != nil {
		return reason
	}
	message := string(content)
	if strings.Contains(message, "当前命令仅支持") && strings.Contains(message, "版本") {
		return "collector_product_version_unsupported"
	}
	if strings.Contains(message, "Additional property --formatter is not allowed") {
		return "collector_argument_unsupported"
	}
	return reason
}

func fileManifest(root, relative, mediaType string) (evidenceFile, error) {
	path, cleaned, err := resolveEvidenceFile(root, relative)
	if err != nil {
		return evidenceFile{}, err
	}
	hash, size, err := hashFile(path)
	if err != nil {
		return evidenceFile{}, err
	}
	return evidenceFile{
		Path: filepath.ToSlash(cleaned), OriginalName: filepath.Base(cleaned), MediaType: mediaType,
		Sensitivity: "internal", SizeBytes: size, SHA256: hash,
	}, nil
}

func resolveEvidenceFile(root, relative string) (string, string, error) {
	cleaned, err := safeRelativePath(relative)
	if err != nil {
		return "", "", err
	}
	rootPath, err := filepath.EvalSymlinks(root)
	if err != nil {
		return "", "", fmt.Errorf("无法解析输出目录：%w", err)
	}
	candidate := filepath.Join(root, cleaned)
	info, err := os.Lstat(candidate)
	if err != nil {
		return "", "", err
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
		return "", "", fmt.Errorf("证据文件必须是普通文件且不能是符号链接：%s", relative)
	}
	resolved, err := filepath.EvalSymlinks(candidate)
	if err != nil {
		return "", "", err
	}
	relativeToRoot, err := filepath.Rel(rootPath, resolved)
	if err != nil || relativeToRoot == ".." || strings.HasPrefix(relativeToRoot, ".."+string(filepath.Separator)) {
		return "", "", fmt.Errorf("证据文件逃逸输出目录：%s", relative)
	}
	return resolved, cleaned, nil
}

func buildEvidenceManifest(options packageOptions, artifact *artifactManifest, caseData *caseDocument, rows []executionRow) (*evidenceManifest, error) {
	artifactItems := make(map[string]artifactItem, len(artifact.CollectionItems))
	for _, item := range artifact.CollectionItems {
		artifactItems[item.PlanItemID] = item
	}
	items := make([]evidenceCollectionItem, 0, len(rows))
	for _, row := range rows {
		item, ok := artifactItems[row.ItemID]
		if !ok {
			return nil, fmt.Errorf("执行清单包含未知采集项：%s", row.ItemID)
		}
		files := make([]evidenceFile, 0, 2)
		status := "failed"
		var failureReason *string
		if row.Status == "awaiting_manual_attachment" {
			relative, err := safeRelativePath(item.OutputContract.OutputPath)
			if err != nil {
				return nil, err
			}
			if info, statErr := os.Lstat(filepath.Join(options.outputDir, relative)); statErr == nil && info.Mode().IsRegular() {
				file, err := fileManifest(options.outputDir, relative, item.OutputContract.MediaType)
				if err != nil {
					return nil, err
				}
				files = append(files, file)
				status = "success"
			} else {
				status = "skipped_by_user"
			}
		} else {
			for _, stream := range []string{"stdout", "stderr"} {
				relative := filepath.Join("commands", row.ItemID+"."+stream)
				if info, statErr := os.Lstat(filepath.Join(options.outputDir, relative)); statErr == nil && info.Mode().IsRegular() {
					mediaType := "text/plain"
					if stream == "stdout" {
						mediaType = item.OutputContract.MediaType
					}
					file, err := fileManifest(options.outputDir, relative, mediaType)
					if err != nil {
						return nil, err
					}
					files = append(files, file)
				}
			}
			if row.ExitCode != nil && *row.ExitCode == 0 {
				status = "success"
			} else {
				reason := collectorFailureReason(options.outputDir, row.ItemID, row.ExitCode)
				failureReason = &reason
			}
		}
		items = append(items, evidenceCollectionItem{
			CollectorID: row.CollectorID, Status: status, Source: options.source, SourceTimezone: options.timezone,
			ClockOffsetMS: options.clockOffsetMS, TimeCoverage: &evidenceTimeCoverage{Start: item.TimeWindow.StartTime, End: item.TimeWindow.EndTime},
			Files: files, ExitCode: row.ExitCode, FailureReason: failureReason,
		})
	}
	bundleID, err := newUUID()
	if err != nil {
		return nil, err
	}
	var parent *string
	if options.parentBundleID != "" {
		value := options.parentBundleID
		parent = &value
	}
	return &evidenceManifest{
		SchemaVersion: "1.0", BundleID: bundleID, CaseID: caseData.CaseID, SessionID: artifact.SessionID,
		BundleType: options.bundleType, ParentBundleID: parent, SelectedScenario: caseData.SelectedScenario,
		CollectionProfileVersion: artifact.Profile.Version, CollectionPlanID: artifact.CollectionPlanID,
		CollectorArtifactVersion: artifact.SchemaVersion, CollectorArtifactSHA256: artifact.ArtifactSHA256,
		SignatureKeyID: artifact.Signature.KeyID, GeneratedAt: time.Now().Format(time.RFC3339Nano),
		IncidentWindow: caseData.IncidentWindow, Targets: caseData.Targets, CollectionItems: items,
		Encryption: map[string]string{"algorithm": "AES-256-GCM", "key_id": artifact.BundleEncryption.KeyID, "key_wrap_algorithm": "RSA-OAEP-SHA256"},
	}, nil
}

func addTarFile(archive *tar.Writer, path, name string) error {
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return err
	}
	header := &tar.Header{Name: filepath.ToSlash(name), Mode: 0o600, Size: info.Size(), ModTime: time.Unix(0, 0), Typeflag: tar.TypeReg}
	if err := archive.WriteHeader(header); err != nil {
		return err
	}
	_, err = io.Copy(archive, file)
	return err
}

func createArchive(outputDir, casePath string, manifest *evidenceManifest, target string) error {
	file, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	removeTarget := true
	defer func() {
		file.Close()
		if removeTarget {
			_ = os.Remove(target)
		}
	}()
	gzipWriter := gzip.NewWriter(file)
	gzipWriter.Header.ModTime = time.Unix(0, 0)
	tarWriter := tar.NewWriter(gzipWriter)
	if err := addTarFile(tarWriter, casePath, "case.json"); err != nil {
		return err
	}
	manifestBytes, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return err
	}
	header := &tar.Header{Name: "manifest.json", Mode: 0o600, Size: int64(len(manifestBytes) + 1), ModTime: time.Unix(0, 0), Typeflag: tar.TypeReg}
	if err := tarWriter.WriteHeader(header); err != nil {
		return err
	}
	if _, err := tarWriter.Write(append(manifestBytes, '\n')); err != nil {
		return err
	}
	paths := make([]string, 0)
	for _, item := range manifest.CollectionItems {
		for _, evidence := range item.Files {
			paths = append(paths, evidence.Path)
		}
	}
	sort.Strings(paths)
	for _, relative := range paths {
		source, cleaned, err := resolveEvidenceFile(outputDir, relative)
		if err != nil {
			return err
		}
		if err := addTarFile(tarWriter, source, filepath.ToSlash(cleaned)); err != nil {
			return err
		}
	}
	if err := tarWriter.Close(); err != nil {
		return err
	}
	if err := gzipWriter.Close(); err != nil {
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	removeTarget = false
	return nil
}

func encryptArchive(source, target string, metadata *bundleEncryption) error {
	if metadata == nil || metadata.Algorithm != "AES-256-GCM" || metadata.KeyWrapAlgorithm != "RSA-OAEP-SHA256" ||
		(metadata.Format != "HCIEB1" && metadata.Format != "HCIEB2") {
		return fmt.Errorf("平台未配置受支持的证据包加密元数据")
	}
	publicPEM, err := base64.StdEncoding.Strict().DecodeString(metadata.PublicKeyPEMBase64)
	if err != nil {
		return fmt.Errorf("加密公钥不是合法 Base64：%w", err)
	}
	block, _ := pem.Decode(publicPEM)
	if block == nil {
		return fmt.Errorf("加密公钥不是合法 PEM")
	}
	parsed, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		return fmt.Errorf("无法解析 RSA 公钥：%w", err)
	}
	publicKey, ok := parsed.(*rsa.PublicKey)
	if !ok || publicKey.N.BitLen() < 3072 {
		return fmt.Errorf("诊断包加密公钥必须是至少 3072 位 RSA 公钥")
	}
	plainInfo, err := os.Stat(source)
	if err != nil {
		return err
	}
	if plainInfo.Size() > maxPlaintextArchiveBytes {
		return fmt.Errorf("明文归档超过 %d MiB 安全上限", maxPlaintextArchiveBytes/(1024*1024))
	}
	if metadata.Format == "HCIEB2" {
		return encryptArchiveV2(source, target, metadata, publicKey, plainInfo.Size())
	}
	plaintext, err := os.ReadFile(source)
	if err != nil {
		return err
	}
	defer zeroBytes(plaintext)
	dataKey := make([]byte, 32)
	if _, err := rand.Read(dataKey); err != nil {
		return err
	}
	defer zeroBytes(dataKey)
	nonce := make([]byte, 12)
	if _, err := rand.Read(nonce); err != nil {
		return err
	}
	wrapped, err := rsa.EncryptOAEP(sha256.New(), rand.Reader, publicKey, dataKey, nil)
	if err != nil {
		return err
	}
	blockCipher, err := aes.NewCipher(dataKey)
	if err != nil {
		return err
	}
	gcm, err := cipher.NewGCM(blockCipher)
	if err != nil {
		return err
	}
	sealed := gcm.Seal(nil, nonce, plaintext, nil)
	tagIndex := len(sealed) - gcm.Overhead()
	header := map[string]string{
		"algorithm": "AES-256-GCM", "key_wrap_algorithm": "RSA-OAEP-SHA256", "key_id": metadata.KeyID,
		"encrypted_data_key": base64.StdEncoding.EncodeToString(wrapped), "nonce": base64.StdEncoding.EncodeToString(nonce),
		"tag": base64.StdEncoding.EncodeToString(sealed[tagIndex:]),
	}
	headerBytes, err := json.Marshal(header)
	if err != nil {
		return err
	}
	output, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	removeTarget := true
	defer func() {
		output.Close()
		if removeTarget {
			_ = os.Remove(target)
		}
	}()
	if _, err := output.Write([]byte(magic)); err != nil {
		return err
	}
	if err := binary.Write(output, binary.BigEndian, uint32(len(headerBytes))); err != nil {
		return err
	}
	if _, err := output.Write(headerBytes); err != nil {
		return err
	}
	if _, err := output.Write(sealed[:tagIndex]); err != nil {
		return err
	}
	if err := output.Close(); err != nil {
		return err
	}
	removeTarget = false
	return nil
}

func encryptArchiveV2(source, target string, metadata *bundleEncryption, publicKey *rsa.PublicKey, plaintextSize int64) error {
	dataKey := make([]byte, 32)
	if _, err := rand.Read(dataKey); err != nil {
		return err
	}
	defer zeroBytes(dataKey)
	noncePrefix := make([]byte, 4)
	if _, err := rand.Read(noncePrefix); err != nil {
		return err
	}
	wrapped, err := rsa.EncryptOAEP(sha256.New(), rand.Reader, publicKey, dataKey, nil)
	if err != nil {
		return err
	}
	blockCipher, err := aes.NewCipher(dataKey)
	if err != nil {
		return err
	}
	gcm, err := cipher.NewGCM(blockCipher)
	if err != nil {
		return err
	}
	header := map[string]string{
		"algorithm": "AES-256-GCM", "key_wrap_algorithm": "RSA-OAEP-SHA256", "key_id": metadata.KeyID,
		"encrypted_data_key": base64.StdEncoding.EncodeToString(wrapped),
		"nonce_prefix":       base64.StdEncoding.EncodeToString(noncePrefix),
		"chunk_size":         fmt.Sprintf("%d", encryptionChunkBytes), "plaintext_size": fmt.Sprintf("%d", plaintextSize),
	}
	headerBytes, err := json.Marshal(header)
	if err != nil {
		return err
	}
	input, err := os.Open(source)
	if err != nil {
		return err
	}
	defer input.Close()
	output, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	removeTarget := true
	defer func() {
		output.Close()
		if removeTarget {
			_ = os.Remove(target)
		}
	}()
	if _, err := output.Write([]byte(magicV2)); err != nil {
		return err
	}
	if err := binary.Write(output, binary.BigEndian, uint32(len(headerBytes))); err != nil {
		return err
	}
	if _, err := output.Write(headerBytes); err != nil {
		return err
	}
	buffer := make([]byte, encryptionChunkBytes)
	for counter := uint64(0); ; counter++ {
		readBytes, readErr := io.ReadFull(input, buffer)
		if readErr == io.EOF {
			break
		}
		if readErr != nil && readErr != io.ErrUnexpectedEOF {
			return readErr
		}
		nonce := make([]byte, gcm.NonceSize())
		copy(nonce, noncePrefix)
		binary.BigEndian.PutUint64(nonce[4:], counter)
		aad := make([]byte, len(headerBytes)+8)
		copy(aad, headerBytes)
		binary.BigEndian.PutUint64(aad[len(headerBytes):], counter)
		sealed := gcm.Seal(nil, nonce, buffer[:readBytes], aad)
		if err := binary.Write(output, binary.BigEndian, uint32(len(sealed))); err != nil {
			return err
		}
		if _, err := output.Write(sealed); err != nil {
			return err
		}
		zeroBytes(sealed)
		if readErr == io.ErrUnexpectedEOF {
			break
		}
	}
	if err := output.Sync(); err != nil {
		return err
	}
	if err := output.Close(); err != nil {
		return err
	}
	removeTarget = false
	return nil
}

func zeroBytes(value []byte) {
	for index := range value {
		value[index] = 0
	}
}

func cleanupPlaintext(outputDir string, manifest *evidenceManifest) int {
	candidates := map[string]struct{}{filepath.Join(outputDir, "execution-manifest.jsonl"): {}}
	for _, item := range manifest.CollectionItems {
		for _, file := range item.Files {
			if cleaned, err := safeRelativePath(file.Path); err == nil {
				candidates[filepath.Join(outputDir, cleaned)] = struct{}{}
			}
		}
	}
	guides, _ := filepath.Glob(filepath.Join(outputDir, "manual-guides", "*.txt"))
	for _, guide := range guides {
		candidates[guide] = struct{}{}
	}
	removed := 0
	for path := range candidates {
		if info, err := os.Stat(path); err == nil && info.Mode().IsRegular() && os.Remove(path) == nil {
			removed++
		}
	}
	for _, directory := range []string{"commands", "manual-guides", "attachments"} {
		_ = os.Remove(filepath.Join(outputDir, directory))
	}
	return removed
}

func packageEvidence(options packageOptions, bundleDir string, artifact *artifactManifest, caseData *caseDocument, rows []executionRow) (*packageResult, error) {
	fmt.Println("[4/4] 加密打包 …")
	if artifact.BundleEncryption == nil || artifact.BundleEncryption.PublicKeyPEMBase64 == "" || artifact.BundleEncryption.KeyID == "" {
		return nil, fmt.Errorf("平台未为该制品配置证据包加密公钥；请配置密钥后重新生成制品")
	}
	manifest, err := buildEvidenceManifest(options, artifact, caseData, rows)
	if err != nil {
		return nil, err
	}
	temporary, err := os.CreateTemp("", "hci-evidence-*.tar.gz")
	if err != nil {
		return nil, err
	}
	archivePath := temporary.Name()
	if err := temporary.Close(); err != nil {
		return nil, err
	}
	_ = os.Remove(archivePath)
	defer os.Remove(archivePath)
	if err := createArchive(options.outputDir, filepath.Join(bundleDir, "case.json"), manifest, archivePath); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Dir(options.outputPath), 0o700); err != nil {
		return nil, err
	}
	if err := encryptArchive(archivePath, options.outputPath, artifact.BundleEncryption); err != nil {
		return nil, err
	}
	hash, size, err := hashFile(options.outputPath)
	if err != nil {
		return nil, err
	}
	removed := 0
	if options.cleanup {
		removed = cleanupPlaintext(options.outputDir, manifest)
	}
	return &packageResult{Path: options.outputPath, SizeBytes: size, SHA256: hash, RemovedFiles: removed}, nil
}

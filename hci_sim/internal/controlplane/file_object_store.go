package controlplane

// FileBundleObjectStore 是开发/单副本部署使用的持久化对象存储适配器。
// 它用内容寻址目录和原子 rename 保证重启后 Draft/Published 对象仍可读取；
// 多副本生产环境应替换为 OCI/S3/WORM 实现，并保留同样的接口语义。

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

type FileBundleObjectStore struct {
	root string
}

func NewFileBundleObjectStore(root string) (*FileBundleObjectStore, error) {
	root = strings.TrimSpace(root)
	if root == "" {
		return nil, errors.New("Bundle 对象目录不能为空")
	}
	for _, dir := range []string{filepath.Join(root, "stage"), filepath.Join(root, "bundles")} {
		if err := os.MkdirAll(dir, 0o750); err != nil {
			return nil, fmt.Errorf("创建 Bundle 对象目录失败: %w", err)
		}
	}
	return &FileBundleObjectStore{root: root}, nil
}

func (s *FileBundleObjectStore) Prepare(raw []byte, expectedDigest string) (ObjectRef, error) {
	if len(raw) == 0 || digestBytes(raw) != expectedDigest {
		return ObjectRef{}, errors.New("bundle_prepare_integrity_failed")
	}
	stageDir := filepath.Join(s.root, "stage")
	file, err := os.CreateTemp(stageDir, strings.TrimPrefix(expectedDigest, "sha256:")+"-")
	if err != nil {
		return ObjectRef{}, err
	}
	key := "stage/" + filepath.Base(file.Name())
	committed := false
	defer func() {
		_ = file.Close()
		if !committed {
			_ = os.Remove(file.Name())
		}
	}()
	if err := file.Chmod(0o640); err != nil {
		return ObjectRef{}, err
	}
	if _, err := file.Write(raw); err != nil {
		return ObjectRef{}, err
	}
	if err := file.Sync(); err != nil {
		return ObjectRef{}, err
	}
	if err := file.Close(); err != nil {
		return ObjectRef{}, err
	}
	committed = true
	return ObjectRef{Key: key, Digest: expectedDigest, Size: int64(len(raw))}, nil
}

func (s *FileBundleObjectStore) Verify(ref ObjectRef) error {
	raw, err := s.Read(ref)
	if err != nil || ref.Size != int64(len(raw)) || ref.Digest != digestBytes(raw) {
		return errors.New("bundle_verify_integrity_failed")
	}
	return nil
}

func (s *FileBundleObjectStore) Commit(ref ObjectRef) (ObjectRef, error) {
	if err := s.Verify(ref); err != nil {
		return ObjectRef{}, errors.New("bundle_commit_integrity_failed")
	}
	source, err := s.pathFor(ref.Key)
	if err != nil {
		return ObjectRef{}, err
	}
	key := "bundles/" + ref.Digest
	target, err := s.pathFor(key)
	if err != nil {
		return ObjectRef{}, err
	}
	if existing, readErr := os.ReadFile(target); readErr == nil {
		raw, sourceErr := os.ReadFile(source)
		if sourceErr != nil || string(existing) != string(raw) {
			return ObjectRef{}, errors.New("bundle_immutable_conflict")
		}
		_ = os.Remove(source)
		return ObjectRef{Key: key, Digest: ref.Digest, Size: ref.Size}, nil
	} else if !errors.Is(readErr, os.ErrNotExist) {
		return ObjectRef{}, fmt.Errorf("读取已发布 Bundle 对象失败: %w", readErr)
	}
	if err := os.Rename(source, target); err != nil {
		return ObjectRef{}, fmt.Errorf("提交 Bundle 对象失败: %w", err)
	}
	return ObjectRef{Key: key, Digest: ref.Digest, Size: ref.Size}, nil
}

func (s *FileBundleObjectStore) Read(ref ObjectRef) ([]byte, error) {
	path, err := s.pathFor(ref.Key)
	if err != nil {
		return nil, err
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, errors.New("bundle_read_integrity_failed")
	}
	if ref.Size != int64(len(raw)) || ref.Digest != digestBytes(raw) {
		return nil, errors.New("bundle_read_integrity_failed")
	}
	return raw, nil
}

func (s *FileBundleObjectStore) ReadPublished(ref ObjectRef) ([]byte, error) {
	if !strings.HasPrefix(ref.Key, "bundles/") {
		return nil, errors.New("bundle_not_published")
	}
	return s.Read(ref)
}

func (s *FileBundleObjectStore) Abort(ref ObjectRef) {
	if strings.HasPrefix(ref.Key, "stage/") {
		if path, err := s.pathFor(ref.Key); err == nil {
			_ = os.Remove(path)
		}
	}
}

func (s *FileBundleObjectStore) pathFor(key string) (string, error) {
	allowedPrefix := strings.HasPrefix(key, "stage/") || strings.HasPrefix(key, "bundles/")
	if !allowedPrefix || filepath.IsAbs(key) || strings.Contains(key, "..") || filepath.Clean(key) != key {
		return "", errors.New("bundle_object_key_invalid")
	}
	return filepath.Join(s.root, key), nil
}

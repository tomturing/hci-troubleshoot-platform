package fixture

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// BundleInfo 是运行时公开的不可变 Bundle 身份，不包含路由输出内容。
type BundleInfo struct {
	SupportID    string `json:"support_id"`
	KBDRevision  int    `json:"kbd_revision"`
	BundleDigest string `json:"bundle_digest"`
}

// BundlePool 是启动时冻结的多 KBD 路由表。构造完成后只允许并发读取。
type BundlePool struct {
	routers map[string]*Router
}

// LoadBundlePoolDir 从只读目录加载全部 JSON manifest，任一文件无效即失败关闭。
func LoadBundlePoolDir(dir string) (*BundlePool, error) {
	dir = strings.TrimSpace(dir)
	if dir == "" {
		return nil, errors.New("fixture bundle 目录不能为空")
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("读取 fixture bundle 目录失败: %w", err)
	}
	paths := make([]string, 0, len(entries))
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), "..") || filepath.Ext(entry.Name()) != ".json" {
			continue
		}
		paths = append(paths, filepath.Join(dir, entry.Name()))
	}
	sort.Strings(paths)
	if len(paths) == 0 {
		return nil, errors.New("fixture bundle 目录不包含 JSON manifest")
	}
	return LoadBundlePool(paths...)
}

// LoadBundlePool 加载显式 manifest 集合；同一 support_id 不允许出现多个 digest。
func LoadBundlePool(paths ...string) (*BundlePool, error) {
	pool := &BundlePool{routers: make(map[string]*Router, len(paths))}
	for _, path := range paths {
		router, err := Load(path)
		if err != nil {
			return nil, fmt.Errorf("加载 fixture bundle %s 失败: %w", filepath.Base(path), err)
		}
		supportID := router.KBD().SupportID
		if existing := pool.routers[supportID]; existing != nil {
			return nil, fmt.Errorf("fixture support_id %s 存在重复 Bundle: %s / %s", supportID, existing.BundleDigest(), router.BundleDigest())
		}
		pool.routers[supportID] = router
	}
	if len(pool.routers) == 0 {
		return nil, errors.New("fixture BundlePool 不能为空")
	}
	return pool, nil
}

// NewBundlePool 用于测试和单 manifest 兼容入口，同样拒绝重复 support_id。
func NewBundlePool(routers ...*Router) (*BundlePool, error) {
	pool := &BundlePool{routers: make(map[string]*Router, len(routers))}
	for _, router := range routers {
		if router == nil {
			return nil, errors.New("fixture Router 不能为空")
		}
		supportID := router.KBD().SupportID
		if pool.routers[supportID] != nil {
			return nil, fmt.Errorf("fixture support_id %s 重复", supportID)
		}
		pool.routers[supportID] = router
	}
	if len(pool.routers) == 0 {
		return nil, errors.New("fixture BundlePool 不能为空")
	}
	return pool, nil
}

func (p *BundlePool) Get(supportID string) *Router {
	if p == nil {
		return nil
	}
	return p.routers[strings.TrimSpace(supportID)]
}

func (p *BundlePool) Size() int {
	if p == nil {
		return 0
	}
	return len(p.routers)
}

func (p *BundlePool) SupportIDs() []string {
	ids := make([]string, 0, p.Size())
	for supportID := range p.routers {
		ids = append(ids, supportID)
	}
	sort.Strings(ids)
	return ids
}

func (p *BundlePool) Bundles() []BundleInfo {
	bundles := make([]BundleInfo, 0, p.Size())
	for _, supportID := range p.SupportIDs() {
		router := p.routers[supportID]
		bundles = append(bundles, BundleInfo{SupportID: supportID, KBDRevision: router.KBD().Revision, BundleDigest: router.BundleDigest()})
	}
	return bundles
}

func (p *BundlePool) MaxOutputLimit() int {
	limit := 0
	for _, router := range p.routers {
		if router.OutputLimit() > limit {
			limit = router.OutputLimit()
		}
	}
	return limit
}

// ValidateRequired 校验部署声明与实际加载集合完全一致，禁止缺失或夹带 Bundle。
func (p *BundlePool) ValidateRequired(raw string) error {
	var required map[string]string
	decoder := json.NewDecoder(strings.NewReader(strings.TrimSpace(raw)))
	if err := decoder.Decode(&required); err != nil || len(required) == 0 {
		return errors.New("HCI_SIM_REQUIRED_BUNDLES 必须是非空 support_id→digest JSON 对象")
	}
	if err := ensureEOF(decoder); err != nil {
		return fmt.Errorf("HCI_SIM_REQUIRED_BUNDLES 无效: %w", err)
	}
	if len(required) != p.Size() {
		return fmt.Errorf("必需 Bundle 数量不匹配: required=%d loaded=%d", len(required), p.Size())
	}
	for supportID, digest := range required {
		router := p.Get(supportID)
		if router == nil {
			return fmt.Errorf("必需 Bundle %s 未加载", supportID)
		}
		if router.BundleDigest() != digest {
			return fmt.Errorf("Bundle %s digest 漂移: required=%s loaded=%s", supportID, digest, router.BundleDigest())
		}
	}
	return nil
}

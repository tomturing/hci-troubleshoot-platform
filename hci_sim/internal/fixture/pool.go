package fixture

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
)

// BundleInfo 是运行时公开的不可变 Bundle 身份，不包含路由输出内容。
type BundleInfo struct {
	SupportID    string `json:"support_id"`
	KBDRevision  int    `json:"kbd_revision"`
	BundleDigest string `json:"bundle_digest"`
}

// BundlePool 是多 KBD 路由表。
// active 保存每个 support_id 当前新建 TestRun 使用的 digest；routersByDigest
// 保留已加载的历史版本，使热切换后仍在有效期内的旧 Lease 可以继续执行。
type BundlePool struct {
	mu              sync.RWMutex
	active          map[string]string
	routersByDigest map[string]*Router
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
	pool := &BundlePool{active: make(map[string]string, len(paths)), routersByDigest: make(map[string]*Router, len(paths))}
	for _, path := range paths {
		router, err := Load(path)
		if err != nil {
			return nil, fmt.Errorf("加载 fixture bundle %s 失败: %w", filepath.Base(path), err)
		}
		supportID := router.KBD().SupportID
		if existing := pool.getActiveLocked(supportID); existing != nil {
			return nil, fmt.Errorf("fixture support_id %s 存在重复 Bundle: %s / %s", supportID, existing.BundleDigest(), router.BundleDigest())
		}
		pool.routersByDigest[router.BundleDigest()] = router
		pool.active[supportID] = router.BundleDigest()
	}
	if len(pool.active) == 0 {
		return nil, errors.New("fixture BundlePool 不能为空")
	}
	return pool, nil
}

// NewBundlePool 用于测试和单 manifest 兼容入口，同样拒绝重复 support_id。
func NewBundlePool(routers ...*Router) (*BundlePool, error) {
	pool := &BundlePool{active: make(map[string]string, len(routers)), routersByDigest: make(map[string]*Router, len(routers))}
	pool.mu.Lock()
	defer pool.mu.Unlock()
	for _, router := range routers {
		if router == nil {
			return nil, errors.New("fixture Router 不能为空")
		}
		supportID := router.KBD().SupportID
		if pool.getActiveLocked(supportID) != nil {
			return nil, fmt.Errorf("fixture support_id %s 重复", supportID)
		}
		pool.routersByDigest[router.BundleDigest()] = router
		pool.active[supportID] = router.BundleDigest()
	}
	if len(pool.active) == 0 {
		return nil, errors.New("fixture BundlePool 不能为空")
	}
	return pool, nil
}

func (p *BundlePool) Get(supportID string) *Router {
	if p == nil {
		return nil
	}
	p.mu.RLock()
	defer p.mu.RUnlock()
	return p.getActiveLocked(strings.TrimSpace(supportID))
}

// GetByDigest 返回 Lease 绑定的不可变 Router，而不是当前 active Router。
func (p *BundlePool) GetByDigest(digest string) *Router {
	if p == nil {
		return nil
	}
	p.mu.RLock()
	defer p.mu.RUnlock()
	return p.routersByDigest[strings.TrimSpace(digest)]
}

// ActiveVariant 返回某个 active Bundle 的可用变体。preferred 不存在时不回退到
// 另一个 Bundle，只在同一个 Bundle 内选择其确定性的默认变体。
func (p *BundlePool) ActiveVariant(supportID, preferred string) string {
	router := p.Get(supportID)
	if router == nil {
		return ""
	}
	if router.HasVariant(preferred) {
		return preferred
	}
	return router.DefaultVariant()
}

// Activate 原子切换一个 support_id 的 active digest，并保留旧 Router。
// Router 在进入池前已经完成 fixture.Parse 完整性校验，切换只交换指针。
func (p *BundlePool) Activate(router *Router) (*Router, error) {
	if p == nil || router == nil {
		return nil, errors.New("fixture BundlePool 或 Router 为空")
	}
	supportID, digest := router.KBD().SupportID, router.BundleDigest()
	if supportID == "" || digest == "" {
		return nil, errors.New("fixture BundlePool 激活缺少 support_id 或 digest")
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	previous := p.getActiveLocked(supportID)
	if existing := p.routersByDigest[digest]; existing != nil && existing.ManifestHash() != router.ManifestHash() {
		return nil, errors.New("fixture BundlePool 检测到 digest 内容冲突")
	}
	p.routersByDigest[digest] = router
	p.active[supportID] = digest
	return previous, nil
}

func (p *BundlePool) Size() int {
	if p == nil {
		return 0
	}
	p.mu.RLock()
	defer p.mu.RUnlock()
	return len(p.active)
}

func (p *BundlePool) SupportIDs() []string {
	p.mu.RLock()
	defer p.mu.RUnlock()
	ids := make([]string, 0, len(p.active))
	for supportID := range p.active {
		ids = append(ids, supportID)
	}
	sort.Strings(ids)
	return ids
}

func (p *BundlePool) Bundles() []BundleInfo {
	if p == nil {
		return nil
	}
	p.mu.RLock()
	defer p.mu.RUnlock()
	supportIDs := make([]string, 0, len(p.active))
	for supportID := range p.active {
		supportIDs = append(supportIDs, supportID)
	}
	sort.Strings(supportIDs)
	bundles := make([]BundleInfo, 0, len(supportIDs))
	for _, supportID := range supportIDs {
		router := p.getActiveLocked(supportID)
		bundles = append(bundles, BundleInfo{SupportID: supportID, KBDRevision: router.KBD().Revision, BundleDigest: router.BundleDigest()})
	}
	return bundles
}

func (p *BundlePool) MaxOutputLimit() int {
	if p == nil {
		return 0
	}
	p.mu.RLock()
	defer p.mu.RUnlock()
	limit := 0
	for _, digest := range p.active {
		router := p.routersByDigest[digest]
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

func (p *BundlePool) getActiveLocked(supportID string) *Router {
	digest := p.active[supportID]
	if digest == "" {
		return nil
	}
	return p.routersByDigest[digest]
}

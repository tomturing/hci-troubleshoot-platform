package reconcile

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// state.go —— 已应用状态的两个语义（收口设计 §五）。
//
//	节点事实 NodeFact    现场探测得到的远端真实状态，**不持久化**
//	桥侧账本 AppliedState 桥自己记录的"上次成功同步了什么"，落盘
//
// 账本不是幂等的唯一依据：节点可能被第三方改动（acli 重装清空插件目录）。
// Skip 判定必须同时满足"账本对齐 且 节点事实对齐"，只信账本会在节点被
// 清空后永久 Skip（B4）。冲突裁决顺序：节点事实 > 桥侧账本 > 云端 spec。

// NodeFact 是通过 remote/probe 探测获得的节点当前事实。
type NodeFact struct {
	FileExists bool
	FileSize   int64
	FileDigest string
	Version    string
	BuildTime  string
	// Probed 为假表示探测没跑成功（不可达/命令缺失）。此时上面所有字段
	// 一律不可信——"探测失败"与"探测到缺失"是两种结论，混为一谈会让
	// 重试逻辑走错分支，也会把"没判定"伪装成"已是最新"（B6）。
	Probed bool
	// Algo 是摘要档位：sha256 | md5 | size_only。低于 sha256 时调用方
	// 必须知道"这次比对用的是弱基线"。
	Algo string
	// Files 是多文件 descriptor 的逐路径事实；单文件链路只填上面几个字段即可。
	Files map[string]FileFact
}

// FileFact 是单个路径的探测事实。
type FileFact struct {
	Exists bool
	Size   int64
	Digest string
	Algo   string
}

// AppliedState 是桥侧持久化的"上次成功同步"记录。
type AppliedState struct {
	Kind        string     `json:"kind"`
	NodeIP      string     `json:"node_ip"`
	DesiredHash string     `json:"desired_hash"`
	AppliedAt   time.Time  `json:"applied_at"`
	Evidence    []Evidence `json:"evidence,omitempty"`
}

// DefaultStateDir 返回账本目录。
//
// 为什么不是 ~/.bridge：cluster 模式下那是容器内 HOME，Pod 重建即丢，
// 账本退化成内存（B9）。统一落 PVC 内的 /var/lib/terminal-bridge；
// 桌面模式由 HCI_BRIDGE_LOG_DIR 同源决定，不写死 HOME。
func DefaultStateDir() string {
	if env := strings.TrimSpace(os.Getenv("HCI_BRIDGE_LOG_DIR")); env != "" {
		return filepath.Join(env, "reconcile", "state")
	}
	return "/var/lib/terminal-bridge/reconcile/state"
}

// Store 是账本的落盘实现：临时文件 + rename，读者永远看不到半截 JSON。
type Store struct {
	dir string
	mu  sync.Mutex
}

// NewStore 构造账本存储；dir 为空时用 DefaultStateDir()。
func NewStore(dir string) *Store {
	if strings.TrimSpace(dir) == "" {
		dir = DefaultStateDir()
	}
	return &Store{dir: dir}
}

// Dir 返回账本目录（日志/排障用）。
func (s *Store) Dir() string { return s.dir }

func stateFileName(kind, nodeIP string) string {
	// kind 与 nodeIP 都会进文件名：nodeIP 允许 IPv6 的冒号，统一替换成下划线，
	// 避免在 Windows 桌面模式下产出非法文件名。
	replacer := strings.NewReplacer("/", "_", "\\", "_", ":", "_", "..", "_")
	return replacer.Replace(kind) + "-" + replacer.Replace(nodeIP) + ".json"
}

// Load 加载桥侧账本。不存在返回 (nil, nil)——"没记录"不是错误，
// 把它当错误会让首次同步永远失败。
func (s *Store) Load(kind, nodeIP string) (*AppliedState, error) {
	if s == nil {
		return nil, nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	raw, err := os.ReadFile(filepath.Join(s.dir, stateFileName(kind, nodeIP)))
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var st AppliedState
	if err := json.Unmarshal(raw, &st); err != nil {
		// 账本坏了不能当成"没记录"静默重写：那会把"上次同步了什么"抹掉。
		// 返回错误让调用方留痕，并按节点事实优先继续（不 Skip）。
		return nil, fmt.Errorf("账本解析失败：%w", err)
	}
	return &st, nil
}

// Save 持久化桥侧账本（临时文件 + rename）。
func (s *Store) Save(st *AppliedState) error {
	if s == nil || st == nil {
		return nil
	}
	if st.Kind == "" || st.NodeIP == "" {
		return fmt.Errorf("账本缺少 kind/node_ip，拒绝落盘")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := os.MkdirAll(s.dir, 0o700); err != nil {
		return err
	}
	raw, err := json.Marshal(st)
	if err != nil {
		return err
	}
	target := filepath.Join(s.dir, stateFileName(st.Kind, st.NodeIP))
	tmp, err := os.CreateTemp(s.dir, ".state-*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if _, err := tmp.Write(raw); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpName, target)
}

// Delete 删除账本条目（还原完成后清理意图用）。不存在视为成功。
func (s *Store) Delete(kind, nodeIP string) error {
	if s == nil {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	err := os.Remove(filepath.Join(s.dir, stateFileName(kind, nodeIP)))
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// List 列出全部账本条目。watcher 重启后凭它 + 自描述备份恢复现场，
// 而不是复活一个内存 goroutine（§3.2 抗崩溃三件套）。
func (s *Store) List() ([]AppliedState, error) {
	if s == nil {
		return nil, nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	entries, err := os.ReadDir(s.dir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	out := make([]AppliedState, 0, len(entries))
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		raw, err := os.ReadFile(filepath.Join(s.dir, entry.Name()))
		if err != nil {
			continue
		}
		var st AppliedState
		if err := json.Unmarshal(raw, &st); err != nil {
			continue
		}
		out = append(out, st)
	}
	return out, nil
}

var defaultStore = struct {
	once sync.Once
	st   *Store
}{}

func sharedStore() *Store {
	defaultStore.once.Do(func() { defaultStore.st = NewStore("") })
	return defaultStore.st
}

// Load 用默认账本目录加载（设计 §五 的包级签名）。
func Load(kind, nodeIP string) (*AppliedState, error) { return sharedStore().Load(kind, nodeIP) }

// Save 用默认账本目录持久化。
func Save(state *AppliedState) error { return sharedStore().Save(state) }

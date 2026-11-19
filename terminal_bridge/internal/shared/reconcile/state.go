package reconcile

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
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

// RestoreIntent 是"成功后要还原"的持久意图（设计 §3.2）。
//
// 为什么必须落盘而不是内存尾钩子：spc 的 OnSuccessRestore 等的是 task_terminal——
// 小时级、且跨越 bridge 进程重启。进程一死，内存 goroutine 就没了，
// 平台清单会永久留在节点上污染下一次采集。意图落盘后，watcher 重启可以从
// 账本 + 自描述备份名（.platform.<taskID>.bak）恢复现场，而不是复活一个协程。
//
// 字段只描述"去哪儿还原什么"，不含任何执行状态：执行状态属于内存里的
// 一次性动作，把它写进意图会让重启后的恢复误判"已经还原过了"。
type RestoreIntent struct {
	Kind      string            `json:"kind"`
	TaskKey   string            `json:"task_key"`
	WaitFor   string            `json:"wait_for"`
	NodeIPs   []string          `json:"node_ips"`
	LocalIPs  map[string]bool   `json:"local_ips,omitempty"`
	Dirs      map[string]string `json:"dirs,omitempty"`
	Slots     []string          `json:"slots,omitempty"`
	CreatedAt time.Time         `json:"created_at"`
	Reason    string            `json:"reason,omitempty"`
}

// intentsDir 是意图子目录：与账本分开放，List() 才不会把意图当账本读
// （两者 JSON 形状不同，混在一起会解析出 Kind 为空的假账本条目）。
func (s *Store) intentsDir() string { return filepath.Join(s.dir, "intents") }

func intentFileName(kind, taskKey string) string {
	replacer := strings.NewReplacer("/", "_", "\\", "_", ":", "_", "..", "_")
	return replacer.Replace(kind) + "-" + replacer.Replace(taskKey) + ".json"
}

// SaveIntent 持久化还原意图（临时文件 + rename，与账本同一套原子写法）。
func (s *Store) SaveIntent(intent RestoreIntent) error {
	if s == nil {
		return nil
	}
	if intent.Kind == "" || intent.TaskKey == "" {
		return fmt.Errorf("还原意图缺少 kind/task_key，拒绝落盘")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	dir := s.intentsDir()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	raw, err := json.Marshal(intent)
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".intent-*.tmp")
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
	return os.Rename(tmpName, filepath.Join(dir, intentFileName(intent.Kind, intent.TaskKey)))
}

// LoadIntents 列出全部未消费的还原意图（按创建时间升序，先写的先还原）。
// 单条损坏只跳过该条：一份坏意图不该让其余任务的现场永远没人收尾。
func (s *Store) LoadIntents() ([]RestoreIntent, error) {
	if s == nil {
		return nil, nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	entries, err := os.ReadDir(s.intentsDir())
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	out := make([]RestoreIntent, 0, len(entries))
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		raw, err := os.ReadFile(filepath.Join(s.intentsDir(), entry.Name()))
		if err != nil {
			continue
		}
		var intent RestoreIntent
		if err := json.Unmarshal(raw, &intent); err != nil || intent.TaskKey == "" {
			continue
		}
		out = append(out, intent)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt.Before(out[j].CreatedAt) })
	return out, nil
}

// DeleteIntent 消费掉一条意图（还原完成后调用）。不存在视为成功——幂等重放的关键。
func (s *Store) DeleteIntent(kind, taskKey string) error {
	if s == nil {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	err := os.Remove(filepath.Join(s.intentsDir(), intentFileName(kind, taskKey)))
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// SaveIntent 用默认账本目录持久化还原意图。
func SaveIntent(intent RestoreIntent) error { return sharedStore().SaveIntent(intent) }

// LoadIntents 用默认账本目录列出还原意图（watcher 重启恢复入口）。
func LoadIntents() ([]RestoreIntent, error) { return sharedStore().LoadIntents() }

// DeleteIntent 用默认账本目录消费还原意图。
func DeleteIntent(kind, taskKey string) error { return sharedStore().DeleteIntent(kind, taskKey) }

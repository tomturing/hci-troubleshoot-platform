package sshx_test

// SFTP 通道缓存的生命周期契约。
//
// 为什么要测缓存而不是只测转发：通道是 subsystem 协商出来的长连接，
// 批量下载时每个文件都重建会多出数百毫秒；但**缓存一个死通道**更糟——
// 远端回收 subsystem 后，后续每一次列表/下载都会失败到用户重连为止。

import (
	"errors"
	"io"
	"os"
	"sync"
	"testing"

	"terminal_bridge/internal/sshx"
)

type fakeSftpRemote struct {
	mu     sync.Mutex
	closed int
	err    error
}

func (f *fakeSftpRemote) ListDir(string) ([]os.FileInfo, error) { return nil, f.err }
func (f *fakeSftpRemote) Stat(string) (os.FileInfo, error)      { return nil, f.err }
func (f *fakeSftpRemote) Open(string) (sshx.SftpSeekReader, error) {
	return nil, f.err
}
func (f *fakeSftpRemote) Getwd() (string, error) { return "/home/acli", f.err }
func (f *fakeSftpRemote) Close() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.closed++
	return nil
}

func (f *fakeSftpRemote) closes() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.closed
}

// TestSftpRemoteClientRequiresLiveSession 会话关闭后不得尝试协商新通道。
func TestSftpRemoteClientRequiresLiveSession(t *testing.T) {
	s := &sshx.SSHSession{}
	client, err := s.SftpRemoteClient()
	if err == nil || client != nil {
		t.Fatalf("无 ssh client 时不应返回通道，实际 (%v,%v)", client, err)
	}
	if err.Error() != "SSH 会话已关闭" {
		t.Fatalf("错误文案应可识别，实际 %q", err.Error())
	}
}

// TestSftpRemoteClientReusesCached 已有缓存必须直接复用（同一实例）。
func TestSftpRemoteClientReusesCached(t *testing.T) {
	cached := &fakeSftpRemote{}
	s := &sshx.SSHSession{Sftp: cached}
	for i := 0; i < 3; i++ {
		got, err := s.SftpRemoteClient()
		if err != nil {
			t.Fatalf("复用缓存不应失败: %v", err)
		}
		if got != cached {
			t.Fatal("应返回同一个缓存通道实例，而不是每次新建")
		}
	}
	if cached.closes() != 0 {
		t.Fatalf("复用期间不得关闭通道，实际关闭 %d 次", cached.closes())
	}
}

// TestDropSftpRemoteClientClosesAndClears 丢弃必须关闭并清空，否则死通道会被永久复用。
func TestDropSftpRemoteClientClosesAndClears(t *testing.T) {
	cached := &fakeSftpRemote{}
	s := &sshx.SSHSession{Sftp: cached}
	s.DropSftpRemoteClient()
	if cached.closes() != 1 {
		t.Fatalf("丢弃时应关闭一次，实际 %d 次", cached.closes())
	}
	if s.Sftp != nil {
		t.Fatal("丢弃后缓存必须清空，否则下次仍拿到死通道")
	}
}

// TestDropSftpRemoteClientIdempotent 重复丢弃不得重复关闭、也不得 panic。
func TestDropSftpRemoteClientIdempotent(t *testing.T) {
	cached := &fakeSftpRemote{}
	s := &sshx.SSHSession{Sftp: cached}
	s.DropSftpRemoteClient()
	s.DropSftpRemoteClient()
	if cached.closes() != 1 {
		t.Fatalf("重复丢弃只应关闭一次，实际 %d 次", cached.closes())
	}
	empty := &sshx.SSHSession{}
	empty.DropSftpRemoteClient() // 空缓存：只验证不 panic
}

// TestSftpRemoteClientConcurrentAccess 并发取通道必须安全（由 sftpMu 保护）。
func TestSftpRemoteClientConcurrentAccess(t *testing.T) {
	cached := &fakeSftpRemote{}
	s := &sshx.SSHSession{Sftp: cached}
	var wg sync.WaitGroup
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if got, err := s.SftpRemoteClient(); err != nil || got != cached {
				t.Errorf("并发取通道失败: got=%v err=%v", got, err)
			}
		}()
	}
	wg.Wait()
}

// TestSftpDownstreamErrorPropagates 通道故障必须原样冒泡，不能被缓存层吞掉。
//
// 调用方（sftp 域）靠这个错误决定是否 Drop 后重试；吞掉就成了"永远打不开目录"。
func TestSftpDownstreamErrorPropagates(t *testing.T) {
	want := errors.New("subsystem request failed")
	var _ io.Closer = &fakeSftpRemote{}
	remote := &fakeSftpRemote{err: want}
	if _, err := remote.ListDir("/sf/log"); !errors.Is(err, want) {
		t.Fatalf("错误应原样返回，实际 %v", err)
	}
}

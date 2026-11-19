package sshx

// sftp_channel.go —— 会话上的懒加载只读 SFTP 通道（自 sftp.go 随 SSHSession 迁入）。
// 接口按消费方归属放在 sshx：sftp 域的实现器满足这里的抽象。

import (
	"fmt"
	"io"
	"os"

	"github.com/pkg/sftp"
)

// ── 远端只读能力抽象 ───────────────────────────────────────────────────────────
//
// 只暴露"读目录 / 取属性 / 打开文件"三件事：SFTP 面板是下载器，不是远程文件管理器，
// 不给删除、重命名、写权限留任何入口。抽象出来同时让单测能注入假远端。

type SftpSeekReader interface {
	io.Reader
	io.Closer
	Seek(offset int64, whence int) (int64, error)
}

type SftpRemote interface {
	ListDir(remotePath string) ([]os.FileInfo, error)
	Stat(remotePath string) (os.FileInfo, error)
	Open(remotePath string) (SftpSeekReader, error)
	// Getwd 取远端登录用户的初始工作目录：面板首次打开时不带路径，
	// 由服务端决定落到 home，而不是让前端猜 /root 还是 /home/xxx。
	Getwd() (string, error)
	Close() error
}

type sftpClientRemote struct {
	client *sftp.Client
}

func (r *sftpClientRemote) ListDir(remotePath string) ([]os.FileInfo, error) {
	return r.client.ReadDir(remotePath)
}

func (r *sftpClientRemote) Stat(remotePath string) (os.FileInfo, error) {
	return r.client.Stat(remotePath)
}

func (r *sftpClientRemote) Open(remotePath string) (SftpSeekReader, error) {
	file, err := r.client.Open(remotePath)
	if err != nil {
		return nil, err
	}
	return file, nil
}

func (r *sftpClientRemote) Getwd() (string, error) { return r.client.Getwd() }

func (r *sftpClientRemote) Close() error { return r.client.Close() }

// sftpRemoteClient 懒加载并缓存 SFTP 通道。
//
// 缓存而非每次新建：SFTP 建链要做 subsystem 协商，批量下载时每个文件都建一次会让
// 每次下载都多出数百毫秒；缓存在会话上，会话关闭时一并回收。
func (s *SSHSession) SftpRemoteClient() (SftpRemote, error) {
	s.sftpMu.Lock()
	defer s.sftpMu.Unlock()
	if s.Sftp != nil {
		return s.Sftp, nil
	}
	if s.Client == nil {
		return nil, fmt.Errorf("SSH 会话已关闭")
	}
	client, err := sftp.NewClient(s.Client)
	if err != nil {
		return nil, err
	}
	s.Sftp = &sftpClientRemote{client: client}
	return s.Sftp, nil
}

// dropSftpRemoteClient 丢弃缓存的 SFTP 通道。
//
// 通道可能因远端 sshd 回收 subsystem、网络抖动而失效。若继续缓存一个死通道，
// 后续每一次列表/下载都会失败到用户重连 SSH 为止。丢弃后下次请求重新协商，
// 代价只是一次 subsystem 握手。
func (s *SSHSession) DropSftpRemoteClient() {
	s.sftpMu.Lock()
	defer s.sftpMu.Unlock()
	if s.Sftp != nil {
		_ = s.Sftp.Close()
		s.Sftp = nil
	}
}

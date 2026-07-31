package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/pem"
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"strings"

	"golang.org/x/crypto/ssh"
)

// serverOptions 聚合 hci-sim 启动参数；startSimServer 可被 main 与集成测试共用。
type serverOptions struct {
	Listen      string
	HostKeyPath string
	Fixtures    string
	User        string
	Password    string
	AcceptAny   bool
	LeaseSecret string
}

// startSimServer 启动 SSH 仿真服务并返回监听器（测试可借此拿到实际监听地址并随时关闭）。
func startSimServer(opts serverOptions) (net.Listener, error) {
	signer, err := loadOrGenerateHostKey(opts.HostKeyPath)
	if err != nil {
		return nil, fmt.Errorf("host key: %w", err)
	}
	fixtures, err := loadFixtures(opts.Fixtures)
	if err != nil {
		return nil, fmt.Errorf("fixtures: %w", err)
	}
	log.Printf("[hci-sim] 已加载 %d 个可路由 fixture", len(fixtures))

	sshConfig := &ssh.ServerConfig{
		PasswordCallback: func(conn ssh.ConnMetadata, pass []byte) (*ssh.Permissions, error) {
			if opts.AcceptAny {
				return &ssh.Permissions{}, nil
			}
			provided := string(pass)
			// 兼容 terminal_bridge 的密码后缀行为：<password>sangfornetwork
			candidate := strings.TrimSuffix(provided, "sangfornetwork")
			if conn.User() == opts.User && (candidate == opts.Password || provided == opts.Password) {
				return &ssh.Permissions{}, nil
			}
			return nil, fmt.Errorf("permission denied")
		},
	}
	sshConfig.AddHostKey(signer)

	handler := &Handler{router: NewFixtureRouter(fixtures), leaseSecret: opts.LeaseSecret}

	ln, err := net.Listen("tcp", opts.Listen)
	if err != nil {
		return nil, err
	}

	go func() {
		for {
			tcp, err := ln.Accept()
			if err != nil {
				log.Printf("[hci-sim] accept 错误: %v", err)
				return
			}
			go func() {
				conn, chans, reqs, err := ssh.NewServerConn(tcp, sshConfig)
				if err != nil {
					log.Printf("[hci-sim] SSH 握手失败: %v", err)
					return
				}
				defer conn.Close()
				log.Printf("[hci-sim] 客户端已连接: %s (user=%s)", conn.RemoteAddr(), conn.User())
				go ssh.DiscardRequests(reqs)
				for ch := range chans {
					go handler.handleChannel(ch, conn.RemoteAddr())
				}
			}()
		}
	}()
	return ln, nil
}

func main() {
	listen := flag.String("listen", envOrDefault("HCI_SIM_LISTEN", "0.0.0.0:2222"), "SSH 监听地址")
	hostKeyPath := flag.String("host-key", envOrDefault("HCI_SIM_HOST_KEY", "./hci-sim-hostkey"), "SSH 主机私钥路径（不存在则生成并持久化）")
	fixturesDir := flag.String("fixtures", envOrDefault("HCI_SIM_FIXTURES", "./fixtures"), "fixture 目录或文件")
	user := flag.String("user", envOrDefault("HCI_SIM_USER", "sim"), "允许登录的用户名")
	password := flag.String("password", envOrDefault("HCI_SIM_PASSWORD", "sim"), "登录密码（terminal_bridge 会自动追加 sangfornetwork 后缀）")
	acceptAny := flag.Bool("accept-any-password", false, "P0 测试桩：接受任意密码（非生产，仅本地验证用）")
	leaseSecret := flag.String("lease-secret", envOrDefault("HCI_SIM_LEASE_SECRET", ""), "scenario lease 校验密钥（空=不校验）")
	flag.Parse()

	if _, err := startSimServer(serverOptions{
		Listen:      *listen,
		HostKeyPath: *hostKeyPath,
		Fixtures:    *fixturesDir,
		User:        *user,
		Password:    *password,
		AcceptAny:   *acceptAny,
		LeaseSecret: *leaseSecret,
	}); err != nil {
		log.Fatalf("[hci-sim] 启动失败: %v", err)
	}
	log.Printf("[hci-sim] SSH 仿真服务已启动: %s (user=%s, fixtures=%s)", *listen, *user, *fixturesDir)
	select {}
}

// loadOrGenerateHostKey 读取或生成 ed25519 主机密钥并持久化，避免重启后 host key 变化导致 known_hosts 校验失败。
func loadOrGenerateHostKey(path string) (ssh.Signer, error) {
	if data, err := os.ReadFile(path); err == nil {
		return ssh.ParsePrivateKey(data)
	}
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, err
	}
	der, err := x509.MarshalPKCS8PrivateKey(priv)
	if err != nil {
		return nil, err
	}
	pemBytes := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})
	if err := os.WriteFile(path, pemBytes, 0o600); err != nil {
		log.Printf("[hci-sim] 警告: 无法持久化 host key: %v", err)
	}
	return ssh.NewSignerFromKey(priv)
}

func envOrDefault(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

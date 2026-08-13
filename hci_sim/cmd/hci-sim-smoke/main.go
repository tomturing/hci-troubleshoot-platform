// hci-sim-smoke 通过真实 Terminal Bridge WebSocket + SSH 路径验证 Golden Fixture。
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"strings"
	"time"

	"golang.org/x/net/websocket"
)

type message struct {
	Type          string `json:"type"`
	CaseID        string `json:"case_id,omitempty"`
	Host          string `json:"host,omitempty"`
	Port          int    `json:"port,omitempty"`
	Username      string `json:"username,omitempty"`
	AuthType      string `json:"auth_type,omitempty"`
	Password      string `json:"password,omitempty"`
	ExecutionMode string `json:"execution_mode,omitempty"`
	ExecID        string `json:"exec_id,omitempty"`
	Command       string `json:"command,omitempty"`
	NodeIP        string `json:"node_ip,omitempty"`
	Container     string `json:"container,omitempty"`
	TraceID       string `json:"trace_id,omitempty"`
	Traceparent   string `json:"traceparent,omitempty"`
	TestRunID     string `json:"test_run_id,omitempty"`
	Timeout       int    `json:"timeout,omitempty"`
	Stdout        string `json:"stdout,omitempty"`
	Stderr        string `json:"stderr,omitempty"`
	ExitCode      int    `json:"exit_code,omitempty"`
	ErrorType     string `json:"error_type,omitempty"`
	Message       string `json:"message,omitempty"`
}

func main() {
	log.SetFlags(0)
	if connectionPath := strings.TrimSpace(os.Getenv("HCI_SIM_CONNECTION_JSON")); connectionPath != "" {
		if err := runConnectionAcceptance(connectionPath); err != nil {
			log.Fatal(err)
		}
		return
	}
	log.Fatal("必须设置 HCI_SIM_CONNECTION_JSON；smoke 验收只消费由当前 KBD/Tool 修订生成的 connection.json")
}

type acceptanceConnection struct {
	SupportID          string `json:"support_id"`
	RecommendedCommand string `json:"recommended_command"`
	IssuedAt           string `json:"issued_at"`
	ExpiresAt          string `json:"expires_at"`
	TTLSeconds         int64  `json:"ttl_seconds"`
	Connection         struct {
		Host          string `json:"host"`
		Port          int    `json:"port"`
		Username      string `json:"username"`
		AuthType      string `json:"auth_type"`
		Password      string `json:"password"`
		ExecutionMode string `json:"execution_mode"`
		TestRunID     string `json:"test_run_id"`
	} `json:"connection"`
}

func runConnectionAcceptance(path string) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("读取 connection.json 失败: %w", err)
	}
	var connection acceptanceConnection
	if err := json.Unmarshal(raw, &connection); err != nil {
		return fmt.Errorf("解析 connection.json 失败: %w", err)
	}
	if connection.SupportID == "" || connection.RecommendedCommand == "" || connection.Connection.Password == "" {
		return errors.New("connection.json 缺少 support_id、recommended_command 或 Lease password")
	}
	if connection.ExpiresAt != "" {
		expiresAt, parseErr := time.Parse(time.RFC3339, connection.ExpiresAt)
		if parseErr != nil {
			return fmt.Errorf("connection.json expires_at 格式无效: %w", parseErr)
		}
		if !time.Now().UTC().Before(expiresAt) {
			return fmt.Errorf("Lease 已过期 expires_at=%s，请重新运行 two-step-acceptance.sh", connection.ExpiresAt)
		}
	}
	bridgeURL := env("HCI_SIM_BRIDGE_URL", "ws://127.0.0.1:9999")
	origin := env("HCI_SIM_BRIDGE_ORIGIN", "http://172.28.24.21")
	caseID := "SIM-KBD-" + connection.SupportID + "-" + time.Now().UTC().Format("150405")
	ws, err := websocket.Dial(bridgeURL, "", origin)
	if err != nil {
		return fmt.Errorf("连接 Linux Terminal Bridge 失败: %w", err)
	}
	defer ws.Close()
	if err := send(ws, message{
		Type: "ssh_connect", CaseID: caseID, Host: connection.Connection.Host, Port: connection.Connection.Port,
		Username: connection.Connection.Username, AuthType: connection.Connection.AuthType, Password: connection.Connection.Password,
		ExecutionMode: connection.Connection.ExecutionMode, TestRunID: connection.Connection.TestRunID,
	}); err != nil {
		return err
	}
	if _, err := receiveUntil(ws, "ssh_connected", ""); err != nil {
		return fmt.Errorf("Linux Terminal Bridge SSH 连接失败: %w", err)
	}
	execID := "sim-kbd-" + connection.SupportID + "-" + time.Now().UTC().Format("150405.000")
	if err := send(ws, message{Type: "ssh_exec_process", CaseID: caseID, ExecID: execID, Command: connection.RecommendedCommand, Container: "host", Timeout: 15}); err != nil {
		return err
	}
	result, err := receiveUntil(ws, "exec_result", execID)
	if err != nil {
		return fmt.Errorf("recommended_command 未收到结果: %w", err)
	}
	if result.ExitCode != 0 {
		return fmt.Errorf("recommended_command exit_code=%d stdout=%q stderr=%q", result.ExitCode, result.Stdout, result.Stderr)
	}
	if !strings.Contains(result.Stdout, `"support_id":"`+connection.SupportID+`"`) {
		return fmt.Errorf("结果缺少 support_id=%s", connection.SupportID)
	}
	log.Printf("PASS support_id=%s test_run_id=%s ssh=connected exit_code=%d synthetic=true", connection.SupportID, connection.Connection.TestRunID, result.ExitCode)
	_ = send(ws, message{Type: "ssh_disconnect", CaseID: caseID})
	return nil
}

func send(ws *websocket.Conn, value message) error {
	encoded, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return websocket.Message.Send(ws, string(encoded))
}

func receiveUntil(ws *websocket.Conn, expectedType, execID string) (message, error) {
	_ = ws.SetDeadline(time.Now().Add(20 * time.Second))
	for {
		var raw string
		if err := websocket.Message.Receive(ws, &raw); err != nil {
			return message{}, err
		}
		var current message
		if err := json.Unmarshal([]byte(raw), &current); err != nil {
			continue
		}
		if current.Type == "ssh_error" {
			return current, errors.New(current.Message)
		}
		if current.Type == expectedType && (execID == "" || current.ExecID == execID) {
			return current, nil
		}
	}
}

func env(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

package main

import (
	"io"
	"strings"
	"testing"
	"time"
)

// TestOnOutputStartNilWSNoPanic 守护 on_output_start 在 ws==nil 时不 panic：
// 交付后台自动重连节点走的就是 nil ws 路径，若 sendMsg(nil, ...) 未被保护就会崩。
// 同时验证 exec 完成标记仍能被正常解析并回环给监听者（runCommand 依赖这条链路）。
func TestOnOutputStartNilWSNoPanic(t *testing.T) {
	s := &SSHSession{createdAt: time.Now(), listeners: make(map[string]*ExecListener)}
	resCh := make(chan ExecResult, 1)
	// ExecID 去连字符后前 16 位 = 1111111122223333，对应标记前缀 __EXEC_DONE_1111111122223333:
	s.registerExecListener(&ExecListener{
		ExecID:     "11111111-2222-3333-4444-555555555555",
		StartTime:  time.Now(),
		OutputBuf:  new(strings.Builder),
		ResultChan: resCh,
	})

	r, w := io.Pipe()
	go func() {
		// 普通终端输出：触发 ssh_output 分支，ws==nil 必须跳过 sendMsg
		_, _ = w.Write([]byte("some terminal output\r\n"))
		// exec 完成标记：触发 exec_result 分支，ws==nil 必须跳过 sendMsg，但结果仍回环
		_, _ = w.Write([]byte("__EXEC_DONE_1111111122223333:0\r\nhello\r\n"))
		_ = w.Close()
	}()

	s.on_output_start(nil, r, "case1", func(reason string) {})

	select {
	case res := <-resCh:
		if res.ExitCode != 0 {
			t.Fatalf("期望 exit 0，得到 %d", res.ExitCode)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("监听未收到 exec 结果：on_output_start 在 ws==nil 时可能提前返回或 panic")
	}
}

// TestDeliverySessionForLiveSession 守护：已有会话时直接复用，不触发自动重连。
func TestDeliverySessionForLiveSession(t *testing.T) {
	b := newBridge()
	sess := &SSHSession{caseID: "case-x", createdAt: time.Now()}
	b.set(sessionKey("case-x", "10.0.0.1"), sess)
	r := &deliveryRunner{bridge: b}
	job := &deliveryJob{CaseID: "case-x", NodeIP: "10.0.0.1", Phase: phasePulling}
	if got := r.sessionFor(job); got != sess {
		t.Fatalf("期望复用已有会话，得到 %v", got)
	}
}

// TestDeliverySessionForNoAuthReturnsNil 守护：没有 lastAuth、也没有会话时，
// 不尝试连接、不 panic，返回 nil（由调用方转为 needs_reauth，走人工重连）。
// 这是「裸」交付的正确退路——不能因为没有节点就假装能自动恢复。
func TestDeliverySessionForNoAuthReturnsNil(t *testing.T) {
	r := &deliveryRunner{bridge: newBridge()}
	job := &deliveryJob{CaseID: "case-x", NodeIP: "10.0.0.1", Phase: phasePulling}
	if got := r.sessionFor(job); got != nil {
		t.Fatalf("期望无会话返回 nil，得到 %v", got)
	}
}

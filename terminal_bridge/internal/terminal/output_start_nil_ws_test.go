package terminal

// 从 delivery 域迁入：这个用例守护的是终端输出泵在 ws==nil 时不 panic，
// 与交付业务无关；放在 delivery 会让"交付域依赖终端域"这条边永久存在。

import (
	"io"
	"strings"
	"terminal_bridge/internal/sshx"
	"testing"
	"time"
)

// TestOnOutputStartNilWSNoPanic 守护 On_output_start 在 ws==nil 时不 panic：
// 交付后台自动重连节点走的就是 nil ws 路径，若 models.SendMsg(nil, ...) 未被保护就会崩。
// 同时验证 exec 完成标记仍能被正常解析并回环给监听者（runCommand 依赖这条链路）。
func TestOnOutputStartNilWSNoPanic(t *testing.T) {
	s := &sshx.SSHSession{CreatedAt: time.Now(), Listeners: make(map[string]*sshx.ExecListener)}
	resCh := make(chan sshx.ExecResult, 1)
	// ExecID 去连字符后前 16 位 = 1111111122223333，对应标记前缀 __EXEC_DONE_1111111122223333:
	s.RegisterExecListener(&sshx.ExecListener{
		ExecID:     "11111111-2222-3333-4444-555555555555",
		StartTime:  time.Now(),
		OutputBuf:  new(strings.Builder),
		ResultChan: resCh,
	})

	r, w := io.Pipe()
	go func() {
		// 普通终端输出：触发 ssh_output 分支，ws==nil 必须跳过 models.SendMsg
		_, _ = w.Write([]byte("some terminal output\r\n"))
		// exec 完成标记：触发 exec_result 分支，ws==nil 必须跳过 models.SendMsg，但结果仍回环
		_, _ = w.Write([]byte("__EXEC_DONE_1111111122223333:0\r\nhello\r\n"))
		_ = w.Close()
	}()

	On_output_start(s, nil, r, "case1", func(reason string) {})

	select {
	case res := <-resCh:
		if res.ExitCode != 0 {
			t.Fatalf("期望 exit 0，得到 %d", res.ExitCode)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("监听未收到 exec 结果：On_output_start 在 ws==nil 时可能提前返回或 panic")
	}
}

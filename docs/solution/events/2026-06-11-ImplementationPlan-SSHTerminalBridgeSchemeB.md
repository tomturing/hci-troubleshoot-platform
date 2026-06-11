# Implementation Plan - SSH Terminal Bridge Scheme B (Dual-Channel Design)

This implementation plan outlines the steps to execute Scheme B, transitioning the command execution proxy from a single interactive PTY session to a dual-channel design (Pty Interactive Channel + Exec Transaction Channel).

## User Review Required

> [!IMPORTANT]
> This is a major architectural change that decouples interactive web terminal sessions from AI agent transaction executions.
> 1. **Protocol Change**: Adds new WebSocket messages (`ssh_exec_process`, `exec_stdout`, `exec_stderr`, `exec_result`).
> 2. **Session Lifecycle**: Each agent command execution on the Exec channel will spin up a transient, dedicated SSH session without PTY allocation.
> 3. **FastAPI Contract**: The `/exec-result` route will accept structured `stdout`, `stderr`, and `exit_code` parameters.

## Proposed Changes

### 1. Go Bridge (terminal_bridge)

#### [MODIFY] [main.go](file:///mnt/d/aihci/hci-troubleshoot-platform/terminal_bridge/main.go)
- Declare `ssh_exec_process` incoming message handler.
- In `execProcess`, invoke `client.NewSession()` for each request.
- Run `session.Start(command)` without requesting PTY.
- Read stdout and stderr using separate stream readers and push chunks labeled `exec_stdout` and `exec_stderr` containing data.
- Block on `session.Wait()` to capture the exit status, then send an `exec_result` frame and close the session.

### 2. Frontend Browser (Vue / TypeScript)

#### [MODIFY] [terminal.ts](file:///mnt/d/aihci/hci-troubleshoot-platform/frontend/customer/src/api/terminal.ts)
- Implement `buildAgentExecProcessMessage(caseId, execId, command)` to send clean commands without trailing markers.

#### [MODIFY] [chat.ts](file:///mnt/d/aihci/hci-troubleshoot-platform/frontend/customer/src/stores/chat.ts)
- Add buffer dictionaries to accumulate `exec_stdout` and `exec_stderr` separately per `exec_id`.
- Dispatch execution requests via `ssh_exec_process` when risk level is low (or non-interactive).
- Collect the final `exec_result` frame and post the structured `stdout`, `stderr`, and `exit_code` back to `/exec-result`.

#### [MODIFY] [MessageBubble.vue](file:///mnt/d/aihci/hci-troubleshoot-platform/frontend/customer/src/components/MessageBubble.vue)
- Update output rendering to cleanly display separate stdout/stderr blocks instead of printing the raw serialized object.

### 3. Backend Agent-Service (FastAPI / Python)

#### [MODIFY] [executor.py](file:///mnt/d/aihci/hci-troubleshoot-platform/backend/agent-service/app/tools/acli/executor.py)
- Remove automatic `2>&1` redirection injection.
- Parse the structured JSON input from the incoming HTTP API request, assigning standard output and errors into their respective fields.

#### [MODIFY] [react_engine.py](file:///mnt/d/aihci/hci-troubleshoot-platform/backend/agent-service/app/adapters/agents/htp/react_engine.py)
- Propagate separate `stdout` and `stderr` to the LLM.

## Verification Plan

### Automated Tests
- Run `uv run pytest backend/agent-service/tests/ -q` to verify existing tests remain green.
- Add unit tests for the updated `executor.py` verifying structured `stdout`/`stderr` handling.

### Manual Verification
- Launch the dev environment.
- Execute an agent diagnostics step and verify that output/error rendering is correct, without backslash escapes or swallowed first lines.
- Verify user input during command execution no longer disrupts the AI transaction output.

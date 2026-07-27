#!/usr/bin/env node

// Terminal Bridge 安全故障注入脚本。
// 使用真实 Windows Edge、同源 WebSocket 和真实 HCI SSH；凭据与命令输出只在进程内存在。

const fs = require("fs");
const { randomBytes, randomUUID } = require("crypto");

const playwrightModule = process.env.PLAYWRIGHT_MODULE;
if (!playwrightModule) throw new Error("缺少 PLAYWRIGHT_MODULE");
const { chromium } = require(playwrightModule);

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少 ${name}`);
  return value;
}

function randomHex(bytes) {
  return randomBytes(bytes).toString("hex");
}

async function main() {
  const baseUrl = required("HCI_E2E_BASE_URL");
  const edgePath = required("HCI_E2E_EDGE_PATH");
  const privateKey = fs.readFileSync(required("HCI_E2E_PRIVATE_KEY_PATH"), "utf8");
  if (!privateKey.includes("PRIVATE KEY")) throw new Error("SSH 私钥文件格式无效");

  const command = required("HCI_FAULT_COMMAND");
  const caseId = process.env.HCI_FAULT_CASE_ID || `E2E${Date.now()}`;
  if (caseId.length > 32) throw new Error("HCI_FAULT_CASE_ID 不能超过 32 个字符");
  const execId = randomUUID();
  const traceId = randomHex(16);
  const parentSpanId = randomHex(8);
  const traceparent = `00-${traceId}-${parentSpanId}-01`;
  const timeoutSeconds = Number.parseInt(process.env.HCI_FAULT_WAIT_TIMEOUT_SECONDS || "180", 10);

  const browser = await chromium.launch({ executablePath: edgePath, headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto(baseUrl, { waitUntil: "networkidle", timeout: 60_000 });

  const result = await page.evaluate(
    async ({ caseId, execId, traceId, traceparent, command, privateKey, sshHost, sshPort, sshUser, timeoutMs }) => {
      const wsUrl = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/terminal-bridge`;
      const socket = new WebSocket(wsUrl);
      const bridgeLogs = [];

      const timeout = (label) =>
        new Promise((_, reject) => window.setTimeout(() => reject(new Error(`${label} 超时`)), timeoutMs));

      const connected = new Promise((resolve, reject) => {
        socket.addEventListener("open", () => {
          socket.send(
            JSON.stringify({
              type: "ssh_connect",
              case_id: caseId,
              host: sshHost,
              port: sshPort,
              username: sshUser,
              auth_type: "key",
              private_key: privateKey,
              trace_id: traceId,
              traceparent,
            }),
          );
        });
        socket.addEventListener("error", () => reject(new Error("WebSocket 连接失败")));
        socket.addEventListener("message", (event) => {
          let message;
          try {
            message = JSON.parse(event.data);
          } catch {
            return;
          }
          if (message.type === "bridge_log" && message.case_id === caseId) bridgeLogs.push(message);
          if (message.type === "ssh_connected" && message.case_id === caseId) resolve();
          if (message.type === "ssh_error" && message.case_id === caseId) {
            reject(new Error(message.message || "SSH 连接失败"));
          }
        });
      });
      await Promise.race([connected, timeout("SSH 连接")]);

      const execution = new Promise((resolve, reject) => {
        socket.addEventListener("message", (event) => {
          let message;
          try {
            message = JSON.parse(event.data);
          } catch {
            return;
          }
          if (message.type === "exec_result" && message.exec_id === execId) {
            resolve({
              exitCode: message.exit_code,
              traceId: message.trace_id,
              traceparent: message.traceparent,
              artifactId: message.artifact_id,
              stdoutBytes: message.stdout_bytes,
              stderrBytes: message.stderr_bytes,
              stdoutSha256: message.stdout_sha256,
              stderrSha256: message.stderr_sha256,
              stdoutTruncated: message.stdout_truncated === true,
              stderrTruncated: message.stderr_truncated === true,
              durationMs: message.duration_ms,
              timedOut: message.timed_out === true,
              errorType: message.error_type || "none",
            });
          }
          if (message.type === "ssh_error" && message.case_id === caseId) {
            reject(new Error(message.message || "SSH 执行期间连接失败"));
          }
        });
      });

      socket.send(
        JSON.stringify({
          type: "ssh_exec_process",
          case_id: caseId,
          exec_id: execId,
          command,
          trace_id: traceId,
          traceparent,
        }),
      );
      const executionResult = await Promise.race([execution, timeout("命令执行")]);
      await new Promise((resolve) => window.setTimeout(resolve, 1_500));
      socket.close();

      const currentExecLogs = bridgeLogs.filter(
        (entry) => entry.exec_id === execId || entry.extra?.exec_id === execId,
      );
      if (currentExecLogs.length === 0) throw new Error("未收到当前 exec_id 的 Bridge 结构化日志");

      const ingest = async () => {
        const response = await fetch("/api/bridge-logs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ logs: currentExecLogs }),
        });
        const body = await response.json().catch(() => ({}));
        return {
          status: response.status,
          accepted: body.accepted,
          duplicates: body.duplicates,
          skipped: body.skipped,
        };
      };

      const firstIngest = await ingest();
      const duplicateIngest = await ingest();
      return {
        caseId,
        execId,
        requestedTraceId: traceId,
        ...executionResult,
        bridgeLogEvents: [...new Set(currentExecLogs.map((entry) => entry.event))].sort(),
        bridgeLogCount: currentExecLogs.length,
        firstIngest,
        duplicateIngest,
      };
    },
    {
      caseId,
      execId,
      traceId,
      traceparent,
      command,
      privateKey,
      sshHost: required("HCI_E2E_SSH_HOST"),
      sshPort: Number.parseInt(process.env.HCI_E2E_SSH_PORT || "22", 10),
      sshUser: required("HCI_E2E_SSH_USER"),
      timeoutMs: timeoutSeconds * 1_000,
    },
  );

  await browser.close();
  const serialized = JSON.stringify(result, null, 2);
  const resultPath = process.env.HCI_FAULT_RESULT_PATH;
  if (resultPath) fs.writeFileSync(resultPath, `${serialized}\n`, "utf8");
  console.log(serialized);
}

main().catch((error) => {
  console.error(`FAULT_INJECTION_ERROR=${error.message}`);
  process.exit(1);
});

#!/usr/bin/env node

// Terminal Bridge 真实浏览器端到端验收脚本。
// 使用 Windows Edge 加载 Customer UI；SSH 私钥只在进程内读取，不输出、不落盘。

const fs = require("fs");

const playwrightModule = process.env.PLAYWRIGHT_MODULE;
if (!playwrightModule) {
  throw new Error("缺少 PLAYWRIGHT_MODULE");
}

const { chromium } = require(playwrightModule);
const resultLines = [];
let capturedExecFrame = null;
let capturedExecResultRequest = null;
let capturedBridgeLogRequest = null;
let resolveExecFrame;
const execFramePromise = new Promise((resolve) => {
  resolveExecFrame = resolve;
});

function record(line) {
  resultLines.push(line);
  console.log(line);
}

function flushResult() {
  const resultPath = process.env.HCI_E2E_RESULT_PATH;
  if (resultPath) {
    fs.writeFileSync(resultPath, `${resultLines.join("\n")}\n`, "utf8");
  }
}

async function main() {
  const baseUrl = process.env.HCI_E2E_BASE_URL;
  const edgePath = process.env.HCI_E2E_EDGE_PATH;
  if (!baseUrl || !edgePath) {
    throw new Error("缺少 HCI_E2E_BASE_URL 或 HCI_E2E_EDGE_PATH");
  }

  const browser = await chromium.launch({
    executablePath: edgePath,
    headless: true,
  });
  const context = await browser.newContext();
  const page = await context.newPage();

  page.on("websocket", (socket) => {
    record(`[browser:websocket] ${socket.url()}`);
    socket.on("framesent", (frame) => {
      try {
        const payload = typeof frame.payload === "string" ? frame.payload : frame.payload.toString("utf8");
        const message = JSON.parse(payload);
        if (message.type === "ssh_exec_process") {
          capturedExecFrame = {
            execId: message.exec_id || "",
            traceId: message.trace_id || "",
            traceparent: message.traceparent || "",
          };
          record(
            `[browser:exec-frame] exec_id=${message.exec_id || ""} trace_id=${message.trace_id || ""} traceparent=${message.traceparent || ""}`,
          );
          resolveExecFrame(capturedExecFrame);
        }
      } catch {
        // SSH 输入、私钥和普通终端帧不得写入验收日志。
      }
    });
  });

  page.on("response", (response) => {
    const url = response.url();
    if (url.includes("/api/bridge-logs") || url.includes("/exec-result")) {
      record(`[browser:http] ${response.status()} ${url}`);
    }
  });

  page.on("request", (request) => {
    if (request.method() !== "POST") return;
    const url = request.url();
    const postData = request.postData();
    if (!postData) return;

    if (url.includes("/exec-result")) {
      capturedExecResultRequest = { url, postData };
      return;
    }
    if (!url.includes("/api/bridge-logs") || !capturedExecFrame?.execId) return;
    try {
      const payload = JSON.parse(postData);
      const hasCurrentExecLog = Array.isArray(payload.logs) && payload.logs.some((entry) => {
        return entry?.exec_id === capturedExecFrame.execId || entry?.extra?.exec_id === capturedExecFrame.execId;
      });
      if (hasCurrentExecLog) capturedBridgeLogRequest = { url, postData };
    } catch {
      // 回采请求体仅在内存中用于幂等重放，解析失败时不记录原文。
    }
  });

  page.on("console", (message) => {
    const text = message.text();
    if (!text.toLowerCase().includes("password") && !text.includes("PRIVATE KEY")) {
      record(`[browser:${message.type()}] ${text}`);
    }
  });

  await page.goto(baseUrl, { waitUntil: "networkidle", timeout: 60_000 });
  record(`PAGE_URL=${page.url()}`);
  record(`PAGE_TITLE=${await page.title()}`);

  if (process.env.HCI_E2E_INSPECT_TERMINAL === "true") {
    await page.getByText("SSH终端", { exact: true }).click();
    await page.waitForTimeout(1_000);
  }

  if (process.env.HCI_E2E_INSPECT_CONNECT === "true") {
    await page.getByText(/连接 SSH 并创建工单/).click();
    await page.waitForTimeout(1_000);
  }

  if (process.env.HCI_E2E_INSPECT_KEY_AUTH === "true") {
    await page.getByText("密钥认证", { exact: true }).click();
    await page.waitForTimeout(500);
  }

  if (process.env.HCI_E2E_CREATE_CASE === "true") {
    const privateKeyPath = process.env.HCI_E2E_PRIVATE_KEY_PATH;
    const sshHost = process.env.HCI_E2E_SSH_HOST;
    const sshUser = process.env.HCI_E2E_SSH_USER;
    if (!privateKeyPath || !sshHost || !sshUser) {
      throw new Error("真实连接缺少 SSH 主机、用户或私钥路径");
    }
    const privateKey = fs.readFileSync(privateKeyPath, "utf8");

    const fillFormItem = async (label, value) => {
      const item = page.locator(".el-form-item").filter({ hasText: label }).first();
      const control = item.locator("input, textarea").first();
      await control.fill(value);
    };

    await fillFormItem("标题", process.env.HCI_E2E_CASE_TITLE || "Terminal Bridge P0 端到端验收");
    await fillFormItem(
      "描述",
      process.env.HCI_E2E_CASE_DESCRIPTION || "dev 环境端到端可观测性只读验收，请执行基础信息检查",
    );
    await fillFormItem("主机地址", sshHost);
    await fillFormItem("端口", process.env.HCI_E2E_SSH_PORT || "22");
    await fillFormItem("用户名", sshUser);
    await page.getByText("密钥认证", { exact: true }).click();
    await fillFormItem("私钥", privateKey);

    const connectButton = page.getByRole("button", { name: /连接 SSH 并创建工单/ }).first();
    await connectButton.click();
    await page.waitForTimeout(15_000);
  }

  if (process.env.HCI_E2E_SELECT_FIRST_OPTION === "true") {
    const terminalDrawer = page.locator(".el-overlay.is-drawer:visible").first();
    if ((await terminalDrawer.count()) > 0) {
      await page.keyboard.press("Escape");
      await terminalDrawer.waitFor({ state: "hidden", timeout: 10_000 });
      record("TERMINAL_DRAWER_CLOSED=true");
    }
    const firstOption = page.getByRole("button", { name: /^①/ }).first();
    const optionTimeoutSeconds = Number.parseInt(process.env.HCI_E2E_OPTION_TIMEOUT_SECONDS || "180", 10);
    await firstOption.waitFor({ state: "visible", timeout: optionTimeoutSeconds * 1_000 });
    record(`SELECT_OPTION=${(await firstOption.innerText()).replace(/\s+/g, " ").slice(0, 200)}`);
    await firstOption.click();
    const afterOptionWaitSeconds = Number.parseInt(process.env.HCI_E2E_AFTER_OPTION_WAIT_SECONDS || "15", 10);
    await page.waitForTimeout(afterOptionWaitSeconds * 1_000);
  }

  const chatMessage = process.env.HCI_E2E_CHAT_MESSAGE;
  if (chatMessage) {
    const inputArea = page.locator(".input-area");
    const assistantInput = inputArea.locator("textarea").first();
    await assistantInput.waitFor({ state: "visible", timeout: 60_000 });
    await page.waitForFunction(() => {
      const input = document.querySelector(".input-area textarea");
      return input && !input.disabled;
    }, undefined, { timeout: 180_000 });
    await assistantInput.fill(chatMessage);
    await inputArea.getByRole("button", { name: "发送", exact: true }).click();
    record(`CHAT_MESSAGE_SENT=${chatMessage}`);
  }

  if (process.env.HCI_E2E_REQUIRE_EXEC_FRAME === "true") {
    const execTimeoutSeconds = Number.parseInt(process.env.HCI_E2E_EXEC_FRAME_TIMEOUT_SECONDS || "360", 10);
    const timeoutPromise = new Promise((_, reject) => {
      setTimeout(() => reject(new Error(`等待 ssh_exec_process 帧超时（${execTimeoutSeconds}s）`)), execTimeoutSeconds * 1_000);
    });
    const frame = capturedExecFrame || (await Promise.race([execFramePromise, timeoutPromise]));
    record(`EXEC_FRAME_CONFIRMED=${frame.execId}`);
    const afterExecSeconds = Number.parseInt(process.env.HCI_E2E_AFTER_EXEC_SECONDS || "10", 10);
    await page.waitForTimeout(afterExecSeconds * 1_000);
  }

  const replayRequest = async (captured, label) => {
    if (!captured) throw new Error(`未捕获可重放的 ${label} 请求`);
    const replayResult = await page.evaluate(async ({ url, postData }) => {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: postData,
      });
      const body = await response.json().catch(() => ({}));
      return {
        status: response.status,
        accepted: body.accepted,
        duplicates: body.duplicates,
        skipped: body.skipped,
        message: body.message,
      };
    }, captured);
    record(
      `${label}_REPLAY status=${replayResult.status} accepted=${replayResult.accepted ?? ""} duplicates=${replayResult.duplicates ?? ""} skipped=${replayResult.skipped ?? ""} message=${replayResult.message ?? ""}`,
    );
  };

  if (process.env.HCI_E2E_REPLAY_EXEC_RESULT === "true") {
    await replayRequest(capturedExecResultRequest, "EXEC_RESULT");
  }
  if (process.env.HCI_E2E_REPLAY_BRIDGE_LOGS === "true") {
    await replayRequest(capturedBridgeLogRequest, "BRIDGE_LOGS");
  }

  const waitSeconds = Number.parseInt(process.env.HCI_E2E_WAIT_SECONDS || "0", 10);
  if (Number.isFinite(waitSeconds) && waitSeconds > 0) {
    record(`WAIT_SECONDS=${waitSeconds}`);
    await page.waitForTimeout(waitSeconds * 1_000);
  }

  record("BODY_BEGIN");
  record((await page.locator("body").innerText()).slice(0, 12_000));
  record("BODY_END");

  const screenshotPath = process.env.HCI_E2E_SCREENSHOT;
  if (screenshotPath) {
    await page.screenshot({ path: screenshotPath, fullPage: true });
    record(`SCREENSHOT=${screenshotPath}`);
  }

  // 只验证凭据文件格式，确保不会把私钥注入命令行参数或验收结果。
  const verificationKeyPath = process.env.HCI_E2E_PRIVATE_KEY_PATH;
  if (verificationKeyPath) {
    const verificationKey = fs.readFileSync(verificationKeyPath, "utf8");
    if (!verificationKey.includes("PRIVATE KEY")) {
      throw new Error("SSH 私钥文件格式无效");
    }
  }

  flushResult();
  await browser.close();
  process.exit(0);
}

main().catch((error) => {
  record(`E2E_ERROR=${error.message}`);
  flushResult();
  process.exit(1);
});

#!/usr/bin/env node
// 完整在线验收：Admin → Gateway → Case/Conversation/Agent → Bridge → hci-sim → Result oracle。
const assert = require('node:assert/strict');

async function main() {
  const modulePath = process.env.PLAYWRIGHT_MODULE;
  const browserPath = process.env.HCI_E2E_BROWSER_PATH;
  const supportId = process.env.HCI_E2E_SUPPORT_ID;
  const category = process.env.HCI_E2E_CATEGORY;
  const description = process.env.HCI_E2E_DESCRIPTION;
  const title = process.env.HCI_E2E_TITLE;
  const expectVmConsole = process.env.HCI_E2E_EXPECT_VM_CONSOLE === '1';
  if (!modulePath || !browserPath || !supportId || !category || !description || !title) {
    throw new Error('需要 PLAYWRIGHT_MODULE、HCI_E2E_BROWSER_PATH、HCI_E2E_SUPPORT_ID、HCI_E2E_CATEGORY、HCI_E2E_DESCRIPTION、HCI_E2E_TITLE');
  }
  const { chromium } = require(modulePath);
  const browser = await chromium.launch({ executablePath: browserPath, headless: true });
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(180000);
    const base = (process.env.HCI_E2E_ADMIN_URL || 'http://localhost:3004').replace(/\/$/, '');
    let resultResponseError;
    const resultResponse = page.waitForResponse(
      response => /\/api\/hci-sim\/v1\/simulations\/test-runs\/[^/]+\/result$/.test(new URL(response.url()).pathname),
      { timeout: 300000 },
    ).catch(error => {
      resultResponseError = error;
      return null;
    });
    await page.goto(`${base}/admin/simulation`);
    console.log('step=page-loaded');
    await page.getByPlaceholder('例如 27123').fill(supportId);
    await page.getByRole('button', { name: '环境构建', exact: true }).click();
    const startButton = page.getByRole('button', { name: '开始测试', exact: true });
    await startButton.waitFor();
    const readyDeadline = Date.now() + 180000;
    while (!(await startButton.isEnabled()) && Date.now() < readyDeadline) {
      await page.waitForTimeout(250);
    }
    assert.equal(await startButton.isEnabled(), true, '环境构建后“开始测试”仍未解锁');
    console.log('step=environment-ready');
    await startButton.click();
    await page.locator('.case-form input').fill(`Signal v2 完整在线仿真 ${supportId}`);
    await page.locator('.case-form textarea').fill(description);
    await page.getByRole('button', { name: '创建工单并进入测试', exact: true }).click();
    console.log('step=case-submitted');

    // TestRun 只能提供执行环境，不能用 support_id 缩窄 Agent 候选。这里选择该
    // 隔离测试分类；分类中只含测试 KBD，最终仍由诊断结论独立回报实际案例编号。
    const categoryButtons = page.locator('.choice-options button');
    const initializationFailure = page.locator('.conversation-shell .message-body.status-failed').first();
    const categoryDeadline = Date.now() + 120000;
    while (!(await categoryButtons.first().isVisible().catch(() => false))) {
      if (await initializationFailure.isVisible().catch(() => false)) {
        throw new Error(`在线会话初始化失败：${(await initializationFailure.innerText()).trim()}`);
      }
      if (Date.now() >= categoryDeadline) {
        const conversationText = await page.locator('.conversation-shell').innerText().catch(() => '会话区域不可见');
        throw new Error(`等待分类选项超时；当前会话：${conversationText.slice(-2000)}`);
      }
      await page.waitForTimeout(250);
    }
    const renderedChoices = await categoryButtons.allTextContents();
    console.log(`step=category-options count=${renderedChoices.length}`);
    let categoryChoice = categoryButtons.filter({ hasText: category });
    if (await categoryChoice.count() !== 1) {
      // 完整在线测试也覆盖真实“以上都不是”补充症状流程。补充的是用户可见的
      // KBD 标题，不传 support_id/TestRun 目标；S0 仍须独立召回并让用户确认分类。
      const noneChoice = categoryButtons.filter({ hasText: '以上都不是' });
      assert.equal(await noneChoice.count(), 1, `分类未召回且缺少“以上都不是”：${JSON.stringify(renderedChoices)}`);
      await noneChoice.click();
      await page.getByPlaceholder('请输入具体症状描述...').fill(title);
      await page.locator('.none-symptom-actions').getByRole('button', { name: '提交', exact: true }).click();
      categoryChoice = page.locator('.choice-options button').filter({ hasText: category });
      await categoryChoice.waitFor({ state: 'visible', timeout: 120000 });
      assert.equal(await categoryChoice.count(), 1, `补充标题后仍未唯一召回隔离测试分类：${category}`);
      console.log('step=category-clarified');
    }
    await categoryChoice.click();
    console.log('step=category-selected');

    // 仿真浏览器回归只连接隔离的 hci-sim。真实策略仍按风险等级展示审批，
    // 测试驱动则像人工操作一样显式点击审批，验证完整交互链而不绕过策略。
    let resultSettled = false;
    let approvalCount = 0;
    const approveUntilSettled = (async () => {
      while (!resultSettled) {
        const buttons = page.getByRole('button', { name: '允许执行', exact: true });
        const count = await buttons.count();
        for (let index = 0; index < count; index += 1) {
          const button = buttons.nth(index);
          if (await button.isVisible().catch(() => false) && await button.isEnabled().catch(() => false)) {
            await button.click();
            approvalCount += 1;
            console.log(`step=command-approved count=${approvalCount}`);
          }
        }
        await page.waitForTimeout(200);
      }
    })();

    let response;
    try {
      response = await resultResponse;
      if (!response) {
        throw resultResponseError || new Error('未收到 TestRun（仿真测试运行）结果请求');
      }
    } finally {
      resultSettled = true;
      await approveUntilSettled;
    }
    const body = await response.json();
    const requestBody = JSON.parse(response.request().postData() || '{}');
    console.log(`step=result-submitted status=${response.status()} summary=${JSON.stringify(requestBody.report_summary || {})}`);
    assert.equal(response.status(), 200, JSON.stringify(body));
    assert.equal(requestBody.outcome, 'passed');
    assert.equal(requestBody.report_summary.expected_support_id, supportId);
    assert.equal(requestBody.report_summary.is_definitive, true);
    assert.equal(requestBody.report_summary.diagnostic_outcome_received, true);
    assert.ok(requestBody.report_summary.supported_support_ids.includes(supportId));
    assert.ok(requestBody.report_summary.command_count >= 1);
    assert.equal(requestBody.report_summary.transport_error_count, 0);
    const vmConsoleOperationCount = await page.getByText('虚拟机控制台截图采集', { exact: false }).count();
    const vmConsoleObservationCount = await page.getByText('虚拟机控制台观察：', { exact: false }).count();
    if (expectVmConsole) {
      assert.ok(vmConsoleOperationCount >= 1, 'qkv_vm_console 未执行固定截图操作');
      assert.ok(vmConsoleObservationCount >= 1, 'qkv_vm_console 未产出视觉观察结果');
    }
    await page.getByText('已通过', { exact: true }).waitFor({ timeout: 300000 });
    console.log(JSON.stringify({
      check: 'semantic-online-agent-browser',
      status: 'PASS',
      support_id: supportId,
      case_id: requestBody.report_summary.case_id,
      command_count: requestBody.report_summary.command_count,
      approval_count: approvalCount,
      nonzero_exit_count: requestBody.report_summary.nonzero_exit_count,
      vm_console_operation_count: vmConsoleOperationCount,
      vm_console_observation_count: vmConsoleObservationCount,
      supported_support_ids: requestBody.report_summary.supported_support_ids,
      scope: '真实浏览器与完整在线 Agent 诊断闭环',
    }));
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error.stack || error.message); process.exitCode = 1; });

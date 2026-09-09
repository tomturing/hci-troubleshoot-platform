#!/usr/bin/env node
// 真实浏览器只读验收：不修改、保存或发布 KBD，不创建工单或执行采集命令。
const assert = require('node:assert/strict');

async function main() {
  const modulePath = process.env.PLAYWRIGHT_MODULE;
  const browserPath = process.env.HCI_E2E_BROWSER_PATH;
  const supportId = process.env.HCI_E2E_SUPPORT_ID;
  const description = process.env.HCI_E2E_DESCRIPTION;
  const expectedCode = process.env.HCI_E2E_EXPECTED_ERROR_CODE;
  if (!modulePath || !browserPath || !supportId || !description) {
    throw new Error('需要 PLAYWRIGHT_MODULE、HCI_E2E_BROWSER_PATH、HCI_E2E_SUPPORT_ID、HCI_E2E_DESCRIPTION');
  }
  const { chromium } = require(modulePath);
  const browser = await chromium.launch({ executablePath: browserPath, headless: true });
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(15000);
    const blockedWrites = [];
    // 即使界面回归意外触发保存，也在发出前阻断，保证脚本只读。
    await page.route('**/api/**', async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method()) &&
          !(request.method() === 'POST' && /^\/api\/v1\/kbd\/\d+\/(semantic-preview|review-signals)$/.test(path))) {
        blockedWrites.push(`${request.method()} ${path}`);
        await route.abort();
        return;
      }
      await route.continue();
    });
    const base = process.env.HCI_E2E_ADMIN_URL || 'http://localhost:3002';
    await page.goto(`${base.replace(/\/$/, '')}/admin/knowledge/kbd-review`);
    await page.getByPlaceholder('按案例 ID 精准搜索').fill(supportId);
    await page.getByRole('button', { name: '搜索', exact: true }).click();
    const row = page.locator('.el-table__row').filter({ hasText: supportId });
    await row.getByRole('button', { name: '详情', exact: true }).click();
    await page.getByRole('button', { name: '路由试运行', exact: true }).click();
    const input = page.getByPlaceholder('输入正例或近似反例的客户描述');
    await input.waitFor({ state: 'visible' });
    await input.fill(description);
    const pendingResponse = page.waitForResponse(
      response => response.url().includes('/semantic-preview') && response.request().method() === 'POST',
      { timeout: 60000 },
    );
    await page.getByRole('button', { name: '试运行当前草稿', exact: true }).click();
    const response = await pendingResponse;
    const body = await response.json();
    if (expectedCode) {
      assert.equal(response.status(), 422);
      assert.equal(body.detail?.code, expectedCode);
      await page.locator('.el-alert--error').filter({ hasText: body.detail.message }).waitFor({ state: 'visible' });
    } else {
      assert.equal(response.status(), 200);
      assert.equal(body.preview_only, true);
      assert.ok(body.decision);
    }
    assert.equal(await input.inputValue(), description, '返回结果后必须保留输入');
    assert.deepEqual(blockedWrites, [], '界面不应发起写操作');
    console.log(JSON.stringify({
      check: 'semantic-entry-browser-smoke', status: 'PASS', support_id: supportId,
      http_status: response.status(), decision: body.decision,
      error_code: body.detail?.code, writes: 0,
      scope: '浏览器 → 管理端同源代理 → 网关 → KBD 路由预览；非诊断链路验收',
    }));
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });

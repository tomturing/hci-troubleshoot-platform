#!/usr/bin/env node
// 仅对显式指定的验收工单上传证据，检查数据面仍走客户页面同源地址。
const fs = require('fs');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE);

async function main() {
  const statePath = process.env.HCI_E2E_OFFLINE_STATE;
  if (!statePath) throw new Error('缺少 HCI_E2E_OFFLINE_STATE');
  const state = JSON.parse(fs.readFileSync(statePath, 'utf8'));
  const base = process.env.HCI_E2E_CUSTOMER_URL || 'http://localhost:3003';
  const browser = await chromium.launch({
    executablePath: process.env.HCI_E2E_BROWSER_PATH,
    headless: true,
  });
  try {
    const page = await browser.newPage();
    const uploaded = [];
    page.on('response', (response) => {
      if (response.url().includes('/api/direct/diagnosis-uploads/')) {
        uploaded.push({ url: response.url(), status: response.status() });
      }
    });
    await page.goto(`${base}/offline-diagnosis?case_id=${state.case.case_id}`);
    const downloading = page.waitForEvent('download');
    await page.getByRole('button', { name: '下载完整采集工具包', exact: true }).click();
    const download = await downloading;
    assert.equal(await download.failure(), null);
    assert.ok(fs.statSync(await download.path()).size > 0);
    state.browser_download = { status: 'PASS', file_name: download.suggestedFilename() };
    if (state.bundle) {
      fs.writeFileSync(statePath, JSON.stringify(state, null, 2) + '\n');
      console.log(JSON.stringify({ case_id: state.case.case_id, download: 'PASS', upload: 'already_completed' }));
      return;
    }
    await page.getByRole('button', { name: '选择证据包', exact: true }).first().waitFor();
    await page.locator('input[type=file]').setInputFiles(state.evidence_path);
    const completed = page.waitForResponse(r => /\/uploads\/[^/]+\/complete$/.test(r.url()), { timeout: 60000 });
    await page.getByRole('button', { name: '上传并开始诊断', exact: true }).click();
    const response = await completed;
    assert.equal(response.status(), 200, await response.text());
    assert.ok(uploaded.length > 0, '没有真实分片上传');
    for (const part of uploaded) {
      assert.equal(new URL(part.url).origin, new URL(base).origin, '直传必须与客户页面同源');
      assert.equal(part.status, 200);
    }
    state.bundle = await response.json();
    state.browser_upload = { same_origin: true, parts: uploaded.length, status: response.status() };
    fs.writeFileSync(statePath, JSON.stringify(state, null, 2) + '\n');
    console.log(JSON.stringify({ case_id: state.case.case_id, ...state.browser_upload }));
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });

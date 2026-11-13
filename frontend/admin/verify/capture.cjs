/**
 * 管理端链路定界真浏览器验收：挂载产品里同一个 LogCollectAttribution 组件，
 * 数据来自真实 derive() 生成的 fixture，再全页截图。
 *
 * 运行（仓库根，admin dev server 需已启动在 3102）：
 *   NODE_PATH=<npx playwright 的 node_modules> node frontend/admin/verify/capture.cjs
 */
const { resolve } = require('node:path')
const { chromium } = require('playwright')

const out = resolve(__dirname, '../../../docs/solution/events/assets/2026-11-13-验收-管理端链路定界.png')
const entry = 'http://127.0.0.1:3102/admin/verify/index.html'

async function main() {
  const browser = await chromium.launch({ args: ['--no-sandbox'] })
  const page = await browser.newPage({ viewport: { width: 1680, height: 1100 } })
  page.setDefaultTimeout(8000)
  const pageErrors = []
  page.on('pageerror', (error) => pageErrors.push(error.message))

  console.log('[step] 打开验收页')
  await page.goto(entry, { waitUntil: 'domcontentloaded' })
  await page.locator('.upload-attribution').waitFor()
  await page.waitForTimeout(500)

  const summaryCard = await page.locator('.upload-attribution').count()
  const tables = await page.locator('.el-table').count()
  const stageRows = await page.locator('.el-table').nth(1).locator('.el-table__row').count()
  console.log(`[step] 摘要卡=${summaryCard} 表格=${tables} 阶段行=${stageRows}`)
  if (summaryCard !== 1 || tables < 2 || stageRows !== 8) {
    throw new Error(`链路定界视图未渲染完整：摘要卡=${summaryCard} 表格=${tables} 阶段行=${stageRows}`)
  }

  await page.screenshot({ path: out, fullPage: true })
  console.log(`截图已保存：${out}`)
  console.log(`页面 JS 异常：${pageErrors.length ? pageErrors.join(' | ') : '无'}`)
  await browser.close()
  if (pageErrors.length) process.exitCode = 1
}

main().catch((error) => {
  console.error(`验收失败：${error.message}`)
  process.exitCode = 1
})
/**
 * 管理端链路定界真浏览器验收：挂载产品里同一个 LogCollectAttribution 组件，
 * 数据来自真实 derive() 生成的 fixture，再全页截图。
 *
 * 运行（仓库根，admin dev server 需已启动在 3102）：
 *   NODE_PATH=<playwright 所在 node_modules> node frontend/admin/verify/capture.cjs
 * 端口冲突时用 VERIFY_URL 指向实际 dev server。
 */
const { resolve } = require('node:path')
const { chromium } = require('playwright')

const assets = resolve(__dirname, '../../../docs/solution/events/assets')
const timelineShot = resolve(assets, '2026-11-16-验收-管理端详情可读性-时间线.png')
const dialogShot = resolve(assets, '2026-11-16-验收-管理端详情可读性-设计说明弹框.png')
const entry = process.env.VERIFY_URL || 'http://127.0.0.1:3102/admin/verify/index.html'

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
  const stageRows = await page.locator('.el-table').first().locator('.el-table__row').count()
  const statusTags = await page.locator('[data-testid="stage-status"]').count()
  console.log(`[step] 摘要卡=${summaryCard} 表格=${tables} 阶段行=${stageRows} 状态标签=${statusTags}`)
  if (summaryCard !== 1 || tables !== 1 || stageRows !== 8 || statusTags !== 8) {
    throw new Error(`时间线视图未渲染完整：摘要卡=${summaryCard} 表格=${tables} 阶段行=${stageRows} 状态标签=${statusTags}`)
  }
  await page.screenshot({ path: timelineShot, fullPage: true })

  console.log('[step] 点击感叹号查看固定设计说明弹框')
  await page.locator('[data-testid="attribution-help"]').click()
  await page.locator('[data-testid="attribution-reference"]').waitFor({ state: 'visible' })
  const cards = await page.locator('[data-testid="attribution-reference"] .ref-card').count()
  if (cards !== 5) throw new Error(`设计说明卡片数应为 5，实际 ${cards}`)
  // 等弹框淡入动画结束，避免截图抓到半透明的中间帧。
  await page.waitForTimeout(500)
  await page.screenshot({ path: dialogShot, fullPage: true })

  console.log(`截图已保存：${timelineShot} / ${dialogShot}`)
  console.log(`页面 JS 异常：${pageErrors.length ? pageErrors.join(' | ') : '无'}`)
  await browser.close()
  if (pageErrors.length) process.exitCode = 1
}

main().catch((error) => {
  console.error(`验收失败：${error.message}`)
  process.exitCode = 1
})
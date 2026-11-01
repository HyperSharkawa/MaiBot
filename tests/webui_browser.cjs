/** 在真实隔离后端上验证新设置的交互、保存与读回；不调用真实模型。 */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const assert = require('node:assert/strict')
const path = require('node:path')
const fs = require('node:fs/promises')
const base = process.env.WEBUI_TEST_URL || 'http://127.0.0.1:18731'
const output = process.env.WEBUI_TEST_OUTPUT
if (!output || new URL(base).hostname !== '127.0.0.1') throw new Error('必须指定隔离测试输出和本地转发地址')

/** 等待真实接口的保存响应，避免以固定延迟猜测是否完成。 */
function saved(page, suffix) {
  return page.waitForResponse(response => response.url().endsWith(suffix) && response.request().method() === 'POST')
}

/** 通过真实认证登录，保存新设置并对照其余配置。 */
async function main() {
  await fs.mkdir(output, { recursive: true })
  const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  const page = await context.newPage()
  const failures = []
  page.on('pageerror', error => failures.push(error.message))
  await context.route('**/*', route => {
    if (new URL(route.request().url()).hostname === '127.0.0.1') return route.continue()
    return route.abort()
  })
  try {
    const login = await context.request.post(`${base}/api/webui/auth/verify`, { data: { token: 'webui-synthetic-test-token' } })
    assert.equal((await login.json()).valid, true)
    assert.equal((await context.request.post(`${base}/api/webui/frontend/retry`)).status(), 202)
    for (let attempt = 0; attempt < 50; attempt++) {
      const status = await (await context.request.get(`${base}/api/webui/frontend/status`)).json()
      if (status.state === 'ready') break
      if (['missing', 'stale'].includes(status.state)) throw new Error(JSON.stringify(status))
      await page.waitForTimeout(100)
    }
    const before = (await (await context.request.get(`${base}/api/webui/config/bot`)).json()).config
    const beforeModels = (await (await context.request.get(`${base}/api/webui/config/model`)).json()).config
    const writes = []
    page.on('request', request => { if (request.method() === 'POST' && request.url().includes('/config/')) writes.push(request.url()) })
    await page.goto(`${base}/config/bot`)
    await page.getByRole('tab', { name: '聊天', exact: true }).click()
    const toggle = page.getByRole('switch', { name: '进行决策和回复前等待识图完成', exact: true })
    await toggle.waitFor()
    assert.equal(await toggle.getAttribute('aria-checked'), String(before.chat.wait_for_image_recognition))
    // 等待超过原界面保存防抖周期，证明初次打开没有写回配置。
    await page.waitForTimeout(2300)
    assert.deepEqual(writes, [])
    let response = saved(page, '/config/bot/section/chat')
    await toggle.click()
    assert.equal((await (await response).json()).success, true)
    const changed = (await (await context.request.get(`${base}/api/webui/config/bot`)).json()).config
    assert.equal(changed.chat.wait_for_image_recognition, !before.chat.wait_for_image_recognition)
    const unchanged = structuredClone(changed)
    unchanged.chat.wait_for_image_recognition = before.chat.wait_for_image_recognition
    assert.deepEqual(unchanged, before)
    await page.reload()
    await page.getByRole('tab', { name: '聊天', exact: true }).click()
    assert.equal(await toggle.getAttribute('aria-checked'), String(!before.chat.wait_for_image_recognition))
    response = saved(page, '/config/bot/section/chat')
    await toggle.click()
    assert.equal((await (await response).json()).success, true)
    await page.screenshot({ path: path.join(output, 'chat-desktop.png') })
    // 注入一次保存失败，必须保留未保存提示且不能改动后端文件。
    await page.route('**/config/bot/section/chat', route => route.fulfill({ status: 500, json: { success: false, message: '测试保存失败' } }), { times: 1 })
    response = saved(page, '/config/bot/section/chat')
    await toggle.click()
    assert.equal((await response).status(), 500)
    await page.getByRole('button', { name: '保存', exact: true }).waitFor()
    const failedSave = (await (await context.request.get(`${base}/api/webui/config/bot`)).json()).config
    assert.deepEqual(failedSave, before)
    await page.goto(`${base}/config/model`)
    writes.length = 0
    await page.getByRole('tab', { name: '为模型分配功能' }).click()
    const card = page.locator('.rounded-lg.border.bg-card').filter({ has: page.getByRole('heading', { name: '组件模型 (utils)', exact: true }) })
    const strategy = card.getByRole('combobox').last()
    await strategy.waitFor()
    await page.waitForTimeout(2300)
    assert.deepEqual(writes, [])
    assert.match(await strategy.innerText(), /按设置顺序/)
    response = saved(page, '/config/model/section/model_task_config')
    await strategy.click()
    await page.getByRole('option', { name: '随机选择（random）', exact: true }).click()
    assert.equal((await (await response).json()).success, true)
    response = saved(page, '/config/model/section/model_task_config')
    await strategy.click()
    await page.getByRole('option', { name: '按设置顺序', exact: true }).click()
    assert.equal((await (await response).json()).success, true)
    const handles = card.locator('[aria-roledescription="sortable"]')
    response = saved(page, '/config/model/section/model_task_config')
    const start = await handles.first().boundingBox()
    const end = await handles.nth(1).boundingBox()
    await page.mouse.move(start.x + start.width / 2, start.y + start.height / 2)
    await page.mouse.down()
    await page.mouse.move(start.x + start.width / 2 + 12, start.y + start.height / 2, { steps: 3 })
    await page.mouse.move(end.x + end.width / 2, end.y + end.height / 2, { steps: 20 })
    await page.mouse.up()
    assert.equal((await (await response).json()).success, true)
    const afterModels = (await (await context.request.get(`${base}/api/webui/config/model`)).json()).config
    const oldOrder = beforeModels.model_task_config.utils.model_list
    assert.deepEqual(afterModels.model_task_config.utils.model_list, [oldOrder[1], oldOrder[0], ...oldOrder.slice(2)])
    assert.equal(afterModels.model_task_config.utils.selection_strategy, 'priority')
    const unchangedModels = structuredClone(afterModels)
    unchangedModels.model_task_config.utils.model_list = beforeModels.model_task_config.utils.model_list
    assert.deepEqual(unchangedModels, beforeModels)
    await page.reload()
    await page.getByRole('tab', { name: '为模型分配功能' }).click()
    assert.match(await strategy.innerText(), /按设置顺序/)
    await page.screenshot({ path: path.join(output, 'model-desktop.png') })
    const mobile = await browser.newContext({ viewport: { width: 390, height: 844 }, storageState: await context.storageState() })
    const mobilePage = await mobile.newPage()
    await mobilePage.goto(`${base}/config/bot`)
    await mobilePage.getByRole('tab', { name: '聊天', exact: true }).click()
    await mobilePage.locator('#wait_for_image_recognition').locator('xpath=../..').evaluate(element => element.scrollIntoView({ block: 'center' }))
    await mobilePage.waitForTimeout(300)
    await mobilePage.screenshot({ path: path.join(output, 'chat-mobile.png') })
    await mobilePage.goto(`${base}/config/model`)
    await mobilePage.getByRole('tab', { name: '为模型分配功能' }).click()
    const mobileCard = mobilePage.locator('.rounded-lg.border.bg-card').filter({ has: mobilePage.getByRole('heading', { name: '组件模型 (utils)', exact: true }) })
    await mobileCard.getByRole('combobox').last().locator('xpath=..').evaluate(element => element.scrollIntoView({ block: 'center' }))
    await mobilePage.waitForTimeout(300)
    await mobilePage.screenshot({ path: path.join(output, 'model-mobile.png') })
    await mobile.close()
    console.log('CHAT_PASS', JSON.stringify({ initial: before.chat.wait_for_image_recognition, writes: writes.length, errors: failures }))
    console.log('MODEL_PASS', JSON.stringify(afterModels.model_task_config.utils))
    await fs.writeFile(path.join(output, 'before-models.json'), JSON.stringify(beforeModels))
    await context.request.post(`${base}/api/webui/config/model/section/model_task_config`, { data: beforeModels.model_task_config })
  } finally {
    await browser.close()
  }
}
/** 无前端资源时，实际点击恢复控件登录并重试，确认不会自动刷新页面。 */
async function recovery() {
  const recoveryBase = process.env.WEBUI_RECOVERY_TEST_URL
  if (!recoveryBase || new URL(recoveryBase).hostname !== '127.0.0.1') throw new Error('恢复测试需要单独回环地址')
  await fs.mkdir(output, { recursive: true })
  const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true })
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } })
  const page = await context.newPage()
  const legacy = process.env.WEBUI_RECOVERY_RESOURCE_MODE === 'legacy'
  try {
    // 未登录的旧页面从其原有登录入口开始，避免把旧应用自身的跳转混入恢复测试。
    await page.goto(legacy ? `${recoveryBase}/auth` : recoveryBase)
    if (legacy) {
      await page.getByText('管理页面尚未更新', { exact: true }).waitFor()
    } else {
      await page.getByRole('heading', { name: '管理页面尚未准备好' }).waitFor()
    }
    await page.getByRole('button', { name: '重试下载', exact: true }).click()
    await page.getByLabel('WebUI Token').fill('wrong-synthetic-token')
    await page.getByRole('button', { name: '登录并重试' }).click()
    await page.getByRole('alert').filter({ hasText: 'Token 无效' }).waitFor()
    await page.getByLabel('WebUI Token').fill('webui-synthetic-test-token')
    await page.screenshot({ path: path.join(output, legacy ? 'recovery-legacy-login-mobile.png' : 'recovery-login-mobile.png') })
    await page.getByRole('button', { name: '登录并重试' }).click()
    await page.getByRole('button', { name: '刷新页面', exact: true }).waitFor()
    if (legacy) {
      assert.equal(await page.locator('script[data-page-version]').getAttribute('data-page-version'), 'legacy')
    } else {
      assert.equal(await page.locator('#frontend-placeholder-title').count(), 1)
      assert.equal(await page.locator('#frontend-placeholder-title').innerText(), '管理页面已准备好')
    }
    await page.screenshot({ path: path.join(output, legacy ? 'recovery-legacy-ready-mobile.png' : 'recovery-ready-mobile.png') })
    const status = await (await context.request.get(`${recoveryBase}/api/webui/frontend/status`)).json()
    assert.equal(status.state, 'ready')
    assert.equal((await context.request.get(`${recoveryBase}/assets/missing.js`)).status(), 404)
    await page.getByRole('button', { name: '刷新页面', exact: true }).click()
    await page.locator('#frontend-placeholder-title').waitFor({ state: 'hidden' })
    assert.equal((await context.request.get(`${recoveryBase}/api/webui/health`)).status(), 200)
    console.log('RECOVERY_PASS', JSON.stringify(status))
  } finally {
    await browser.close()
  }
}
(process.env.WEBUI_RECOVERY_TEST_URL ? recovery() : main()).catch(error => { console.error(error); process.exitCode = 1 })

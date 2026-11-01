/** 使用真实浏览器验证宿主恢复控件的时序，不依赖 React 或业务配置。 */
const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const http = require('node:http')
const path = require('node:path')
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const root = path.resolve(__dirname, '..')
const current = 'a'.repeat(64)
const previous = 'b'.repeat(64)

/** 返回与宿主状态接口相同的测试数据。 */
function status(state) {
  return { state, expected_id: current, current_id: state === 'ready' ? current : null, using_old_page: false, error: null }
}

/** 为每个时序场景建立独立页面、请求计数和回环服务器。 */
async function runCase(browser, name, html, scenario) {
  const script = await fs.readFile(path.join(root, 'src/webui/frontend_host/recovery.js'))
  let state = 'ready'
  let statusRequests = 0
  let retries = 0
  let transfer = null
  let authenticated = true
  let selection = null
  const server = http.createServer((request, response) => {
    if (request.url === '/api/webui/frontend/recovery.js') {
      response.writeHead(200, { 'Content-Type': 'application/javascript' }).end(script)
    } else if (request.url === '/api/webui/frontend/status') {
      statusRequests++
      response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({ ...status(state), download: transfer }))
    } else if (request.url === '/api/webui/frontend/sources') {
      response.writeHead(authenticated ? 200 : 401, { 'Content-Type': 'application/json' }).end(JSON.stringify({ sources: [{ id: 'github', name: 'GitHub' }, { id: 'hongkong', name: '香港加速' }] }))
    } else if (request.url === '/api/webui/auth/verify') {
      let body = ''
      request.on('data', chunk => { body += chunk })
      request.on('end', () => {
        authenticated = JSON.parse(body).token === 'synthetic'
        response.writeHead(authenticated ? 200 : 401, { 'Content-Type': 'application/json' }).end(JSON.stringify({ valid: authenticated, message: authenticated ? '成功' : 'Token 无效' }))
      })
    } else if (request.url === '/api/webui/frontend/retry') {
      let body = ''
      request.on('data', chunk => { body += chunk })
      request.on('end', () => {
        retries++
        selection = body ? JSON.parse(body).source_id : 'auto'
        state = 'downloading'
        response.writeHead(202, { 'Content-Type': 'application/json' }).end(JSON.stringify(status(state)))
      })
    } else if (request.url === '/') {
      response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }).end(html)
    } else {
      response.writeHead(404).end()
    }
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } })
  const page = await context.newPage()
  try {
    await scenario({
      page, base: `http://127.0.0.1:${server.address().port}`,
      setState: value => { state = value }, counts: () => ({ statusRequests, retries }),
      setDownload: value => { transfer = value }, setAuthenticated: value => { authenticated = value },
      selected: () => selection,
    })
    console.log(`PASS ${name}`)
  } finally {
    await context.close()
    server.closeAllConnections()
    await new Promise(resolve => server.close(resolve))
  }
}

/** 构造携带实际响应版本的旧页面或匹配页面。 */
function pageMarkup(version) {
  return `<!doctype html><html><body><main>测试管理页</main><script src="/api/webui/frontend/recovery.js" data-page-version="${version}" defer></script></body></html>`
}

/** 检查首次就绪、重复重试以及页面离开后的轮询停止。 */
async function main() {
  const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true })
  try {
    const missing = await fs.readFile(path.join(root, 'src/webui/frontend_host/missing.html'), 'utf8')
    for (const [name, html] of [['说明页首次查询已就绪', missing], ['旧页面首次查询已更新', pageMarkup(previous)], ['无清单旧页面', pageMarkup('legacy')]]) {
      await runCase(browser, name, html, async ({ page, base, counts }) => {
        await page.goto(base)
        await page.getByRole('button', { name: '刷新页面', exact: true }).waitFor()
        assert.equal(counts().retries, 0)
        assert.equal(page.url(), `${base}/`)
        if (name.startsWith('说明页')) {
          assert.equal(await page.locator('#frontend-placeholder-title').innerText(), '管理页面已准备好')
          if (process.env.WEBUI_TEST_OUTPUT) {
            await fs.mkdir(process.env.WEBUI_TEST_OUTPUT, { recursive: true })
            await page.screenshot({ path: path.join(process.env.WEBUI_TEST_OUTPUT, 'ready-before-first-status.png') })
          }
        }
      })
    }
    await runCase(browser, '匹配页面不显示刷新提示', pageMarkup(current), async ({ page, base }) => {
      await page.goto(base)
      await page.waitForFunction(() => document.querySelector('maibot-frontend-status')?.shadowRoot.querySelector('strong').textContent === '新版页面已准备好')
      assert.equal(await page.locator('maibot-frontend-status section').isHidden(), true)
    })
    await runCase(browser, '后台下载持续禁用重试且离开后停止轮询', missing, async ({ page, base, setState, counts }) => {
      setState('missing')
      await page.goto(base)
      const retry = page.getByRole('button', { name: '重试下载', exact: true })
      await retry.click()
      await page.waitForFunction(() => document.querySelector('maibot-frontend-status')?.shadowRoot.querySelector('.detail').textContent.includes('正在下载'))
      assert.equal(await retry.isDisabled(), true)
      await retry.evaluate(button => button.click())
      assert.equal(counts().retries, 1)
      await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pagehide')))
      const before = counts().statusRequests
      // 超过约定的一秒轮询周期，验证离开后确实没有新的请求。
      await page.waitForTimeout(1200)
      assert.equal(counts().statusRequests, before)
      setState('ready')
      await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })))
      await page.getByRole('button', { name: '刷新页面', exact: true }).waitFor()
      assert.equal(counts().statusRequests, before + 1)
    })
    for (const [name, html] of [['说明页下载进度', missing], ['旧页面下载进度', pageMarkup(previous)]]) {
      await runCase(browser, name, html, async ({ page, base, setState, setDownload }) => {
        setState('downloading')
        const download = { phase: 'archive', source_name: '香港加速', source_index: 2, source_count: 4, received_bytes: 1200000, total_bytes: 2400000, bytes_per_second: 28000, low_speed_seconds: 0, automatic: true }
        setDownload(download)
        await page.goto(base)
        await page.getByText('当前下载源：香港加速 · 第 2/4 个', { exact: true }).waitFor()
        assert.equal(await page.getByRole('progressbar').getAttribute('value'), '50')
        assert.match(await page.locator('maibot-frontend-status .bytes').innerText(), /28.0 KB\/s/)
        if (process.env.WEBUI_TEST_OUTPUT) {
          await fs.mkdir(process.env.WEBUI_TEST_OUTPUT, { recursive: true })
          await page.screenshot({ path: path.join(process.env.WEBUI_TEST_OUTPUT, `${name}-mobile.png`) })
          await page.setViewportSize({ width: 1440, height: 900 })
          await page.screenshot({ path: path.join(process.env.WEBUI_TEST_OUTPUT, `${name}-desktop.png`) })
        }
        setDownload({ ...download, total_bytes: null, bytes_per_second: 1000, low_speed_seconds: 30 })
        await page.getByText('下载较慢，持续低于 5 KB/s 达 60 秒后将切换下载源。', { exact: true }).waitFor()
        assert.equal(await page.getByRole('progressbar').getAttribute('value'), null)
        setDownload({ ...download, source_index: 3, source_name: '加速源名称很长时也应当能够正常换行显示', received_bytes: 0, phase: 'checksum' })
        await page.getByText('正在获取校验文件…', { exact: true }).waitFor()
        setDownload({ ...download, phase: 'validating', received_bytes: 2400000 })
        setState('installing')
        await page.getByText('正在校验并安装页面…', { exact: true }).waitFor()
        assert.equal(await page.getByRole('button', { name: '刷新页面', exact: true }).isHidden(), true)
        setState('ready')
        await page.getByRole('button', { name: '刷新页面', exact: true }).waitFor()
        assert.equal(page.url(), `${base}/`)
      })
    }
    await runCase(browser, '登录后选源不自动下载', missing, async ({ page, base, setState, setAuthenticated, selected, counts }) => {
      setState('missing')
      setAuthenticated(false)
      await page.goto(base)
      await page.getByRole('button', { name: '登录后选择下载源', exact: true }).click()
      await page.getByLabel('WebUI Token').fill('wrong')
      await page.getByRole('button', { name: '登录', exact: true }).click()
      await page.getByRole('alert').filter({ hasText: 'Token 无效' }).waitFor()
      await page.getByLabel('WebUI Token').fill('synthetic')
      await page.getByRole('button', { name: '登录', exact: true }).click()
      const selector = page.getByRole('combobox', { name: '下载源', exact: true })
      await selector.waitFor()
      assert.equal(counts().retries, 0)
      await selector.selectOption('hongkong')
      assert.equal(counts().retries, 0)
      await page.getByRole('button', { name: '重试下载', exact: true }).click()
      await page.waitForFunction(() => document.querySelector('maibot-frontend-status').shadowRoot.querySelector('select').disabled)
      assert.equal(selected(), 'hongkong')
      assert.equal(counts().retries, 1)
    })
    await runCase(browser, '复用旧页面随后建立的登录状态', pageMarkup(previous), async ({ page, base, setState, setAuthenticated, counts }) => {
      setState('stale')
      setAuthenticated(false)
      await page.goto(base)
      const login = page.getByRole('button', { name: '登录后选择下载源', exact: true })
      await login.waitFor()
      setAuthenticated(true)
      await login.click()
      await page.getByRole('combobox', { name: '下载源', exact: true }).waitFor()
      assert.equal(await page.getByLabel('WebUI Token').isHidden(), true)
      assert.equal(counts().retries, 0)
    })
  } finally {
    await browser.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })

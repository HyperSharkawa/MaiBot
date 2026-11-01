/** 宿主管理恢复控件，不依赖 React、旧页面资源或外部服务。 */
(() => {
  const pageVersion = document.currentScript.dataset.pageVersion
  const placeholderTitle = document.getElementById('frontend-placeholder-title')
  const host = document.createElement('maibot-frontend-status')
  const shadow = host.attachShadow({ mode: 'open' })
  shadow.innerHTML = `
    <style>
      :host{position:fixed;right:16px;bottom:16px;z-index:2147483647;max-width:440px;width:calc(100% - 32px);font:14px/1.6 system-ui,sans-serif;color:#202632}
      section{background:#fff;border:1px solid #d7dce4;box-shadow:0 4px 24px #17203320;border-radius:12px;padding:18px}
      strong{display:block;font-size:16px}p{margin:8px 0}button,input{font:inherit;border-radius:6px;padding:8px 12px}
      button{cursor:pointer;border:1px solid #c8cfdb;background:#f4f6f9;color:#202632}button:disabled{cursor:wait;opacity:.6}
      input{box-sizing:border-box;width:100%;border:1px solid #9ca8ba;margin:8px 0}label{display:block}form{margin-top:12px}
      [hidden]{display:none!important}.actions{display:flex;gap:8px;flex-wrap:wrap}.error{color:#a62121}
    </style>
    <section hidden aria-live="polite">
      <strong></strong><p class="detail"></p><p class="error" role="alert" hidden></p>
      <div class="actions"><button class="retry" type="button">重试下载</button><button class="refresh" type="button" hidden>刷新页面</button></div>
      <form hidden><label for="webui-token">WebUI Token</label><input id="webui-token" type="password" autocomplete="off" required><button type="submit">登录并重试</button></form>
    </section>`
  document.body.append(host)
  const section = shadow.querySelector('section')
  const title = shadow.querySelector('strong')
  const detail = shadow.querySelector('.detail')
  const error = shadow.querySelector('.error')
  const retry = shadow.querySelector('.retry')
  const refresh = shadow.querySelector('.refresh')
  const form = shadow.querySelector('form')
  const token = shadow.querySelector('input')
  let encounteredUpdate = false
  let timer
  let active = false
  let submitting = false
  let stopped = false
  let controller = new AbortController()

  /** 后台任务仍在运行时持续禁用按钮，不能随 POST 请求结束而提前启用。 */
  function updateButtons() {
    retry.disabled = active || submitting
    form.querySelector('button').disabled = active || submitting
  }

  /** 错误信息使用纯文本，网络返回的内容不会作为 HTML 执行。 */
  function showError(message) {
    error.textContent = message
    error.hidden = false
    section.hidden = false
  }

  /** 仅活动下载期间轮询；状态查询不会发起新的下载。 */
  async function update() {
    if (stopped) return
    clearTimeout(timer)
    try {
      const response = await fetch('/api/webui/frontend/status', { cache: 'no-store', credentials: 'same-origin', signal: controller.signal })
      if (!response.ok) throw new Error('无法读取页面状态，请刷新后重试。')
      const data = await response.json()
      if (stopped) return
      active = ['checking', 'downloading', 'installing'].includes(data.state)
      encounteredUpdate ||= data.state !== 'ready'
      const needsRefresh = Boolean(placeholderTitle) || pageVersion !== data.current_id || encounteredUpdate
      section.hidden = data.state === 'ready' && !needsRefresh
      title.textContent = data.state === 'ready' ? '新版页面已准备好' : data.using_old_page ? '管理页面尚未更新' : data.state === 'missing' ? '页面下载未完成' : '正在准备管理页面'
      if (placeholderTitle && data.state === 'ready') placeholderTitle.textContent = '管理页面已准备好'
      detail.textContent = data.state === 'ready'
        ? '请先保存正在编辑的内容，再刷新页面。'
        : data.error || ({ checking: '正在检查本地页面资源…', downloading: '正在下载当前版本的页面…', installing: '正在校验并安装页面…' }[data.state] || '当前没有可用页面。Bot 会继续运行。')
      retry.hidden = data.state === 'ready'
      updateButtons()
      refresh.hidden = data.state !== 'ready'
      if (active) timer = setTimeout(update, 1000)
    } catch (exception) {
      if (!stopped) showError(exception.message)
    }
  }

  /** 发起认证重试；未登录时原地显示登录表单，避免依赖缺失的页面。 */
  async function requestRetry() {
    if (stopped || active || submitting) return
    error.hidden = true
    submitting = true
    updateButtons()
    try {
      const response = await fetch('/api/webui/frontend/retry', { method: 'POST', credentials: 'same-origin', signal: controller.signal })
      if (stopped) return
      if (response.status === 401) {
        form.hidden = false
        token.focus()
      } else if (!response.ok) {
        throw new Error('无法开始下载，请稍后重试。')
      } else {
        form.hidden = true
        await update()
      }
    } catch (exception) {
      if (!stopped) showError(exception.message)
    } finally {
      submitting = false
      updateButtons()
    }
  }

  retry.addEventListener('click', requestRetry)
  refresh.addEventListener('click', () => location.reload())
  form.addEventListener('submit', async (event) => {
    event.preventDefault()
    if (stopped || active || submitting) return
    submitting = true
    updateButtons()
    error.hidden = true
    try {
      const response = await fetch('/api/webui/auth/verify', {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: token.value }),
        signal: controller.signal,
      })
      const result = await response.json()
      if (stopped) return
      if (!response.ok || !result.valid) throw new Error(result.message || '登录失败，请检查 Token。')
      token.value = ''
      submitting = false
      await requestRetry()
    } catch (exception) {
      if (!stopped) showError(exception.message)
    } finally {
      submitting = false
      updateButtons()
    }
  })
  window.addEventListener('pagehide', () => {
    stopped = true
    clearTimeout(timer)
    controller.abort()
  })
  // 浏览器后退缓存会复用同一页面，恢复时重新建立请求控制器和状态读取。
  window.addEventListener('pageshow', (event) => {
    if (event.persisted && stopped) {
      stopped = false
      submitting = false
      controller = new AbortController()
      update()
    }
  })
  update()
})()

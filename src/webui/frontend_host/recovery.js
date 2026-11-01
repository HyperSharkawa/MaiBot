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
      strong{display:block;font-size:16px}p{margin:8px 0;overflow-wrap:anywhere}button,input,select{font:inherit;border-radius:6px;padding:8px 12px}
      button{cursor:pointer;border:1px solid #c8cfdb;background:#f4f6f9;color:#202632}button:disabled{cursor:wait;opacity:.6}
      input,select{box-sizing:border-box;width:100%;border:1px solid #9ca8ba;margin:8px 0}label{display:block}form{margin-top:12px}
      progress{width:100%;height:12px;accent-color:#3979d4}small{display:block;color:#505b6b}.transfer{margin:12px 0}
      [hidden]{display:none!important}.actions{display:flex;gap:8px;flex-wrap:wrap}.error{color:#a62121}
    </style>
    <section hidden aria-live="polite">
      <strong></strong><p class="detail"></p><p class="error" role="alert" hidden></p>
      <div class="transfer" hidden><p class="source"></p><progress aria-label="页面下载进度" max="100"></progress><small class="bytes"></small><small class="slow"></small></div>
      <label class="sources" hidden>下载源<select aria-label="下载源"><option value="auto">自动选择</option></select></label>
      <div class="actions"><button class="retry" type="button">重试下载</button><button class="refresh" type="button" hidden>刷新页面</button></div>
      <form hidden><label for="webui-token">WebUI Token</label><input id="webui-token" type="password" autocomplete="off" required><button type="submit">登录</button></form>
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
  const sourceSelector = shadow.querySelector('select')
  const sourceLabel = shadow.querySelector('.sources')
  const transfer = shadow.querySelector('.transfer')
  const sourceText = shadow.querySelector('.source')
  const progress = shadow.querySelector('progress')
  const bytes = shadow.querySelector('.bytes')
  const slow = shadow.querySelector('.slow')
  let encounteredUpdate = false
  let timer
  let active = false
  let submitting = false
  let stopped = false
  let controller = new AbortController()
  let sourcesLoaded = false
  let needsLogin = false

  /** 后台任务仍在运行时持续禁用按钮，不能随 POST 请求结束而提前启用。 */
  function updateButtons() {
    retry.disabled = active || submitting
    form.querySelector('button').disabled = active || submitting
    sourceSelector.disabled = active || submitting
    if (needsLogin) retry.textContent = '登录后选择下载源'
    else retry.textContent = '重试下载'
  }

  /** 使用十进制单位，与低速阈值 5,000 字节/秒保持一致。 */
  function size(value) {
    if (value >= 1000000) return `${(value / 1000000).toFixed(1)} MB`
    return `${(value / 1000).toFixed(1)} KB`
  }

  /** 只显示当前源的字节和速度；无总大小时使用原生不定进度条。 */
  function renderProgress(data) {
    const download = data.download
    transfer.hidden = !download || data.state === 'ready'
    if (!download) return
    sourceText.textContent = `当前下载源：${download.source_name} · 第 ${download.source_index}/${download.source_count} 个`
    progress.hidden = !active
    const knownSize = download.total_bytes > 0
    if (knownSize) progress.value = Math.min(100, download.received_bytes / download.total_bytes * 100)
    else progress.removeAttribute('value')
    const percentage = knownSize ? `（${Math.min(100, download.received_bytes / download.total_bytes * 100).toFixed(0)}%）` : ''
    bytes.textContent = download.phase === 'archive' || download.phase === 'validating'
      ? `已下载 ${size(download.received_bytes)}${knownSize ? ` / ${size(download.total_bytes)}` : ''}${percentage} · 速度 ${size(download.bytes_per_second)}/s`
      : ''
    slow.textContent = active && download.phase === 'archive' && download.low_speed_seconds > 0
      ? `下载较慢，持续低于 5 KB/s 达 60 秒后将${download.automatic ? '切换下载源' : '结束本次尝试'}。`
      : ''
  }

  /** 认证后读取选择项；读取失败不触发下载，不自动改写用户选择。 */
  async function loadSources() {
    const response = await fetch('/api/webui/frontend/sources', { cache: 'no-store', credentials: 'same-origin', signal: controller.signal })
    if (stopped) return
    if (response.status === 401) {
      needsLogin = true
      sourcesLoaded = true
      return
    }
    if (!response.ok) throw new Error('无法读取下载源，请稍后重试。')
    const data = await response.json()
    if (stopped) return
    const selected = sourceSelector.value
    sourceSelector.replaceChildren(new Option('自动选择', 'auto'))
    for (const source of data.sources) sourceSelector.add(new Option(source.name, source.id))
    if ([...sourceSelector.options].some(option => option.value === selected)) sourceSelector.value = selected
    sourcesLoaded = true
    needsLogin = false
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
        : data.error || (data.state === 'downloading'
          ? ({ connecting: '正在连接下载源…', checksum: '正在获取校验文件…', archive: '正在下载当前版本的页面…', validating: '正在校验下载文件…' }[data.download?.phase] || '正在下载当前版本的页面…')
          : ({ checking: '正在检查本地页面资源…', installing: '正在校验并安装页面…' }[data.state] || '当前没有可用页面。Bot 会继续运行。'))
      renderProgress(data)
      if (!active && data.state !== 'ready' && !sourcesLoaded) await loadSources()
      if (stopped) return
      sourceLabel.hidden = needsLogin || !sourcesLoaded || data.state === 'ready'
      retry.hidden = data.state === 'ready'
      if (data.state === 'ready') form.hidden = true
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
    if (needsLogin) {
      // 用户可能已经通过旧页面登录，重新检查 Cookie 后再决定是否需要 Token。
      submitting = true
      updateButtons()
      try {
        await loadSources()
        if (stopped) return
        if (needsLogin) {
          form.hidden = false
          token.focus()
        } else {
          await update()
        }
      } catch (exception) {
        if (!stopped) showError(exception.message)
      } finally {
        submitting = false
        updateButtons()
      }
      return
    }
    error.hidden = true
    submitting = true
    updateButtons()
    try {
      const response = await fetch('/api/webui/frontend/retry', {
        method: 'POST', credentials: 'same-origin', signal: controller.signal,
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source_id: sourceSelector.value }),
      })
      if (stopped) return
      if (response.status === 401) {
        needsLogin = true
        form.hidden = false
        token.focus()
      } else if (!response.ok) {
        if (response.status === 409) {
          await update()
          throw new Error('本次下载仍在进行，请完成后再选择其他源。')
        }
        if (response.status === 400) {
          await loadSources()
          throw new Error('下载源已失效或被禁用，请重新选择。')
        }
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
      form.hidden = true
      await loadSources()
      await update()
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

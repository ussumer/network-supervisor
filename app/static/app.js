/* clash-radar dashboard: no dependencies, hand-rolled SVG chart. */
(() => {
  const $ = (sel) => document.querySelector(sel);
  const RANGES = { 60: '1h', 360: '6h', 1440: '24h', 10080: '7d' };
  const state = { rangeMinutes: 60, loading: false };

  const UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];

  function fmtBytes(n, digits) {
    n = Number(n) || 0;
    let i = 0;
    while (n >= 1024 && i < UNITS.length - 1) { n /= 1024; i += 1; }
    const d = digits !== undefined ? digits : (i === 0 ? 0 : n >= 100 ? 0 : n >= 10 ? 1 : 2);
    return `${n.toFixed(d)} ${UNITS[i]}`;
  }

  const fmtRate = (n) => `${fmtBytes(n, 1)}/s`;
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));

  function hhmm(ts, withDate) {
    const d = new Date(ts * 1000);
    const p = (x) => String(x).padStart(2, '0');
    const time = `${p(d.getHours())}:${p(d.getMinutes())}`;
    return withDate ? `${p(d.getMonth() + 1)}-${p(d.getDate())} ${time}` : time;
  }

  async function getJSON(url) {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) throw new Error(`${url} → HTTP ${res.status}`);
    return res.json();
  }

  function toast(msg, isError) {
    const el = $('#toast');
    el.textContent = msg;
    el.className = `toast show${isError ? ' err' : ''}`;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.className = 'toast'; }, 2600);
  }

  // ---------------------------------------------------------------- overview
  function renderOverview(d) {
    const t = d.today;
    $('#todayBilled').textContent = fmtBytes(t.billed);
    $('#todayHint').innerHTML =
      `原始 ${fmtBytes(t.raw)} · 直连 ${fmtBytes(t.direct)}`;

    const c = d.cycle;
    $('#cycleUsed').textContent = fmtBytes(c.used);
    $('#cycleHint').textContent =
      `额度 ${fmtBytes(c.budget)} · 日均 ${fmtBytes(c.avg_day)}`;

    $('#remain').textContent = fmtBytes(c.remaining);
    $('#remainHint').textContent = Number.isFinite(c.days_left)
      ? `按当前速率预计 ${c.days_left} 天用完（已用 ${c.pct}%）`
      : `已用 ${c.pct}% · 数据积累 1 小时后给出预测`;

    const bar = $('#bar');
    const pct = Math.min(100, c.pct || 0);
    bar.classList.toggle('warn', pct >= 80);
    bar.querySelector('i').style.width = `${pct}%`;

    $('#rate').textContent = fmtRate((d.rates.up || 0) + (d.rates.down || 0));
    $('#upRate').textContent = fmtBytes(d.rates.up || 0, 1);
    $('#downRate').textContent = fmtBytes(d.rates.down || 0, 1);

    const st = d.collector;
    const dot = $('#dot');
    if (st.ok) {
      dot.className = 'dot ok';
      $('#statusText').textContent = `采集中 · ${st.connections} 条连接 · 每 ${st.poll_interval}s`;
      $('#metaText').textContent = `secret: ${st.secret}`;
    } else {
      dot.className = 'dot bad';
      $('#statusText').textContent = `API 异常：${st.last_error || '连接失败'}`;
      $('#metaText').textContent = st.api || '';
    }
  }

  // ------------------------------------------------------------------- chart
  function drawSeries(points) {
    const svg = $('#chart');
    const W = 960, H = 240, padL = 58, padR = 12, padT = 12, padB = 26;
    if (!points || !points.length) {
      svg.innerHTML = '<text x="480" y="120" fill="#6e7681" text-anchor="middle" font-size="13">暂无数据，等待采集…</text>';
      return;
    }
    const n = points.length;
    const peak = Math.max(1, ...points.map((p) => Math.max(p.billed, p.up, p.down)));
    const x = (i) => padL + (W - padL - padR) * (n === 1 ? 0.5 : i / (n - 1));
    const y = (v) => H - padB - (H - padT - padB) * (Math.max(0, v) / peak);

    const path = (key) => points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join(' ');
    const area = `${path('down')} L${x(n - 1).toFixed(1)},${H - padB} L${x(0).toFixed(1)},${H - padB} Z`;

    let grid = '';
    for (let i = 0; i <= 4; i += 1) {
      const gv = (peak / 4) * i;
      const gy = y(gv).toFixed(1);
      grid += `<line x1="${padL}" y1="${gy}" x2="${W - padR}" y2="${gy}" stroke="#21262d" stroke-width="1"/>`;
      grid += `<text x="${padL - 8}" y="${Number(gy) + 4}" fill="#6e7681" font-size="10" text-anchor="end">${fmtBytes(gv, 1)}</text>`;
    }

    const ticks = 5;
    let labels = '';
    for (let i = 0; i < ticks; i += 1) {
      const idx = Math.round((n - 1) * (i / (ticks - 1)));
      const p = points[idx];
      if (!p) continue;
      labels += `<text x="${x(idx).toFixed(1)}" y="${H - 8}" fill="#6e7681" font-size="10" text-anchor="middle">${hhmm(p.t, state.rangeMinutes > 1440)}</text>`;
    }

    svg.innerHTML = `
      ${grid}
      <path d="${area}" fill="rgba(88,166,255,0.16)"/>
      <path d="${path('down')}" fill="none" stroke="#58a6ff" stroke-width="1.6"/>
      <path d="${path('up')}" fill="none" stroke="#f0883e" stroke-width="1.4"/>
      <path d="${path('billed')}" fill="none" stroke="#3fb950" stroke-width="1.8"/>
      ${labels}
    `;
  }

  // ------------------------------------------------------------------- tables
  function renderTop(selector, items) {
    const tbody = document.querySelector(`${selector} tbody`);
    if (!items || !items.length) {
      tbody.innerHTML = '<tr><td class="empty">暂无数据</td></tr>';
      return;
    }
    tbody.innerHTML = items.map((it) => {
      const billed = it.billed || 0;
      const direct = it.direct || 0;
      const total = billed || direct || it.raw || 0;
      const badges = [];
      if (it.ratio && it.ratio !== 1) badges.push(`<span class="ratio">${it.ratio}x</span>`);
      if (!billed && direct) badges.push('<span class="tag">直连·不计费</span>');
      return `<tr>
        <td class="name" title="${esc(it.name)}">${esc(it.name)}${badges.join('')}</td>
        <td class="num">${fmtBytes(total)}</td>
        <td style="width:26%"><div class="share"><i style="width:${it.share || 0}%"></i></div></td>
      </tr>`;
    }).join('');
  }

  function renderLive(data) {
    const tbody = document.querySelector('#liveTable tbody');
    const rows = data.connections || [];
    $('#liveHint').textContent = rows.length
      ? `${rows.length} 条活动连接 · ↑${fmtRate(data.rates.up)} ↓${fmtRate(data.rates.down)}`
      : '暂无活动连接';
    if (!rows.length) {
      tbody.innerHTML = '<tr><td class="empty">当前没有经过代理的连接</td></tr>';
      return;
    }
    tbody.innerHTML = rows.slice(0, 40).map((c) => `<tr>
      <td class="name" title="${esc(c.process)}">${esc(c.process)}</td>
      <td class="name" title="${esc(c.host)}">${esc(c.host)}${c.port ? ':' + esc(c.port) : ''}</td>
      <td class="name">${esc((c.chains || []).join(' → ') || c.node)}</td>
      <td class="num">↑${fmtBytes(c.up)} ↓${fmtBytes(c.down)}</td>
    </tr>`).join('');
  }

  // ------------------------------------------------------------------ refresh
  async function refreshOverview() {
    try {
      const data = await getJSON('/api/overview');
      renderOverview(data);
    } catch (err) {
      $('#dot').className = 'dot bad';
      $('#statusText').textContent = `看板异常：${err.message}`;
    }
  }

  async function refreshSeries() {
    try {
      const data = await getJSON(`/api/series?minutes=${state.rangeMinutes}`);
      drawSeries(data.points);
    } catch (err) { /* keep last chart */ }
  }

  async function refreshLive() {
    try {
      renderLive(await getJSON('/api/live?limit=60'));
    } catch (err) { /* ignore */ }
  }

  async function refreshTops() {
    const hours = state.rangeMinutes / 60;
    const label = RANGES[state.rangeMinutes] || `${hours}h`;
    try {
      const [p, h, n, r, d] = await Promise.all([
        getJSON(`/api/top?dim=process&hours=${hours}&limit=10`),
        getJSON(`/api/top?dim=host&hours=${hours}&limit=10`),
        getJSON(`/api/top?dim=node&hours=${hours}&limit=10`),
        getJSON(`/api/top?dim=rule&hours=${hours}&limit=10`),
        getJSON(`/api/devices?hours=${hours}`),
      ]);
      $('#ttlProcess').textContent = `Top 进程（近 ${label}）`;
      $('#ttlHost').textContent = `Top 域名（近 ${label}）`;
      $('#ttlNode').textContent = `Top 节点（近 ${label}）`;
      $('#ttlRule').textContent = `Top 规则（近 ${label}）`;
      $('#ttlDevice').textContent = `设备（近 ${label}）`;
      const devices = d.items || [];
      $('#deviceHint').textContent = devices.length > 1
        ? `${devices.length} 台设备合计计入下方总额`
        : '只有本机数据；手机端跑 android-agent.py 后这里会多一行';
      renderTop('#topDevice', devices);
      renderTop('#topProcess', p.items);
      renderTop('#topHost', h.items);
      renderTop('#topNode', n.items);
      renderTop('#topRule', r.items);
    } catch (err) { /* ignore */ }
  }

  // ----------------------------------------------------------------- settings
  async function loadSettings() {
    try {
      const s = await getJSON('/api/settings');
      $('#fBudget').value = s.budget_gb;
      $('#fBilling').value = s.billing_mode;
      $('#fCycle').value = s.cycle_mode;
      $('#fCycleDay').value = s.cycle_mode === 'rolling' ? s.cycle_days : s.cycle_start_day;
      $('#fRatios').value = (s.ratios || []).map((r) => `${r.pattern} ${r.ratio}`).join('\n');
    } catch (err) { toast('读取设置失败', true); }
  }

  async function saveSettings() {
    const isRolling = $('#fCycle').value === 'rolling';
    const dayVal = Number($('#fCycleDay').value) || 1;
    const ratios = $('#fRatios').value
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const parts = line.split(/\s+/);
        return { pattern: parts[0], ratio: Number(parts[1] || 1) };
      });
    const body = {
      budget_gb: Number($('#fBudget').value) || 100,
      billing_mode: $('#fBilling').value,
      cycle_mode: $('#fCycle').value,
      cycle_start_day: isRolling ? 1 : dayVal,
      cycle_days: isRolling ? dayVal : 30,
      ratios,
    };
    try {
      const res = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await res.json();
      toast('已保存，历史数据已按新倍率重算');
      await Promise.all([refreshOverview(), refreshSeries(), refreshTops()]);
    } catch (err) {
      toast(`保存失败：${err.message}`, true);
    }
  }

  // -------------------------------------------------------------------- boot
  function setActiveTab() {
    document.querySelectorAll('#rangeTabs button').forEach((btn) => {
      btn.classList.toggle('active', Number(btn.dataset.min) === state.rangeMinutes);
    });
  }

  function bind() {
    document.querySelectorAll('#rangeTabs button').forEach((btn) => {
      btn.addEventListener('click', () => {
        state.rangeMinutes = Number(btn.dataset.min);
        setActiveTab();
        const url = new URL(location.href);
        url.searchParams.set('range', state.rangeMinutes);
        history.replaceState(null, '', url);
        refreshSeries();
        refreshTops();
      });
    });
    $('#btnSave').addEventListener('click', saveSettings);
    $('#btnReload').addEventListener('click', loadSettings);
    $('#fCycle').addEventListener('change', () => {
      $('#fCycleDay').title = $('#fCycle').value === 'rolling' ? '滚动天数' : '周期起始日（1-28）';
    });
  }

  function start() {
    if (new URLSearchParams(location.search).has('debug')) {
      setTimeout(() => {
        let worst = null;
        let max = 0;
        document.querySelectorAll('*').forEach((el) => {
          const rect = el.getBoundingClientRect();
          if (rect.right > max) {
            max = rect.right;
            worst = el;
          }
        });
        const desc = worst ? `${worst.tagName}.${String(worst.className).slice(0, 40)}` : '-';
        document.title = `iw=${innerWidth} sw=${document.documentElement.scrollWidth} bw=${document.body.scrollWidth} maxRight=${max.toFixed(0)} el=${desc}`;
      }, 1500);
    }
    const fromUrl = Number(new URLSearchParams(location.search).get('range'));
    if (RANGES[fromUrl]) state.rangeMinutes = fromUrl;
    setActiveTab();
    bind();
    refreshOverview().then(loadSettings);
    refreshSeries();
    refreshLive();
    refreshTops();
    setInterval(refreshOverview, 2000);
    setInterval(refreshLive, 2000);
    setInterval(refreshSeries, 15000);
    setInterval(refreshTops, 20000);
  }

  document.addEventListener('DOMContentLoaded', start);
})();

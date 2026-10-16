// 稳健再平衡追踪页
// fetch history.json from the same repo (relative URL works on GitHub Pages).

async function main() {
  let history = [];
  try {
    const res = await fetch('data/history.json', { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    history = await res.json();
  } catch (e) {
    document.getElementById('status-badge').innerHTML =
      `<span class="badge badge-err">⚠ 加载失败: ${e.message}</span>`;
    document.getElementById('curve').textContent = '';
    return;
  }

  if (!Array.isArray(history) || history.length === 0) {
    document.getElementById('status-badge').innerHTML =
      `<span class="badge badge-info">暂无数据</span>` +
      `<span class="muted">(GitHub Action 还没跑过, 或 <code>data/history.json</code> 为空)</span>`;
    document.getElementById('curve').textContent = '';
    return;
  }

  history.sort((a, b) => a.date.localeCompare(b.date));
  const latest = history[history.length - 1];

  renderStatus(latest);
  renderCurve(history);
  renderChart();   // 缓存破坏: 给 grid_chart.png 加时间戳
  renderLatest(latest);
  renderTable(history);
}

function renderChart() {
  // 加 ?t=Date.now() 让浏览器每次访问都重新拉图, 不会用旧缓存
  const img = document.getElementById('grid-chart');
  if (img) img.src = `data/grid_chart.png?t=${Date.now()}`;
}

function renderStatus(rec) {
  const grid = rec.grid_signals || [];
  const anyBuy = grid.some(s => s.signal === 'buy');
  const anySell = grid.some(s => s.signal === 'sell');
  const anyAction = rec.triggered || anyBuy || anySell;

  const cls = anyAction ? 'badge-warn' : 'badge-ok';
  const txt = anyAction ? '⚠ 有操作' : '✓ 未触发';
  document.getElementById('status-badge').innerHTML =
    `<span class="badge ${cls}">${txt}</span> ` +
    `<span>${rec.date}</span> ` +
    `<span>总市值 <strong>¥${Math.round(rec.total).toLocaleString()}</strong></span> ` +
    `<span class="muted">(ETF ¥${Math.round(rec.etf_value).toLocaleString()} + 现金 ¥${Math.round(rec.cash).toLocaleString()})</span>`;
}

function renderCurve(history) {
  const chart = echarts.init(document.getElementById('curve'));
  const dates = history.map(r => r.date);
  const totals = history.map(r => r.total);

  chart.setOption({
    tooltip: {
      trigger: 'axis',
      formatter: (params) => {
        const p = params[0];
        const rec = history[p.dataIndex];
        let html = `<b>${p.axisValue}</b><br/>` +
                   `总市值: ¥${Math.round(rec.total).toLocaleString()}<br/>`;
        rec.positions.forEach((pos, i) => {
          const w = (rec.weights[i] * 100).toFixed(2);
          html += `${pos.name}: ${w}%<br/>`;
        });
        html += `信号: ${rec.triggered ? '⚠ 触发' : '✓ 未触发'}`;
        return html;
      },
    },
    grid: { left: 60, right: 30, top: 40, bottom: 60 },
    xAxis: {
      type: 'category',
      data: dates,
      axisLabel: { rotate: 30 },
    },
    yAxis: {
      type: 'value',
      name: '总市值 (¥)',
      scale: true,
      axisLabel: {
        formatter: (v) => v.toLocaleString(),
      },
    },
    series: [{
      name: '总市值',
      type: 'line',
      data: totals,
      smooth: true,
      showSymbol: false,
      areaStyle: { opacity: 0.15, color: '#c29438' },
      lineStyle: { width: 2, color: '#c29438' },
      itemStyle: { color: '#c29438' },
      markPoint: {
        symbol: 'pin',
        data: history
          .map((r, i) => r.triggered ? { coord: [dates[i], totals[i]], itemStyle: { color: '#c0392b' } } : null)
          .filter(x => x),
      },
    }],
  });

  window.addEventListener('resize', () => chart.resize());
}

function renderLatest(rec) {
  const rows = rec.positions.map((p, i) => {
    const w = rec.weights[i];
    const delta = (w - rec.target_w) * 100;
    const over = Math.abs(delta) > rec.threshold_pp;
    return `<tr>
      <td>${p.name}</td>
      <td>${p.code}</td>
      <td>${Math.round(p.shares).toLocaleString()}</td>
      <td>¥${p.price.toFixed(3)}</td>
      <td>¥${Math.round(p.value).toLocaleString()}</td>
      <td>${(w * 100).toFixed(2)}%</td>
      <td class="${over ? 'warn' : ''}">${delta >= 0 ? '+' : ''}${delta.toFixed(2)}pp</td>
    </tr>`;
  }).join('');

  const grid = rec.grid_signals || [];
  const gridRows = grid.map(s => {
    const sig = s.signal;
    let sigClass = 'ok', sigText = '— 持有';
    if (sig === 'buy') { sigClass = 'warn'; sigText = '⚠ 买入'; }
    else if (sig === 'sell') { sigClass = 'ok'; sigText = '✓ 止盈'; }
    // 距下次触发: 待机=距开仓(距窗口最低), 持仓=距止盈
    const trigPct = s.trigger_pct ?? 0;
    const trigKind = s.trigger_kind || 'open';
    const trigPrefix = trigKind === 'tp' ? '距止盈' : '距开仓';
    let trigClass = 'ok';
    if (trigKind === 'tp' && trigPct <= 0.5) trigClass = 'warn';
    if (trigKind === 'open') {
      if (trigPct <= 0.5) trigClass = 'warn';
      else if (trigPct <= 2.0) trigClass = 'warn';
    }
    return `<tr>
      <td>${s.name}</td>
      <td class="${sigClass}">${sigText}</td>
      <td>${s.num_buys}</td>
      <td>¥${s.avg_cost.toFixed(3)}</td>
      <td>¥${s.price.toFixed(3)}</td>
      <td class="${s.unrealized_pnl >= 0 ? 'ok' : 'warn'}">${s.return_pct >= 0 ? '+' : ''}${s.return_pct.toFixed(2)}%</td>
      <td class="${trigClass}">${trigPrefix} ${trigPct >= 0 ? '+' : ''}${trigPct.toFixed(2)}%</td>
    </tr>`;
  }).join('');

  const layer = grid[0]?.layer_yuan || 10000;

  document.getElementById('latest-detail').innerHTML = `
    <p class="muted">价格日期: ${rec.positions[0]?.date || '?'}</p>

    <h3 style="margin-top:1em;">3pp 再平衡</h3>
    <table>
      <thead>
        <tr>
          <th>名称</th><th>代码</th><th>数量</th><th>价格</th><th>市值</th><th>权重</th><th>偏离</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>

    ${grid.length ? `
    <h3 style="margin-top:1.5em;">复合网格 <span class="muted" style="font-size:12px;font-weight:normal;">(加仓额 ¥${layer.toLocaleString()})</span></h3>
    <table>
      <thead>
        <tr>
          <th>名称</th><th>信号</th><th>层数</th><th>均价</th><th>现价</th><th>浮盈%</th><th>距下次触发</th>
        </tr>
      </thead>
      <tbody>${gridRows}</tbody>
    </table>
    ` : ''}
  `;
}

function renderTable(history) {
  const rows = history.slice().reverse().map(r =>
    `<tr>
      <td>${r.date}</td>
      <td>¥${Math.round(r.total).toLocaleString()}</td>
      <td class="${r.triggered ? 'warn' : 'ok'}">${r.triggered ? '⚠ 触发' : '✓'}</td>
    </tr>`
  ).join('');
  document.getElementById('history-table').innerHTML = `
    <table>
      <thead>
        <tr><th>日期</th><th>总市值</th><th>信号</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

// ---- Holdings editor (POSTs to repository_dispatch) ----
// GitHub's /dispatches endpoint always requires authentication, even on
// public repos (changed 2022). We store a fine-grained PAT in
// localStorage so the user only has to paste it once per browser.
//
// PAT requirements:
//   - Fine-grained personal access token
//   - Repository access: only fantasy-mark/hongli-rebalance
//   - Permissions: Contents = Read & Write
//   - (Optionally) set an expiration (recommended ≤ 90 days)
//   - Get one: https://github.com/settings/personal-access-tokens/new
//
// The PAT is stored unencrypted in localStorage. Any JS on this page
// (including browser extensions) can read it. Only use this on a
// trusted personal device.

const REPO = "fantasy-mark/hongli-rebalance";
const HOLDINGS_URL = "data/holdings.json";
const DISPATCH_URL = `https://api.github.com/repos/${REPO}/dispatches`;
const PAT_STORAGE_KEY = "hongli_rebalance_pat";

// Lightweight PAT shape validation (catches typos before hitting the API).
// Accepts both classic `ghp_*` and fine-grained `github_pat_*` formats.
function isPlausiblePat(s) {
  if (typeof s !== "string") return false;
  s = s.trim();
  if (s.length < 30 || s.length > 255) return false;
  return /^(ghp_|github_pat_|ghs_|gho_)[A-Za-z0-9_]+$/.test(s);
}

function getPat() {
  try {
    const v = localStorage.getItem(PAT_STORAGE_KEY);
    return v && v.trim() ? v.trim() : null;
  } catch (e) {
    return null;
  }
}

function setPat(pat) {
  try {
    if (pat) localStorage.setItem(PAT_STORAGE_KEY, pat);
    else    localStorage.removeItem(PAT_STORAGE_KEY);
  } catch (e) {
    console.warn("localStorage unavailable:", e);
  }
  updatePatBadge();
}

function updatePatBadge() {
  const badge = document.getElementById("pat-badge");
  if (!badge) return;
  const has = !!getPat();
  // 外部链接常驻：跳转去配 PAT / cron-job
  const githubLink = `<a href="https://github.com/settings/personal-access-tokens/21045200" target="_blank" rel="noopener" class="btn-link">github ↗</a>`;
  const cronLink = `<a href="https://console.cron-job.org/" target="_blank" rel="noopener" class="btn-link">cron-job ↗</a>`;
  const patPart = has
    ? `🔑 PAT 已设置 <button type="button" id="pat-clear-btn" class="btn-link">清除</button>`
    : `⚠ <button type="button" id="pat-set-btn" class="btn-link">设置 PAT</button> (保存前需要)`;
  badge.innerHTML = `${patPart} <span class="muted">·</span> ${githubLink} <span class="muted">·</span> ${cronLink}`;
  const setBtn = document.getElementById("pat-set-btn");
  if (setBtn) setBtn.addEventListener("click", promptForPat);
  const clearBtn = document.getElementById("pat-clear-btn");
  if (clearBtn) clearBtn.addEventListener("click", () => { setPat(null); });
}

function promptForPat() {
  const cur = getPat() || "";
  const v = window.prompt(
    "粘贴 GitHub Fine-grained PAT\n" +
    "(Repository access: 仅 fantasy-mark/hongli-rebalance\n" +
    " Permissions: Contents = Read & Write)\n" +
    "创建: https://github.com/settings/personal-access-tokens/new",
    cur
  );
  if (v === null) return;   // cancelled
  const trimmed = v.trim();
  if (!trimmed) { setPat(null); return; }
  if (!isPlausiblePat(trimmed)) {
    alert("格式不像 GitHub PAT (应以 ghp_/github_pat_/ghs_/gho_ 开头). 已忽略, 请重试.");
    return;
  }
  setPat(trimmed);
}

// Known ETF catalog surfaced in the "code" dropdown (mirrors src/record.py ETF_DEFS).
// Users can still type a custom 6-digit code, but the workflow will reject unknown ones.
const ETF_CATALOG = {
  "515180": "红利ETF易方达",
  "159937": "黄金ETF博时",
  "511380": "可转债ETF博时",
  "513300": "纳斯达克ETF华夏",
};

let originalHoldings = null;   // last fetched snapshot for cash reference

function renderEditor(holdings) {
  originalHoldings = JSON.parse(JSON.stringify(holdings));  // deep copy
  // Show current cash at the top of the cash editor.
  const cashEl = document.getElementById('cash-balance');
  if (cashEl) cashEl.textContent = (holdings.cash ?? 0).toFixed(2);

  // Start with ONE empty row in each section.
  const tBody = document.getElementById('trade-tbody');
  tBody.innerHTML = '';
  tBody.appendChild(makeTradeRow());
  const cBody = document.getElementById('cash-tbody');
  cBody.innerHTML = '';
  cBody.appendChild(makeCashRow());

  setStatus('trade', '');
  setStatus('cash',  '');
  updateCashPreview();
}

function setStatus(kind, msg, cls) {
  const el = document.getElementById(kind + '-status');
  if (!el) return;
  el.textContent = msg || '';
  el.className = cls || 'muted';
}

function escapeAttr(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// ---- Trade editor (buy / sell) ----

function makeTradeRow(t = {}) {
  // Trade-row shape: {date, side, code, shares, price, amount(=shares*price read-only), note}
  const tr = document.createElement('tr');
  const today = new Date().toISOString().slice(0, 10);
  const side = (t.side === 'sell') ? 'sell' : 'buy';
  tr.innerHTML = `
    <td><input data-k="date" type="date" value="${escapeAttr(t.date || today)}" style="width:9em;" /></td>
    <td>
      <select data-k="side" style="padding:0.2em;">
        <option value="buy"  ${side === 'buy'  ? 'selected' : ''}>买入</option>
        <option value="sell" ${side === 'sell' ? 'selected' : ''}>卖出</option>
      </select>
    </td>
    <td>
      <input data-k="code" type="text" list="etf-codes"
             value="${escapeAttr(t.code || '')}"
             placeholder="6 位代码" maxlength="6" pattern="\\d{6}" style="width:6em;" />
      <datalist id="etf-codes">
        ${Object.entries(ETF_CATALOG).map(([c, n]) => `<option value="${c}">${n}</option>`).join('')}
      </datalist>
    </td>
    <td><input data-k="shares" type="number" value="${t.shares ?? ''}" min="0" step="1" style="text-align:right;width:7em;" placeholder="股数" /></td>
    <td><input data-k="price"  type="number" value="${t.price  ?? ''}" min="0" step="0.0001" style="text-align:right;width:7em;" placeholder="¥" /></td>
    <td><span data-k="amount-readout" class="muted" style="display:inline-block;width:7em;text-align:right;">—</span></td>
    <td><input data-k="note"   type="text"   value="${escapeAttr(t.note || '')}" placeholder="备注" style="width:12em;" /></td>
    <td class="del-cell"><button type="button" class="row-del-btn" title="删除该行">✕</button></td>
  `;
  // 实时刷新 read-only 金额 + 现金预览
  const updateAmount = () => {
    const sh = parseFloat(tr.querySelector('[data-k="shares"]').value) || 0;
    const px = parseFloat(tr.querySelector('[data-k="price"]').value)  || 0;
    const readout = tr.querySelector('[data-k="amount-readout"]');
    if (sh > 0 && px > 0) {
      readout.textContent = '¥' + (sh * px).toFixed(2);
    } else {
      readout.textContent = '—';
    }
    updateCashPreview();
  };
  tr.querySelector('[data-k="shares"]').addEventListener('input', updateAmount);
  tr.querySelector('[data-k="price"]').addEventListener('input',  updateAmount);
  tr.querySelector('[data-k="side"]').addEventListener('change',  updateCashPreview);
  tr.querySelector('.row-del-btn').addEventListener('click', () => {
    tr.remove();
    updateCashPreview();
  });
  return tr;
}

function collectTradeRows() {
  // Returns array of validated trade {side, date, code, shares, price, note}
  // or null if any non-blank row is invalid (rows are marked red).
  const trs = [...document.querySelectorAll('#trade-tbody tr')];
  let ok = true;
  const out = [];
  trs.forEach((tr) => {
    const get = k => (tr.querySelector(`[data-k="${k}"]`) || {}).value || '';
    const side = get('side').trim();
    const date = get('date').trim();
    const code = get('code').trim();
    const shares = get('shares').trim();
    const price  = get('price').trim();
    const note   = get('note').trim();
    // Skip rows where the user hasn't entered any of the required trade
    // fields yet. (date defaults to today so we don't include it.)
    if (!code && !shares && !price && !note) return;

    const trade = { side, date };
    let rowOK = true;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) rowOK = false;
    if (!['buy', 'sell'].includes(side))    rowOK = false;
    if (!/^\d{6}$/.test(code))              rowOK = false;
    else trade.code = code;
    const s = parseFloat(shares);
    const p = parseFloat(price);
    if (!(s > 0)) rowOK = false; else trade.shares = s;
    if (!(p > 0)) rowOK = false; else trade.price  = p;
    if (note) trade.note = note;

    tr.style.outline = rowOK ? '' : '2px solid #c0392b';
    if (!rowOK) ok = false;
    if (rowOK) out.push(trade);
  });
  return ok ? out : null;
}

// ---- Cash editor (deposit / withdraw) ----

function makeCashRow(t = {}) {
  // Cash-row shape: {date, side, amount, note}
  const tr = document.createElement('tr');
  const today = new Date().toISOString().slice(0, 10);
  const side = (t.side === 'withdraw') ? 'withdraw' : 'deposit';
  tr.innerHTML = `
    <td><input data-k="date" type="date" value="${escapeAttr(t.date || today)}" style="width:9em;" /></td>
    <td>
      <select data-k="side" style="padding:0.2em;">
        <option value="deposit"  ${side === 'deposit'  ? 'selected' : ''}>入金</option>
        <option value="withdraw" ${side === 'withdraw' ? 'selected' : ''}>提现</option>
      </select>
    </td>
    <td><input data-k="amount" type="number" value="${t.amount ?? ''}" min="0" step="0.01" style="text-align:right;width:10em;" placeholder="¥" /></td>
    <td><input data-k="note"   type="text"   value="${escapeAttr(t.note || '')}" placeholder="备注" style="width:18em;" /></td>
    <td class="del-cell"><button type="button" class="row-del-btn" title="删除该行">✕</button></td>
  `;
  tr.querySelector('[data-k="amount"]').addEventListener('input', updateCashPreview);
  tr.querySelector('[data-k="side"]').addEventListener('change',  updateCashPreview);
  tr.querySelector('.row-del-btn').addEventListener('click', () => {
    tr.remove();
    updateCashPreview();
  });
  return tr;
}

function collectCashRows() {
  // Returns array of validated cash rows {side, date, amount, note}
  // or null if any non-blank row is invalid.
  const trs = [...document.querySelectorAll('#cash-tbody tr')];
  let ok = true;
  const out = [];
  trs.forEach((tr) => {
    const get = k => (tr.querySelector(`[data-k="${k}"]`) || {}).value || '';
    const side   = get('side').trim();
    const date   = get('date').trim();
    const amount = get('amount').trim();
    const note   = get('note').trim();
    // Skip rows where the user hasn't entered an amount yet — the date
    // defaults to today so a "blank" row is detected by missing amount/note.
    if (!amount && !note) return;

    const row = { side, date };
    let rowOK = true;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) rowOK = false;
    if (!['deposit', 'withdraw'].includes(side)) rowOK = false;
    const a = parseFloat(amount);
    if (!(a > 0)) rowOK = false;
    else row.amount = a;
    if (note) row.note = note;

    tr.style.outline = rowOK ? '' : '2px solid #c0392b';
    if (!rowOK) ok = false;
    if (rowOK) out.push(row);
  });
  return ok ? out : null;
}

// ---- Cash preview (shared by both editors) ----

function netTradeDelta(trades) {
  // Sum (signed cash) for trade rows. Buys cost cash (negative), sells earn cash (positive).
  return (trades || []).reduce((acc, t) => {
    if (t.side === 'buy')  return acc - t.shares * t.price;
    if (t.side === 'sell') return acc + t.shares * t.price;
    return acc;
  }, 0);
}

function netCashDelta(rows) {
  // Sum (signed cash) for deposit/withdraw rows. Deposits add, withdraws subtract.
  return (rows || []).reduce((acc, r) => {
    if (r.side === 'deposit')  return acc + r.amount;
    if (r.side === 'withdraw') return acc - r.amount;
    return acc;
  }, 0);
}

function getCurrentCash() {
  return parseFloat(document.getElementById('cash-balance')?.textContent) || 0;
}

function updateCashPreview() {
  // Live preview of the COMBINED effect of BOTH forms on cash.
  // (Per user decision: same-submit batch check; here we show the worst-case
  //  if the user hypothetically saved BOTH forms back-to-back.)
  const trades = collectTradeRows();   // null on validation error → ignore
  const cash   = collectCashRows();    // null on validation error → ignore
  if (trades === null || cash === null) {
    document.getElementById('trade-cash-preview').textContent = '';
    document.getElementById('cash-cash-preview').textContent  = '';
    setSaveEnabled('trade', true);
    setSaveEnabled('cash',  true);
    return;
  }
  const current   = getCurrentCash();
  const tradeNet  = netTradeDelta(trades);   // negative for net buys
  const cashNet   = netCashDelta(cash);      // positive for net deposits
  // Trade editor preview: assume cash editor NOT yet saved → current cash baseline.
  const tradeProj = current + tradeNet;
  // Cash editor preview: assume trade editor NOT yet saved → current cash baseline.
  const cashProj  = current + cashNet;
  // Worst-case: save both → cumulative
  const worst     = current + tradeNet + cashNet;

  const tradeEl = document.getElementById('trade-cash-preview');
  tradeEl.textContent =
    `本批次交易净流出 ¥${(-tradeNet).toFixed(2)} | 预计剩余现金 ¥${tradeProj.toFixed(2)}`;
  tradeEl.style.color = tradeProj < 0 ? '#c0392b' : '#27ae60';
  setSaveEnabled('trade', tradeProj >= 0);

  const cashEl = document.getElementById('cash-cash-preview');
  cashEl.textContent =
    `本批次资金净变动 ¥${cashNet.toFixed(2)} | 预计剩余现金 ¥${cashProj.toFixed(2)}` +
    (worst !== cashProj ? ` (若两表单都提交: ¥${worst.toFixed(2)})` : '');
  cashEl.style.color = cashProj < 0 ? '#c0392b' : '#27ae60';
  setSaveEnabled('cash', cashProj >= 0);
}

function setSaveEnabled(kind, enabled) {
  const btn = document.getElementById(kind + '-save-btn');
  if (btn) btn.disabled = !enabled;
}

// ---- HTTP submission (shared) ----

async function loadHoldings() {
  const res = await fetch(HOLDINGS_URL, { cache: 'no-store' });
  if (!res.ok) throw new Error(`HTTP ${res.status} loading ${HOLDINGS_URL}`);
  return await res.json();
}

async function submitTrades(kind) {
  // kind ∈ {'trade', 'cash'}
  const trades = (kind === 'trade') ? collectTradeRows() : collectCashRows();
  const statusEl = document.getElementById(kind + '-status');
  const btn = document.getElementById(kind + '-save-btn');

  if (trades === null) {
    statusEl.className = 'err';
    statusEl.textContent = '❌ 表格有红色高亮的行, 请检查必填字段.';
    return;
  }
  if (trades.length === 0) {
    statusEl.className = 'err';
    statusEl.textContent = '❌ 表格为空, 请先点「+ 加一行」录入交易.';
    return;
  }
  if (trades.length > 20) {
    statusEl.className = 'err';
    statusEl.textContent = '❌ 单次最多 20 笔交易, 当前 ' + trades.length + ' 笔.';
    return;
  }

  // Defense-in-depth cash check (server also enforces).
  const current   = getCurrentCash();
  const tradeNet  = netTradeDelta(trades.filter(t => t.side === 'buy' || t.side === 'sell'));
  const cashNet   = netCashDelta(trades.filter(t => t.side === 'deposit' || t.side === 'withdraw'));
  const projected = current + tradeNet + cashNet;
  if (projected < 0) {
    statusEl.className = 'err';
    statusEl.textContent =
      `❌ 现金不足: 当前 ¥${current.toFixed(2)}, 本批次后预计 ¥${projected.toFixed(2)} (缺 ¥${(-projected).toFixed(2)}).`;
    return;
  }

  const pat = getPat();
  if (!pat) {
    promptForPat();
    statusEl.className = 'err';
    statusEl.textContent = '❌ 需要先设置 GitHub PAT (页面顶部的提示).';
    return;
  }

  btn.disabled = true;
  statusEl.className = 'muted';
  statusEl.textContent = '提交中…';

  try {
    const res = await fetch(DISPATCH_URL, {
      method: 'POST',
      headers: {
        'Accept': 'application/vnd.github+json',
        'Authorization': `Bearer ${pat}`,
      },
      body: JSON.stringify({
        event_type: 'update_holdings',
        client_payload: { schema: 'trades-v1', trades },
      }),
    });
    if (res.status === 204 || res.ok) {
      statusEl.className = 'ok';
      statusEl.textContent =
        `✅ 已提交 ${trades.length} 笔交易. Action 约 30 秒后 commit 完成, 邮件会基于新持仓重发. 刷新页面查看新数据.`;
      // Refresh cash reference so the preview reflects the post-commit state.
      try {
        const fresh = await loadHoldings();
        document.getElementById('cash-balance').textContent = (fresh.cash ?? 0).toFixed(2);
      } catch (e) { /* ignore — refresh-on-success is best-effort */ }
      // Clear form on success.
      resetForm(kind);
    } else {
      const text = await res.text();
      if (res.status === 401 || res.status === 403) {
        statusEl.className = 'err';
        statusEl.textContent = `❌ HTTP ${res.status} 认证失败 — PAT 无效/过期/或没勾选 Contents:Write. 顶部点「设置 PAT」重试.`;
        updatePatBadge();
      } else {
        statusEl.className = 'err';
        statusEl.textContent = `❌ 提交失败: HTTP ${res.status} — ${text.slice(0, 200)}`;
      }
    }
  } catch (e) {
    statusEl.className = 'err';
    statusEl.textContent = `❌ 网络错误: ${e.message}`;
  } finally {
    // Don't re-enable if cash check would still fail; let updateCashPreview decide.
    updateCashPreview();
  }
}

function resetForm(kind) {
  const tbody = document.getElementById((kind === 'trade' ? 'trade' : 'cash') + '-tbody');
  tbody.innerHTML = '';
  tbody.appendChild(kind === 'trade' ? makeTradeRow() : makeCashRow());
  updateCashPreview();
}

function initEditor() {
  // Trade editor
  document.getElementById('trade-add-row-btn').addEventListener('click', () => {
    document.getElementById('trade-tbody').appendChild(makeTradeRow());
    updateCashPreview();
  });
  document.getElementById('trade-reset-btn').addEventListener('click', () => resetForm('trade'));
  document.getElementById('trade-save-btn').addEventListener('click',  () => submitTrades('trade'));

  // Cash editor
  document.getElementById('cash-add-row-btn').addEventListener('click', () => {
    document.getElementById('cash-tbody').appendChild(makeCashRow());
    updateCashPreview();
  });
  document.getElementById('cash-reset-btn').addEventListener('click', () => resetForm('cash'));
  document.getElementById('cash-save-btn').addEventListener('click',  () => submitTrades('cash'));

  // Load initial state
  loadHoldings()
    .then(renderEditor)
    .catch(e => {
      setStatus('trade', `加载失败: ${e.message}`, 'err');
      setStatus('cash',  `加载失败: ${e.message}`, 'err');
    });

  updatePatBadge();
}

main();
initEditor();
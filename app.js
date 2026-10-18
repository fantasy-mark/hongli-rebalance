// 稳健再平衡追踪页
// fetch history.json from the same repo (relative URL works on GitHub Pages).

// Module-level handle to the most recent record (date + etf_value). Set by
// main() after history.json loads; read by initEditor() / renderEditor()
// since the two are independent entry points invoked in sequence at the
// bottom of this file.
let latestRec = null;

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
  latestRec = latest;  // expose to renderEditor (called from initEditor)

  renderStatus(latest);
  renderCurve(history);
  renderChart();   // 缓存破坏: 给 grid_chart.png 加时间戳
  renderLatest(latest);
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
  // Two-pool cash display; fall back to legacy `cash` field if record is
  // older than the pool-split migration.
  const cashReb = Number(rec.cash_rebalance ?? rec.cash ?? 0);
  const cashGr  = Number(rec.cash_grid ?? 0);
  const cashTotal = Number(rec.cash_total ?? cashReb + cashGr);
  document.getElementById('status-badge').innerHTML =
    `<span class="badge ${cls}">${txt}</span> ` +
    `<span>${rec.date}</span> ` +
    `<span>总市值 <strong>¥${Math.round(rec.total).toLocaleString()}</strong></span> ` +
    `<span class="muted">(ETF ¥${Math.round(rec.etf_value).toLocaleString()} + ` +
    `现金 ¥${Math.round(cashTotal).toLocaleString()} = ` +
    `3pp再平衡 ¥${Math.round(cashReb).toLocaleString()} + ` +
    `复合网格 ¥${Math.round(cashGr).toLocaleString()})</span>`;
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
      <td>${s.in_cycle ? '¥' + Math.round(s.current_value).toLocaleString() : '—'}</td>
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
          <th>名称</th><th>信号</th><th>层数</th><th>均价</th><th>现价</th><th>市值</th><th>浮盈%</th><th>距下次触发</th>
        </tr>
      </thead>
      <tbody>${gridRows}</tbody>
    </table>
    ` : ''}
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

// Asset lists shown in 资金管理's "资产" column.
//   再平衡: 3-asset equal-weight basket (excludes QDII).
//   网格:    the 3 rebalance ETFs PLUS 513300 (QDII 纳斯达克, grid-only).
// Arrays (not Sets) so we can render in a fixed display order.
const REBALANCE_CODES_LIST = ["515180", "159937", "511380"];
const GRID_CODES_LIST      = ["515180", "159937", "511380", "513300"];

// Look up an asset's display name. Prefers the name from the latest record
// (so the row shows exactly what 最新持仓's tables show), falling back to
// the static ETF_CATALOG for codes not in the record (e.g. 513300, which is
// grid-only and never appears in positions[]).
function nameForCode(code, latestRec) {
  if (latestRec?.positions) {
    const p = latestRec.positions.find(x => x.code === code);
    if (p?.name) return p.name;
  }
  return ETF_CATALOG[code] || code;
}

// Minimal HTML escape for cell text (we control the inputs but defense in depth).
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

let originalHoldings = null;   // last fetched snapshot for cash reference

// Cached ETF value (from the latest record). Refreshed on load + after save.
// Used by renderEditor + post-save refresh to keep the table consistent.
let latestEtfValue = 0;

// The 3-asset equal-weight rebalance strategy. Only these codes count toward
// the "再平衡 市值" cell; the QDII (513300) is grid-only and excluded.
const REBALANCE_CODES = new Set(["515180", "159937", "511380"]);

// Both 资金管理 市值 cells read from the SAME data that 最新持仓's tables
// display (latestRec = the last record in history.json).  This guarantees
// the cash table's "市值" row can never disagree with the snapshot the
// user sees above — the 3pp row's value column and the compound-grid row's
// (new) 市值 column.
//
// Trade-off: after a manual trade/grid op, holdings.json / grid_state.json
// are updated immediately, but the 资金管理 市值 cell only updates after
// the next daily.yml re-runs (which is when latestRec is refreshed).  This
// is intentional — the user is asking for consistency with the displayed
// 最新持仓, not "freshest possible".
function computeRebalanceMarketValue(latestRec) {
  if (!latestRec?.positions) return 0;
  let total = 0;
  for (const p of latestRec.positions) {
    if (!REBALANCE_CODES.has(p.code)) continue;
    total += Number(p.value || 0);
  }
  return total;
}

function computeGridMarketValue(latestRec) {
  const signals = latestRec?.grid_signals || [];
  let total = 0;
  for (const s of signals) {
    if (!s.in_cycle) continue;
    total += Number(s.current_value || 0);
  }
  return total;
}

// Populate the 7 cells of 资金管理's summary table:
//   再平衡 row:  资产, 市值, 余额
//   网格 row:    资产, 市值, 余额
//   合计 row:                市值, 余额
//
// Called from 3 sites (renderEditor on initial load + post-save refresh,
// renderGridEditor when grid_state.json loads).  Extracted so the cell
// list and the "read from latestRec for market value, from holdings for
// balance" rule stay consistent.
function renderCashPoolSummary(latestRec, holdings) {
  const cashReb = Number(holdings?.cash_rebalance ?? 0);
  const cashGr  = Number(holdings?.cash_grid ?? 0);
  const rebalanceMarket = computeRebalanceMarketValue(latestRec);
  const gridMarket      = computeGridMarketValue(latestRec);
  const setText = (id, txt) => { const el = document.getElementById(id); if (el) el.textContent = txt; };
  setText('cash-rebalance-market', `¥${rebalanceMarket.toFixed(2)}`);
  setText('cash-rebalance-balance', `¥${cashReb.toFixed(2)}`);
  setText('cash-grid-market', `¥${gridMarket.toFixed(2)}`);
  setText('cash-grid-balance', `¥${cashGr.toFixed(2)}`);
  setText('cash-total-market', `¥${(rebalanceMarket + gridMarket).toFixed(2)}`);
  setText('cash-total-balance', `¥${(cashReb + cashGr).toFixed(2)}`);
  const setHTML = (id, html) => { const el = document.getElementById(id); if (el) el.innerHTML = html; };
  setHTML('cash-rebalance-assets',
    REBALANCE_CODES_LIST
      .map(c => `<span class="asset-tag">${escapeHtml(nameForCode(c, latestRec))}</span>`)
      .join(''));
  // 复合网格 资产 column: only show assets with active grid positions
  // (in_cycle=true).  The 复合网格 signal state in 最新持仓's nested
  // h3 ("— 持有" vs "未持仓") is the source of truth — if all grid
  // signals are 未持仓, then 资金管理's 资产 column is empty too,
  // matching the nested-section display.  This keeps the two views
  // (最新持仓 h3 grid table + 资金管理 summary row) consistent: both
  // reflect the same in_cycle snapshot.
  // grid_signals records only carry the asset name (no code), so match
  // against the name resolved from each GRID_CODES_LIST entry.
  const inCycleNames = new Set(
    (latestRec?.grid_signals || [])
      .filter(s => s.in_cycle)
      .map(s => s.name)
  );
  const gridAssetTags = GRID_CODES_LIST
    .filter(c => inCycleNames.has(nameForCode(c, latestRec)))
    .map(c => `<span class="asset-tag">${escapeHtml(nameForCode(c, latestRec))}</span>`)
    .join('');
  setHTML('cash-grid-assets',
    gridAssetTags || '<span class="muted">— 无持仓 —</span>');
}

function renderEditor(holdings, latestRec) {
  originalHoldings = JSON.parse(JSON.stringify(holdings));  // deep copy
  // Two-pool × (市值 / 余额) table at the top of the cash editor, plus
  // an 资产 column (ETFs in each pool) and a 合计 row (sum of both).
  latestEtfValue = Number(latestRec?.etf_value ?? 0);
  renderCashPoolSummary(latestRec, holdings);

  // Start with ONE empty card in each section.
  const tList = document.getElementById('trade-list');
  tList.innerHTML = '';
  tList.appendChild(makeTradeCard());
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

// Trade-op options for the unified 操作 dropdown.  Same shape as GRID_OPS
// so both editors render identical card layouts.
const TRADE_OPS = { buy: '买入', sell: '卖出' };

// Build one trade card (multi-line div).  Shape mirrors makeGridCard:
//   line 1: 日期, 资产 (dropdown), 操作 (dropdown), [del]
//   line 2: 数量, 价格, 金额(read-only)
//   line 3: 备注 (full width)
// The JSON output preserves the historical "side" key for trades-v1
// (collected as data-k="op" in the UI, mapped to "side" in collectTradeRows).
function makeTradeCard(t = {}) {
  const card = document.createElement('div');
  card.className = 'trade-card';
  const today = new Date().toISOString().slice(0, 10);
  // Default to first asset in catalog so 资产 always has a value (trade
  // historically used a free-form 6-digit code; unifying to a dropdown
  // simplifies the UI and matches grid; user can extend ETF_CATALOG).
  const firstCode = Object.keys(ETF_CATALOG)[0];
  const code = (t.code && t.code in ETF_CATALOG) ? t.code : firstCode;
  const op = TRADE_OPS[t.side] ? t.side : 'buy';
  card.innerHTML = `
    <div class="card-line">
      <label>日期 <input data-k="date" type="date" value="${escapeAttr(t.date || today)}" /></label>
      <label>资产 <select data-k="code">
        ${Object.entries(ETF_CATALOG).map(([c, n]) =>
          `<option value="${c}" ${c === code ? 'selected' : ''}>${c} ${n}</option>`).join('')}
      </select></label>
      <label>操作 <select data-k="op">
        ${Object.entries(TRADE_OPS).map(([v, label]) =>
          `<option value="${v}" ${v === op ? 'selected' : ''}>${label}</option>`).join('')}
      </select></label>
      <button type="button" class="row-del-btn" title="删除该行">✕</button>
    </div>
    <div class="card-line">
      <label>数量 <input data-k="shares" type="number" min="0" step="1" value="${t.shares ?? ''}" placeholder="股数" /></label>
      <label>价格 <input data-k="price"  type="number" min="0" step="0.0001" value="${t.price ?? ''}" placeholder="¥" /></label>
      <span class="muted">金额 <span data-k="amount-readout" class="amount-readout">—</span></span>
    </div>
    <div class="card-line">
      <label class="grow">备注 <input data-k="note" type="text" value="${escapeAttr(t.note || '')}" placeholder="备注" /></label>
    </div>
  `;
  const updateAmount = () => {
    const sh = parseFloat(card.querySelector('[data-k="shares"]').value) || 0;
    const px = parseFloat(card.querySelector('[data-k="price"]').value)  || 0;
    const readout = card.querySelector('[data-k="amount-readout"]');
    readout.textContent = (sh > 0 && px > 0) ? '¥' + (sh * px).toFixed(2) : '—';
    updateCashPreview();
  };
  card.querySelector('[data-k="shares"]').addEventListener('input', updateAmount);
  card.querySelector('[data-k="price"]').addEventListener('input',  updateAmount);
  card.querySelector('[data-k="op"]').addEventListener('change',    updateCashPreview);
  card.querySelector('.row-del-btn').addEventListener('click', () => {
    card.remove();
    updateCashPreview();
  });
  return card;
}

function collectTradeRows() {
  // Returns array of validated trade {side, date, code, shares, price, note}
  // or null if any non-blank card is invalid (cards are marked red).
  // The unified form uses data-k="op" for the 操作 dropdown; the JSON
  // payload preserves the historical "side" key for trades-v1 compat.
  const cards = [...document.querySelectorAll('#trade-list .trade-card')];
  let ok = true;
  const out = [];
  cards.forEach((card) => {
    const get = k => (card.querySelector(`[data-k="${k}"]`) || {}).value || '';
    const op    = get('op').trim();
    const date  = get('date').trim();
    const code  = get('code').trim();
    const shares = get('shares').trim();
    const price  = get('price').trim();
    const note   = get('note').trim();
    // Skip cards where the user hasn't entered any of the required trade
    // fields yet. (date + code default so we don't include them.)
    if (!shares && !price && !note) return;

    const trade = { side: op, date, code };
    let rowOK = true;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) rowOK = false;
    if (!TRADE_OPS[op])                      rowOK = false;
    if (!(code in ETF_CATALOG))              rowOK = false;
    const s = parseFloat(shares);
    const p = parseFloat(price);
    if (!(s > 0)) rowOK = false; else trade.shares = s;
    if (!(p > 0)) rowOK = false; else trade.price  = p;
    if (note) trade.note = note;

    card.classList.toggle('invalid', !rowOK);
    if (!rowOK) ok = false;
    if (rowOK) out.push(trade);
  });
  return ok ? out : null;
}

// ---- Cash editor (deposit / withdraw) ----

// 6 valid cash sides — used by both the row template and the validator.
const CASH_SIDES = [
  'deposit_rebalance', 'deposit_grid',
  'withdraw_rebalance', 'withdraw_grid',
  'transfer_to_rebalance', 'transfer_to_grid',
];
const CASH_SIDE_LABELS = {
  deposit_rebalance:     '入金→3pp再平衡',
  deposit_grid:          '入金→复合网格',
  withdraw_rebalance:    '提现←3pp再平衡',
  withdraw_grid:         '提现←复合网格',
  transfer_to_rebalance: '转换: 复合网格→3pp再平衡',
  transfer_to_grid:      '转换: 3pp再平衡→复合网格',
};

function makeCashRow(t = {}) {
  // Cash-row shape: {date, side (6 options), amount, note}
  const tr = document.createElement('tr');
  const today = new Date().toISOString().slice(0, 10);
  const side = CASH_SIDES.includes(t.side) ? t.side : 'deposit_rebalance';
  const opts = CASH_SIDES.map(s =>
    `<option value="${s}" ${s === side ? 'selected' : ''}>${CASH_SIDE_LABELS[s]}</option>`,
  ).join('');
  tr.innerHTML = `
    <td><input data-k="date" type="date" value="${escapeAttr(t.date || today)}" style="width:9em;" /></td>
    <td>
      <select data-k="side" style="padding:0.2em;">${opts}</select>
    </td>
    <td><input data-k="amount" type="number" value="${t.amount ?? ''}" min="0" step="0.01" style="text-align:right;width:10em;" placeholder="¥" /></td>
    <td><input data-k="note"   type="text"   value="${escapeAttr(t.note || '')}" placeholder="备注" style="width:12em;" /></td>
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
    if (!CASH_SIDES.includes(side))         rowOK = false;
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
//
// Two cash pools: cash_rebalance (real ETF money) + cash_grid (virtual).
// Each cash-row side routes its amount into one or both pools:
//   deposit_rebalance / withdraw_rebalance → cash_rebalance only
//   deposit_grid      / withdraw_grid      → cash_grid only
//   transfer_to_grid  (rebalance→grid)     → -rebalance, +grid
//   transfer_to_rebalance (grid→rebalance) → +rebalance, -grid
// Buy/sell trades only touch cash_rebalance (per user decision: grid is
// fully virtual; ETF trades never deduct from cash_grid).

function netTradeDeltaByPool(trades) {
  // Returns { rebalance, grid }. Buys/sells ONLY affect cash_rebalance.
  return (trades || []).reduce((acc, t) => {
    if (t.side === 'buy')  return { rebalance: acc.rebalance - t.shares * t.price, grid: acc.grid };
    if (t.side === 'sell') return { rebalance: acc.rebalance + t.shares * t.price, grid: acc.grid };
    return acc;
  }, { rebalance: 0, grid: 0 });
}

function netTradeDelta(trades) {
  // Back-compat: total cash delta for trade rows (always = rebalance, since
  // grid never touches ETF trades).
  return netTradeDeltaByPool(trades).rebalance;
}

function netCashDeltaByPool(rows) {
  // Returns { rebalance, grid }.
  return (rows || []).reduce((acc, r) => {
    switch (r.side) {
      case 'deposit_rebalance':     return { rebalance: acc.rebalance + r.amount, grid: acc.grid };
      case 'deposit_grid':          return { rebalance: acc.rebalance,            grid: acc.grid + r.amount };
      case 'withdraw_rebalance':    return { rebalance: acc.rebalance - r.amount, grid: acc.grid };
      case 'withdraw_grid':         return { rebalance: acc.rebalance,            grid: acc.grid - r.amount };
      case 'transfer_to_grid':      return { rebalance: acc.rebalance - r.amount, grid: acc.grid + r.amount };
      case 'transfer_to_rebalance': return { rebalance: acc.rebalance + r.amount, grid: acc.grid - r.amount };
      default: return acc;
    }
  }, { rebalance: 0, grid: 0 });
}

function netCashDelta(rows) {
  // Back-compat: net signed cash (sum of rebalance + grid) — for legacy callers.
  const d = netCashDeltaByPool(rows);
  return d.rebalance + d.grid;
}

function getCurrentCash() {
  // Read the BASELINE from originalHoldings, NOT the displayed card. The
  // rebalance card shows ETF + cash combined for display; the trade/cash
  // preview math must use the raw cash_rebalance baseline.
  const h = originalHoldings || {};
  return {
    rebalance: Number(h.cash_rebalance ?? 0),
    grid:      Number(h.cash_grid      ?? 0),
  };
}

function updateCashPreview() {
  // Live preview of the COMBINED effect of BOTH forms on each pool.
  // Per-pool guard: each pool must stay >= 0 (server also enforces).
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
  const tradeDelta = netTradeDeltaByPool(trades);   // only rebalance
  const cashDelta  = netCashDeltaByPool(cash);

  // Per-form projections: each form assumes the other has NOT been saved.
  // Trade preview: cash editor not yet saved → baseline = current
  const tradeProjReb = current.rebalance + tradeDelta.rebalance;
  const tradeProjGr  = current.grid;
  // Cash preview: trade editor not yet saved → baseline = current
  const cashProjReb  = current.rebalance + cashDelta.rebalance;
  const cashProjGr   = current.grid      + cashDelta.grid;
  // Worst-case: save both → cumulative
  const worstReb = current.rebalance + tradeDelta.rebalance + cashDelta.rebalance;
  const worstGr  = current.grid      + cashDelta.grid;

  const tradeEl = document.getElementById('trade-cash-preview');
  tradeEl.textContent =
    `本批次交易净流出 ¥${(-tradeDelta.rebalance).toFixed(2)} | ` +
    `预计剩余3pp再平衡 ¥${tradeProjReb.toFixed(2)} (复合网格 ¥${tradeProjGr.toFixed(2)})`;
  tradeEl.style.color = tradeProjReb < 0 ? '#c0392b' : '#27ae60';
  setSaveEnabled('trade', tradeProjReb >= 0);

  const cashEl = document.getElementById('cash-cash-preview');
  cashEl.textContent =
    `本批次资金净变动: 3pp再平衡 ¥${cashDelta.rebalance >= 0 ? '+' : ''}${cashDelta.rebalance.toFixed(2)}, ` +
    `复合网格 ¥${cashDelta.grid >= 0 ? '+' : ''}${cashDelta.grid.toFixed(2)} | ` +
    `预计剩余: 3pp再平衡 ¥${cashProjReb.toFixed(2)}, 复合网格 ¥${cashProjGr.toFixed(2)}` +
    ((worstReb !== cashProjReb || worstGr !== cashProjGr)
      ? ` (若两表单都提交: 3pp再平衡 ¥${worstReb.toFixed(2)}, 复合网格 ¥${worstGr.toFixed(2)})`
      : '');
  cashEl.style.color = (cashProjReb < 0 || cashProjGr < 0) ? '#c0392b' : '#27ae60';
  setSaveEnabled('cash', cashProjReb >= 0 && cashProjGr >= 0);
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

  // Defense-in-depth cash check (server also enforces).  Two pools:
  // each must stay >= 0 after this batch.
  const current   = getCurrentCash();
  const tradeDelta = netTradeDeltaByPool(trades);
  const cashDelta  = netCashDeltaByPool(trades);
  const projectedReb = current.rebalance + tradeDelta.rebalance + cashDelta.rebalance;
  const projectedGr  = current.grid      + cashDelta.grid;
  if (projectedReb < 0 || projectedGr < 0) {
    statusEl.className = 'err';
    const bad = [];
    if (projectedReb < 0) bad.push(`3pp再平衡: 当前 ¥${current.rebalance.toFixed(2)}, 预计 ¥${projectedReb.toFixed(2)} (缺 ¥${(-projectedReb).toFixed(2)})`);
    if (projectedGr  < 0) bad.push(`复合网格:   当前 ¥${current.grid.toFixed(2)}, 预计 ¥${projectedGr.toFixed(2)} (缺 ¥${(-projectedGr).toFixed(2)})`);
    statusEl.textContent = `❌ 现金不足 — ${bad.join(' / ')}.`;
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
      // Re-render the cash pool table; ETF market value and grid market
      // value stay constant until the next daily Action re-computes
      // history.json / grid_state.json.
      try {
        const fresh = await loadHoldings();
        originalHoldings = JSON.parse(JSON.stringify(fresh));
        renderCashPoolSummary(latestRec, fresh);
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
  if (kind === 'trade') {
    const list = document.getElementById('trade-list');
    list.innerHTML = '';
    list.appendChild(makeTradeCard());
  } else {
    const tbody = document.getElementById('cash-tbody');
    tbody.innerHTML = '';
    tbody.appendChild(makeCashRow());
  }
  updateCashPreview();
}

// ---- Grid editor (open / add / tp) ----

const GRID_OPS = { open: "开仓", add: "加仓", tp: "止盈" };
const GRID_STATE_URL = "data/grid_state.json";

let currentGridState = null;   // last fetched snapshot for preview

async function loadGridState() {
  const res = await fetch(GRID_STATE_URL, { cache: 'no-store' });
  if (!res.ok) throw new Error(`HTTP ${res.status} loading ${GRID_STATE_URL}`);
  return await res.json();
}

function renderGridEditor(state) {
  currentGridState = state;
  const list = document.getElementById('grid-list');
  list.innerHTML = '';
  list.appendChild(makeGridCard());
  setStatus('grid', '');
  refreshGridPreview();
  // Refresh the cash-management summary table.  Market-value cells read
  // from latestRec (the same data shown in 最新持仓's tables); balance
  // cells use originalHoldings (the cached holdings.json snapshot).  This
  // keeps 资金管理 consistent with the displayed 最新持仓.  The latestRec
  // guard avoids wiping cells to ¥0.00 if this grid fetch races the
  // initial history.json load.
  if (latestRec) renderCashPoolSummary(latestRec, originalHoldings);
}

function refreshGridPreview() {
  const el = document.getElementById('grid-preview');
  if (!el || !currentGridState) { if (el) el.textContent = ''; return; }
  // Map code → state using ETF_CATALOG.
  const codeToState = {};
  for (const [code, name] of Object.entries(ETF_CATALOG)) {
    codeToState[code] = currentGridState[name] || null;
  }
  const parts = Object.entries(ETF_CATALOG).map(([code, name]) => {
    const s = codeToState[code];
    if (!s || !s.in_cycle) return `${code} 待开仓`;
    const n = (s.entries || []).length;
    const invested = (s.entries || []).reduce(
      (acc, e) => acc + e.shares * e.price, 0,
    );
    const total_sh = (s.entries || []).reduce((acc, e) => acc + e.shares, 0);
    const avg = total_sh > 0 ? (invested / total_sh).toFixed(3) : '0';
    return `${code} 持仓中 (${n} 层, 均价 ¥${avg})`;
  });
  el.textContent = `当前复合网格: ${parts.join(' | ')}`;
}

function makeGridCard(t = {}) {
  const card = document.createElement('div');
  card.className = 'trade-card';
  const today = new Date().toISOString().slice(0, 10);
  const op = GRID_OPS[t.op] ? t.op : 'add';
  const firstCode = Object.keys(ETF_CATALOG)[0];
  const code = (t.code && t.code in ETF_CATALOG) ? t.code : firstCode;
  card.innerHTML = `
    <div class="card-line">
      <label>日期 <input data-k="date" type="date" value="${escapeAttr(t.date || today)}" /></label>
      <label>资产 <select data-k="code">
        ${Object.entries(ETF_CATALOG).map(([c, n]) =>
          `<option value="${c}" ${c === code ? 'selected' : ''}>${c} ${n}</option>`).join('')}
      </select></label>
      <label>操作 <select data-k="op">
        ${Object.entries(GRID_OPS).map(([v, label]) =>
          `<option value="${v}" ${v === op ? 'selected' : ''}>${label}</option>`).join('')}
      </select></label>
      <button type="button" class="row-del-btn" title="删除该行">✕</button>
    </div>
    <div class="card-line">
      <label>数量 <input data-k="shares" type="number" min="0" step="1" value="${t.shares ?? ''}" placeholder="股" /></label>
      <label>价格 <input data-k="price"  type="number" min="0" step="0.0001" value="${t.price ?? ''}" placeholder="¥" /></label>
      <span class="muted">金额 <span data-k="amount-readout" class="amount-readout">—</span></span>
    </div>
    <div class="card-line">
      <label class="grow">备注 <input data-k="note" type="text" value="${escapeAttr(t.note || '')}" placeholder="备注" /></label>
    </div>
  `;
  const updateAmount = () => {
    const px = parseFloat(card.querySelector('[data-k="price"]').value)  || 0;
    const sh = parseFloat(card.querySelector('[data-k="shares"]').value) || 0;
    const ro = card.querySelector('[data-k="amount-readout"]');
    ro.textContent = (px > 0 && sh > 0) ? '¥' + (px * sh).toFixed(2) : '—';
  };
  card.querySelector('[data-k="price"]').addEventListener('input',  updateAmount);
  card.querySelector('[data-k="shares"]').addEventListener('input', updateAmount);
  card.querySelector('.row-del-btn').addEventListener('click', () => card.remove());
  return card;
}

function collectGridRows() {
  // Returns array of validated ops {date, code, op, price, shares, note}
  // or null if any non-blank card is invalid.
  const cards = [...document.querySelectorAll('#grid-list .trade-card')];
  let ok = true;
  const out = [];
  cards.forEach((card) => {
    const get = k => (card.querySelector(`[data-k="${k}"]`) || {}).value || '';
    const date   = get('date').trim();
    const code   = get('code').trim();
    const op     = get('op').trim();
    const price  = get('price').trim();
    const shares = get('shares').trim();
    const note   = get('note').trim();
    // Skip fully-blank cards (date + code default; check op/price/note)
    if (!op && !price && !shares && !note) return;

    const row = { date, code, op };
    let rowOK = true;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) rowOK = false;
    if (!GRID_OPS[op]) rowOK = false;
    if (!(code in ETF_CATALOG)) rowOK = false;
    const p = parseFloat(price);
    if (!(p > 0)) rowOK = false; else row.price = p;
    if (op === 'tp') {
      row.shares = 0;   // ignored by server, but valid
    } else {
      const s = parseFloat(shares);
      if (!(s > 0)) rowOK = false; else row.shares = s;
    }
    if (note) row.note = note;

    card.classList.toggle('invalid', !rowOK);
    if (!rowOK) ok = false;
    if (rowOK) out.push(row);
  });
  return ok ? out : null;
}

async function submitGridOp() {
  const statusEl = document.getElementById('grid-status');
  const btn = document.getElementById('grid-save-btn');
  const ops = collectGridRows();
  if (ops === null) {
    statusEl.className = 'err';
    statusEl.textContent = '❌ 表格有红色高亮的行, 请检查必填字段.';
    return;
  }
  if (ops.length === 0) {
    statusEl.className = 'err';
    statusEl.textContent = '❌ 表格为空, 请先点「+ 加一行」录入复合网格操作.';
    return;
  }
  if (ops.length > 20) {
    statusEl.className = 'err';
    statusEl.textContent = '❌ 单次最多 20 个操作, 当前 ' + ops.length + ' 个.';
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
        event_type: 'update_grid',
        client_payload: { schema: 'grid-ops-v1', ops },
      }),
    });
    if (res.status === 204 || res.ok) {
      statusEl.className = 'ok';
      statusEl.textContent =
        `✅ 已提交 ${ops.length} 个复合网格操作. Action 约 30 秒后 commit 完成, 邮件+图表同步更新. 刷新页面查看新数据.`;
      // Refresh grid state preview from server
      try {
        const fresh = await loadGridState();
        renderGridEditor(fresh);
      } catch (e) { /* ignore — refresh-on-success is best-effort */ }
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
    btn.disabled = false;
  }
}

function resetGridForm() {
  const list = document.getElementById('grid-list');
  list.innerHTML = '';
  list.appendChild(makeGridCard());
}

function initEditor() {
  // Trade editor
  document.getElementById('trade-add-row-btn').addEventListener('click', () => {
    document.getElementById('trade-list').appendChild(makeTradeCard());
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

  // Grid editor (manual open/add/tp)
  document.getElementById('grid-add-row-btn').addEventListener('click', () => {
    document.getElementById('grid-list').appendChild(makeGridCard());
  });
  document.getElementById('grid-reset-btn').addEventListener('click', resetGridForm);
  document.getElementById('grid-save-btn').addEventListener('click',  submitGridOp);

  // Load initial state
  loadHoldings()
    .then(h => renderEditor(h, latestRec))
    .catch(e => {
      setStatus('trade', `加载失败: ${e.message}`, 'err');
      setStatus('cash',  `加载失败: ${e.message}`, 'err');
    });

  loadGridState()
    .then(renderGridEditor)
    .catch(e => setStatus('grid', `加载失败: ${e.message}`, 'err'));

  updatePatBadge();
}

main();
initEditor();
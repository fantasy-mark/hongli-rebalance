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
    const badge = document.getElementById('status-badge');
    badge.className = 'err';
    badge.innerHTML =
      `<div class="status-main">` +
        `<span class="badge badge-err">⚠ 加载失败</span>` +
        `<span class="status-error">${e.message}</span>` +
      `</div>`;
    document.getElementById('curve').textContent = '';
    return;
  }

  if (!Array.isArray(history) || history.length === 0) {
    const badge = document.getElementById('status-badge');
    badge.className = 'info';
    badge.innerHTML =
      `<div class="status-main">` +
        `<span class="badge badge-info">暂无数据</span>` +
      `</div>` +
      `<div class="status-breakdown">` +
        `GitHub Action 还没跑过, 或 <code>data/history.json</code> 为空` +
      `</div>`;
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
  // Race-condition backfill: renderEditor is gated on latestRec to
  // avoid writing ¥0.00 when loadHoldings() wins the race.  But if
  // loadHoldings() finished first, originalHoldings is already set
  // and the 市值 cells are still empty — fill them in now.
  if (originalHoldings) renderCashPoolSummary(latestRec, originalHoldings);
}

function renderChart() {
  // 加 ?t=Date.now() 让浏览器每次访问都重新拉图, 不会用旧缓存
  const img = document.getElementById('grid-chart');
  if (img) img.src = `data/grid_chart.png?t=${Date.now()}`;
}

function renderStatus(rec) {
  const grid = rec.grid_signals || [];
  // 网格引擎**不再自动交易** —— signal 只是"建议提交哪种 op"。
  // "buy"/"sell" 是自动时代的旧值 (只可能出现在旧 history.json 里), 兜底别崩。
  const anySuggestion = grid.some(s =>
    ['open', 'add', 'tp', 'buy', 'sell'].includes(s.signal));

  const cls = rec.triggered ? 'warn' : (anySuggestion ? 'info' : 'ok');
  const badgeCls = rec.triggered ? 'badge-warn' : (anySuggestion ? 'badge-info' : 'badge-ok');
  const txt = rec.triggered ? '⚠ 需要调仓' : (anySuggestion ? 'ℹ 网格有建议' : '✓ 未触发');
  // Two-pool cash display; fall back to legacy `cash` field if record is
  // older than the pool-split migration.
  const cashReb = Number(rec.cash_rebalance ?? rec.cash ?? 0);
  const cashGr  = Number(rec.cash_grid ?? 0);
  const cashTotal = Number(rec.cash_total ?? cashReb + cashGr);
  const badge = document.getElementById('status-badge');
  badge.className = cls;
  badge.innerHTML =
    `<div class="status-main">` +
      `<span class="badge ${badgeCls}">${txt}</span>` +
      `<span class="status-date">${rec.date}</span>` +
      `<span class="status-total">总市值<strong>¥${Math.round(rec.total).toLocaleString()}</strong></span>` +
    `</div>` +
    `<div class="status-breakdown">` +
      `ETF ¥${Math.round(rec.etf_value).toLocaleString()}` +
      ` + 现金 ¥${Math.round(cashTotal).toLocaleString()}` +
      ` <span class="status-pool">(3pp再平衡 ¥${Math.round(cashReb).toLocaleString()} + ` +
      `复合网格 ¥${Math.round(cashGr).toLocaleString()})</span>` +
    `</div>`;
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

// 建议动作 → 徽章文案。新词表 "open"/"add"/"tp" —— 引擎**不再自动交易**,
// 这三个值是"建议你在表单里提交哪种 op", 不是"已成交"。
// "buy"/"sell" 是自动时代的旧值 (只可能出现在旧 history.json 里), 兜底别崩。
function gridSuggestionLabel(sig) {
  if (sig === 'open') return { cls: 'warn', text: '建议开仓' };
  if (sig === 'add')  return { cls: 'warn', text: '建议加仓' };
  if (sig === 'tp')   return { cls: 'ok',   text: '建议回本' };
  if (sig === 'buy')  return { cls: 'warn', text: '买入(旧)' };
  if (sig === 'sell') return { cls: 'ok',   text: '回本(旧)' };
  return { cls: 'ok', text: '— 无建议' };
}

function renderLatest(rec) {
  const rows = rec.positions.map((p, i) => {
    const w = rec.weights[i];
    const delta = (w - rec.target_w) * 100;
    const over = Math.abs(delta) > rec.threshold_pp;
    // D6: 份额拆成**两个数**摆出来 —— 「真实持仓」与「归并」。
    //   真实持仓 ← p.shares        (账本里的真实成交份额)
    //   归并     ← p.merged_shares (网格归并进来的**模拟**份额)
    // 两个数分开显示, 才看得出某个仓位里有多少是虚拟的 (网格是虚拟策略)。
    // ⚠️ 市值 p.value / 权重 / 偏离 走的是**另一个口径** ——
    //    (真实持仓 + 归并) × 价格, 页脚写清楚了, 别拿「真实持仓 × 价格」
    //    去对市值。
    const realShares = Number(p.shares ?? 0);
    const mergedShares = Number(p.merged_shares || 0);
    const mergedCell = mergedShares > 0
      ? `<span style="color:#B03A2E;">${Math.round(mergedShares).toLocaleString()}</span>`
      : '<span class="muted">—</span>';
    return `<tr>
      <td>${p.name}</td>
      <td>${p.code}</td>
      <td>${Math.round(realShares).toLocaleString()}</td>
      <td>${mergedCell}</td>
      <td>¥${p.price.toFixed(3)}</td>
      <td>¥${Math.round(p.value).toLocaleString()}</td>
      <td>${(w * 100).toFixed(2)}%</td>
      <td class="${over ? 'warn' : ''}">${delta >= 0 ? '+' : ''}${delta.toFixed(2)}pp</td>
    </tr>`;
  }).join('');

  const grid = rec.grid_signals || [];
  const gridRows = grid.map(s => {
    const lab = gridSuggestionLabel(s.signal);
    let sigText = lab.text;
    // 有建议时把建议金额挂在后面 (¥10000 / 份额) —— 让用户照着填表单。
    const sugAmount = Number(s.suggest_amount || 0);
    if (s.signal === 'open' || s.signal === 'add') {
      sigText += ` ¥${Math.round(sugAmount).toLocaleString()}`;
    }
    // 距建议触发: 待机=距开仓(距窗口最低), 持仓=距加仓 / 距回本出场位。
    // 距离口径: 正数 = 还没触发, ≤0 = 已越过触发线。
    const trigPct = s.trigger_pct ?? 0;
    const trigKind = s.trigger_kind || 'open';
    const trigPrefix = trigKind === 'tp' ? '距回本'
                     : (trigKind === 'add' ? '距加仓' : '距开仓');
    let trigClass = 'ok';
    if (trigPct <= 0.5) trigClass = 'warn';
    else if (trigKind === 'open' && trigPct <= 2.0) trigClass = 'warn';
    // 有建议 = 已经越过触发线 → 这一格显示"已触发"而不是百分比。
    const trigCell = s.signal
      ? `<span title="触发价 ¥${Number(s.trigger_price ?? 0).toFixed(3)}">已触发</span>`
      : `${trigPrefix} ${trigPct >= 0 ? '+' : ''}${trigPct.toFixed(2)}%`;
    // 资金占用 = 网格**在周期内**的层 (出场后本金收回, 占用回落)。
    // 已归并的份额不再占网格的钱 —— 它们是再平衡组合的持仓了。
    const useValue = Number(s.current_value || 0);
    const mergedShares = Number(s.merged_shares || 0);
    const mergedValue = Number(s.merged_value || 0);
    let valueCell = useValue > 0 ? '¥' + Math.round(useValue).toLocaleString() : '—';
    if (useValue > 0 && mergedValue > 0) {
      // Tooltip 说明这一栏只是网格自己的占用, 归并部分记在再平衡那一栏。
      valueCell = `<span title="网格在周期内的层; 另有 ¥${Math.round(mergedValue).toLocaleString()} 已归并到再平衡持仓">${valueCell}</span>`;
    }
    const mergedCell = mergedShares > 0
      ? `<span style="color:#B03A2E;">${Math.round(mergedShares).toLocaleString()}</span>`
      : '<span class="muted">—</span>';
    return `<tr>
      <td>${s.name}</td>
      <td class="${lab.cls}">${sigText}</td>
      <td>${s.num_buys}</td>
      <td>¥${s.avg_cost.toFixed(3)}</td>
      <td>¥${s.price.toFixed(3)}</td>
      <td>${valueCell}</td>
      <td class="${s.unrealized_pnl >= 0 ? 'ok' : 'warn'}">${s.in_cycle ? (s.return_pct >= 0 ? '+' : '') + s.return_pct.toFixed(2) + '%' : '—'}</td>
      <td>${mergedCell}</td>
      <td class="${trigClass}">${trigCell}</td>
    </tr>`;
  }).join('');

  const layer = grid[0]?.layer_yuan || 10000;

  document.getElementById('latest-detail').innerHTML = `
    <p class="muted">价格日期: ${rec.positions[0]?.date || '?'}</p>

    <h3 style="margin-top:1em;">3pp 再平衡</h3>
    <table>
      <thead>
        <tr>
          <th>名称</th><th>代码</th><th>真实持仓</th><th>归并</th><th>价格</th><th>市值</th><th>权重</th><th>偏离</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="muted" style="font-size:12px;">真实持仓 = 账本里的真实成交份额 · 归并 = 网格归并进来的份额(模拟口径, 虚拟策略) · 市值 = (真实持仓 + 归并) × 价格 · 权重与偏离按含归并口径</p>

    ${grid.length ? `
    <h3 style="margin-top:1.5em;">复合网格 <span class="muted" style="font-size:12px;font-weight:normal;">(引擎只给建议, 不会自动下单 · 单层金额 ¥${layer.toLocaleString()})</span></h3>
    <table>
      <thead>
        <tr>
          <th>名称</th><th>建议</th><th>层数</th><th>均价</th><th>现价</th><th>资金占用</th><th>浮盈%</th><th>已归并</th><th>距建议触发</th>
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

// Known ETF catalog surfaced in the "code" dropdown (mirrors src/application/daily_snapshot.py ETF_DEFS).
// Users can still type a custom 6-digit code, but the workflow will reject unknown ones.
const ETF_CATALOG = {
  "515180": "红利ETF易方达",
  "159937": "黄金ETF博时",
  "511380": "可转债ETF博时",
  "513300": "纳斯达克ETF华夏",
};

// Asset lists shown in 资金管理's "资产" column.
//   再平衡: the basket the engine actually rebalances — derived from the
//           latest record's positions (src/application/daily_snapshot.py: target_w =
//           1/len(positions), 4 assets @ 25% each). Hardcoding the codes
//           here is how this row silently under-reported 纳斯达克ETF华夏's
//           ¥38,736 after the 2026-10-14 four-asset migration — the 3pp
//           table above it and the 总市值 badge both showed 4 assets.
//   网格:    every asset the grid tracks — derived from the record's
//           `grid_signals` (one row per grid asset, written by the engine).
//           ⚠️ 这两个清单都**不再抄在页面里**: 引擎换篮子时, 页面跟着记录走,
//           不会拿一份过期的手抄清单继续说谎。
// (The engine's universe lives in src/domain/portfolio/asset_universe.py;
//  the page reads it out of the record rather than duplicating it.)

// The rebalance basket, in the engine's own order. Falls back to the full
// catalog when the record hasn't loaded yet.
function rebalanceCodes(latestRec) {
  const fromRecord = (latestRec?.positions || []).map(p => p.code);
  return fromRecord.length ? fromRecord : Object.keys(ETF_CATALOG);
}

// Look up an asset's display name. Prefers the name from the latest record
// (so the row shows exactly what 最新持仓's tables show), falling back to
// the static ETF_CATALOG for codes not in the record.
function nameForCode(code, latestRec) {
  if (latestRec?.positions) {
    const p = latestRec.positions.find(x => x.code === code);
    if (p?.name) return p.name;
  }
  return ETF_CATALOG[code] || code;
}

// The inverse lookup: display name → code. Prefers the latest record
// (the engine's own view of the basket), falls back to the static
// ETF_CATALOG, and finally to the name itself. Lets the UI follow the
// engine's asset list instead of a hand-copied one.
function codeForName(name, latestRec) {
  const p = (latestRec?.positions || []).find(x => x.name === name);
  if (p?.code) return p.code;
  for (const [code, n] of Object.entries(ETF_CATALOG)) {
    if (n === name) return code;
  }
  return name;
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
  // Every position in the record IS part of the rebalance basket (the engine
  // equal-weights all of them), and p.value already includes the grid's
  // merged shares (模拟口径). So no code filter here — filtering is exactly
  // what made this cell under-report the portfolio.
  let total = 0;
  for (const p of latestRec.positions) {
    total += Number(p.value || 0);
  }
  return total;
}

function computeGridMarketValue(latestRec) {
  const signals = latestRec?.grid_signals || [];
  let total = 0;
  for (const s of signals) {
    // 资金占用 = 网格**在周期内**的层 (出场后本金收回, 占用回落)。
    // 归并到再平衡的份额算在 3pp 再平衡那一行, 不在这里重复计。
    const useValue = Number(s.current_value || 0);
    if (useValue > 0) total += useValue;
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
    rebalanceCodes(latestRec)
      .map(c => `<span class="asset-tag">${escapeHtml(nameForCode(c, latestRec))}</span>`)
      .join(''));
  // 复合网格 资产 column: only show assets the strategy actually holds.
  // The 复合网格 signal state in 最新持仓's nested h3 (the 资金占用 column:
  // "—" vs a number) is the source of truth — if all grid rows show "—",
  // then 资金管理's 资产 column is empty too, matching the nested-section
  // display.  This keeps the two views (最新持仓 h3 grid table + 资金管理
  // summary row) consistent: both reflect the same snapshot.
  //
  // "Holds" means the grid still has capital tied up in that asset, i.e.
  // current_value > 0 (the layers of the currently-open cycle). 出场后本金
  // 全额收回, 网格不再持有该资产 —— 归并出去的那批份额归再平衡组合管。
  //
  // 资产清单**取自记录** (grid_signals —— 引擎自己的清单, 每个网格资产一行),
  // 不再是页面里抄的一份: 引擎换篮子时这一行跟着记录走, 不会漏掉新资产。
  const gridAssetTags = (latestRec?.grid_signals || [])
    .filter(s => Number(s.current_value || 0) > 0)
    .map(s => `<span class="asset-tag">${escapeHtml(s.name)}</span>`)
    .join('');
  setHTML('cash-grid-assets',
    gridAssetTags || '<span class="muted">— 无持仓 —</span>');
}

function renderEditor(holdings, latestRec) {
  originalHoldings = JSON.parse(JSON.stringify(holdings));  // deep copy
  // Two-pool × (市值 / 余额) table at the top of the cash editor, plus
  // an 资产 column (ETFs in each pool) and a 合计 row (sum of both).
  latestEtfValue = Number(latestRec?.etf_value ?? 0);
  // Race-condition guard: initEditor()'s loadHoldings() fetch usually
  // wins over main()'s history.json fetch (holdings.json is much
  // smaller), so this callback often runs with latestRec still null.
  // Calling renderCashPoolSummary(null, ...) would write ¥0.00 into the
  // 3pp再平衡 市值 / 复合网格 市值 cells and stay there until the next
  // refresh — main() sets latestRec a moment later but doesn't re-trigger
  // this function.  Mirror the same guard renderGridEditor uses: only
  // populate the market-value cells when latestRec is actually loaded.
  // main() will follow up with a renderCashPoolSummary(latestRec,
  // originalHoldings) once history.json arrives, so the cells get
  // filled in shortly after — just not with a flash of 0 in between.
  if (latestRec) renderCashPoolSummary(latestRec, holdings);

  // Start with ONE empty card in each section.
  const tList = document.getElementById('trade-list');
  tList.innerHTML = '';
  tList.appendChild(makeTradeCard());
  const cList = document.getElementById('cash-list');
  cList.innerHTML = '';
  cList.appendChild(makeCashCard());

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

// ---- Cash editor (6 cash sides across 2 pools) ----

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

function makeCashCard(t = {}) {
  // Cash-card shape: {date, side (6 options), amount, note}
  // Card layout (3 lines) mirrors makeGridCard / makeTradeCard for
  // visual consistency across all 3 editors (3pp再平衡 / 复合网格 / 资金管理).
  const card = document.createElement('div');
  card.className = 'trade-card';
  const today = new Date().toISOString().slice(0, 10);
  const side = CASH_SIDES.includes(t.side) ? t.side : 'deposit_rebalance';
  const opts = CASH_SIDES.map(s =>
    `<option value="${s}" ${s === side ? 'selected' : ''}>${CASH_SIDE_LABELS[s]}</option>`,
  ).join('');
  card.innerHTML = `
    <div class="card-line">
      <label>日期 <input data-k="date" type="date" value="${escapeAttr(t.date || today)}" /></label>
      <label>方向 <select data-k="side">${opts}</select></label>
      <button type="button" class="row-del-btn" title="删除该行">✕</button>
    </div>
    <div class="card-line">
      <label>金额 <input data-k="amount" type="number" min="0" step="0.01" value="${t.amount ?? ''}" placeholder="¥" /></label>
    </div>
    <div class="card-line">
      <label class="grow">备注 <input data-k="note" type="text" value="${escapeAttr(t.note || '')}" placeholder="备注" /></label>
    </div>
  `;
  card.querySelector('[data-k="amount"]').addEventListener('input', updateCashPreview);
  card.querySelector('[data-k="side"]').addEventListener('change',  updateCashPreview);
  card.querySelector('.row-del-btn').addEventListener('click', () => {
    card.remove();
    updateCashPreview();
  });
  return card;
}

function collectCashRows() {
  // Returns array of validated cash rows {side, date, amount, note}
  // or null if any non-blank card is invalid (card gets the .invalid outline).
  const cards = [...document.querySelectorAll('#cash-list .trade-card')];
  let ok = true;
  const out = [];
  cards.forEach((card) => {
    const get = k => (card.querySelector(`[data-k="${k}"]`) || {}).value || '';
    const side   = get('side').trim();
    const date   = get('date').trim();
    const amount = get('amount').trim();
    const note   = get('note').trim();
    // Skip cards where the user hasn't entered an amount yet — the date
    // defaults to today so a "blank" card is detected by missing amount/note.
    if (!amount && !note) return;

    const row = { side, date };
    let rowOK = true;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) rowOK = false;
    if (!CASH_SIDES.includes(side))         rowOK = false;
    const a = parseFloat(amount);
    if (!(a > 0)) rowOK = false;
    else row.amount = a;
    if (note) row.note = note;

    card.classList.toggle('invalid', !rowOK);
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
    const list = document.getElementById('cash-list');
    list.innerHTML = '';
    list.appendChild(makeCashCard());
  }
  updateCashPreview();
}

// ---- Grid editor (open / add / tp) ----

const GRID_OPS = { open: "开仓", add: "加仓", tp: "回本" };
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
  // 操作记录 (per-asset ops log) — does NOT depend on latestRec, safe to
  // call before history.json loads (avoids the race fixed at 7ecd8e7).
  renderGridOpsLog(state);
  // Refresh the cash-management summary table.  Market-value cells read
  // from latestRec (the same data shown in 最新持仓's tables); balance
  // cells use originalHoldings (the cached holdings.json snapshot).  This
  // keeps 资金管理 consistent with the displayed 最新持仓.  The latestRec
  // guard avoids wiping cells to ¥0.00 if this grid fetch races the
  // initial history.json load.
  if (latestRec) renderCashPoolSummary(latestRec, originalHoldings);
}

// Render the per-asset grid operations log under the strategy chart.
// Each row = one open/add/tp event. Sorted date desc. Up to 10 visible
// (container clips with vertical scroll). Two source tiers now that the
// engine stopped trading:
//   manual   → orange chip 手动  (**唯一的实盘来源**: page editor)
//   backtest → purple chip 回测  (SIMULATED: replayed from historical prices,
//                                   see scripts/backfill_grid_ops.py)
// "auto" is a legacy value from the auto-trading era — the migration
// (scripts/reset_grid_positions.py) removed those rows; the mapping is kept
// so an old state file isn't mislabelled as 手动.
// tp ops show per-event realized P&L (green positive / red negative);
// open/add show —.
function renderGridOpsLog(state) {
  const root = document.getElementById('grid-ops-scroll');
  if (!root) return;
  // Flatten per-asset ops into one sortable list. Skip assets with no
  // dict-shaped state (defensive — grid_state.json root is dict of dicts).
  const rows = [];
  for (const [name, asset] of Object.entries(state || {})) {
    if (!asset || typeof asset !== 'object') continue;
    const ops = Array.isArray(asset.ops) ? asset.ops : [];
    for (const op of ops) rows.push({ ...op, _asset: name });
  }
  // Sort: date desc first, then asset name asc for stable intra-day order.
  rows.sort((a, b) => {
    const d = (b.date || '').localeCompare(a.date || '');
    return d !== 0 ? d : a._asset.localeCompare(b._asset);
  });
  if (rows.length === 0) {
    root.innerHTML = '<p class="grid-ops-empty">暂无操作记录</p>';
    const stale = document.getElementById('grid-ops-footer');
    if (stale) stale.textContent = '';
    return;
  }
  // Render ALL rows; container clips to ~10 visible via max-height: 360px.
  // (User asked "界面显示最多10笔，通过滚动条查看" — DOM may hold more.)
  const OP_LABELS = { open: '开仓', add: '加仓', tp: '回本' };
  const SRC_LABELS = { manual: '手动', backtest: '回测', auto: '旧自动' };
  // Whitelist: `src` lands in a class attribute, so never interpolate the
  // raw value — an unknown source falls back to the manual tier.
  const SRC_TIERS = ['manual', 'backtest', 'auto'];
  const body = rows.map(r => {
    const isTp = r.op === 'tp';
    // 出场时归并到再平衡持仓的份额数 —— 新规则下这才是"出场"的实际结果,
    // 已实现盈亏恒为 0 (除非低于成本手动出场, 那时才是负数)。
    const toMerge = (typeof r.shares_to_merge === 'number') ? r.shares_to_merge : 0;
    let pnlCell;
    if (isTp && typeof r.pnl === 'number') {
      if (r.pnl === 0 && toMerge > 0) {
        // "+¥0.00" 会被读成"没赚到", 实际是"浮盈转成了再平衡持仓的份额"。
        pnlCell = '<td class="col-pnl muted">¥0.00</td>';
      } else {
        const cls = r.pnl >= 0 ? 'pos' : 'neg';
        const sign = r.pnl >= 0 ? '+' : '';
        pnlCell = `<td class="col-pnl ${cls}">${sign}¥${r.pnl.toFixed(2)}</td>`;
      }
    } else {
      pnlCell = '<td class="col-pnl muted">—</td>';
    }
    const src = SRC_TIERS.includes(r.source) ? r.source : 'manual';
    const srcLabel = SRC_LABELS[src];
    const opText = OP_LABELS[r.op] || r.op || '';
    const sharesText = (typeof r.shares === 'number')
      ? Math.round(r.shares).toLocaleString()
      : '';
    let noteText = r.note || '';   // empty string renders as empty cell
    if (toMerge > 0) {
      noteText += (noteText ? ' · ' : '') +
        `归并再平衡 ${Math.round(toMerge).toLocaleString()} 份`;
    }
    return `<tr>
      <td>${escapeHtml(r.date || '')}</td>
      <td>${escapeHtml(r._asset)}</td>
      <td class="col-op"><span class="src-chip ${src}">${srcLabel}</span> ${escapeHtml(opText)}</td>
      <td>¥${(typeof r.price === 'number' ? r.price : 0).toFixed(4)}</td>
      <td class="col-shares">${sharesText}</td>
      ${pnlCell}
      <td>${escapeHtml(noteText)}</td>
    </tr>`;
  }).join('');
  root.innerHTML = `
    <table class="grid-ops-table">
      <thead><tr>
        <th>日期</th><th>资产</th><th>操作</th>
        <th>价格</th><th>数量</th><th>盈亏</th><th>备注</th>
      </tr></thead>
      <tbody>${body}</tbody>
    </table>
  `;
  // Footer: total + a per-tier breakdown, so a long backtest-dominated log
  // makes its own composition obvious without scrolling.
  const foot = document.getElementById('grid-ops-footer');
  if (foot) {
    const counts = {};
    for (const r of rows) {
      const k = SRC_TIERS.includes(r.source) ? r.source : 'manual';
      counts[k] = (counts[k] || 0) + 1;
    }
    foot.textContent = '共 ' + rows.length + ' 笔 · ' + SRC_TIERS
      .filter(k => counts[k])
      .map(k => SRC_LABELS[k] + ' ' + counts[k])
      .join(' · ');
  }
}

function refreshGridPreview() {
  const el = document.getElementById('grid-preview');
  if (!el || !currentGridState) { if (el) el.textContent = ''; return; }
  // 网格的资产清单以 **grid_state.json 的 key** 为准 —— 那是引擎的宇宙。
  // 页面里那份 ETF_CATALOG 只是交易表单下拉框的候选项, 不是网格的清单:
  // 引擎换篮子时, 这一行会跟着 grid_state 走, 不会漏掉新资产。
  const parts = Object.entries(currentGridState).map(([name, s]) => {
    const code = codeForName(name, latestRec);
    // 已归并到再平衡持仓的份额 —— 它们不再属于网格, 只是记在这儿方便对照。
    const mergedShares = Number((s && s.merged_shares) || 0);
    const mergedTail = mergedShares > 0
      ? ` (已归并 ${Math.round(mergedShares).toLocaleString()} 份)`
      : '';
    if (!s || !s.in_cycle) {
      // 不在周期里 = 网格不持有该资产 (出场时本金全额收回)。
      return `${code} 待开仓${mergedTail}`;
    }
    const n = (s.entries || []).length;
    const invested = (s.entries || []).reduce(
      (acc, e) => acc + e.shares * e.price, 0,
    );
    const total_sh = (s.entries || []).reduce((acc, e) => acc + e.shares, 0);
    const avg = total_sh > 0 ? (invested / total_sh).toFixed(3) : '0';
    return `${code} 持仓中 (${n} 层, 均价 ¥${avg}${mergedTail})`;
  });
  el.textContent = `当前复合网格: ${parts.join(' | ')}`;
}

function makeGridCard(t = {}) {
  const card = document.createElement('div');
  card.className = 'trade-card';
  const today = new Date().toISOString().slice(0, 10);
  // 默认选 open —— 网格从零开始, 新一行最常见的动作就是开仓。
  // (曾经默认 'add', 而引擎不再自动开仓, 于是新行的第一件事老是要改成 open。)
  const op = GRID_OPS[t.op] ? t.op : 'open';
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

// 网格 op 的前置条件 —— 与 scripts/apply_grid.py:inline_manual_op 的服务端校验
// 一一对应。引擎不再自动交易, 开/加/回本只能在这里提交, 所以前置条件必须
// 在前端先说清楚, 否则整批操作会被服务端静默拒绝 (只有 Actions 日志能看到)。
function gridOpPrecondition(code, op, inCycle) {
  if (op === 'open') {
    return inCycle
      ? `${code} 当前已有周期 (持仓中), 开仓要求先回本出场 (tp)`
      : null;
  }
  if (op === 'add' || op === 'tp') {
    return inCycle
      ? null
      : `${code} 当前不在周期内 (待开仓), 请先提交 open, 再 add / tp`;
  }
  return `${code}: 未知操作 ${op}`;
}

function collectGridRows() {
  // 返回 {ops, errors}
  //   ops    — 校验通过的操作数组
  //   errors — [{card, msg}], 有错时整批不提交
  const cards = [...document.querySelectorAll('#grid-list .trade-card')];
  const ops = [];
  const errors = [];

  // 按提交顺序模拟服务端状态 (服务端也是顺序应用同一资产的连续操作)。
  // sim[code] = 该资产在"本批次执行到当前行"之后的 in_cycle。
  const sim = {};
  const inCycleOf = (code) => {
    if (code in sim) return sim[code];
    const name = ETF_CATALOG[code];
    const s = name ? (currentGridState || {})[name] : null;
    sim[code] = !!(s && s.in_cycle);
    return sim[code];
  };

  cards.forEach((card) => {
    const get = k => (card.querySelector(`[data-k="${k}"]`) || {}).value || '';
    const date   = get('date').trim();
    const code   = get('code').trim();
    const op     = get('op').trim();
    const price  = get('price').trim();
    const shares = get('shares').trim();
    const note   = get('note').trim();

    // ---- 空白卡片跳过 ----
    // 之前判的是 "op/price/shares/note 全空", 但 op 有默认值 ('add')、
    // date/code 也有默认值 → 条件永远为假 → 一张没填的卡片会卡死整批。
    // 正确判据: 用户真正要填的三个字段 (价格 / 数量 / 备注) 都没动。
    if (!price && !shares && !note) return;

    const row = { date, code, op };
    let rowOK = true, msg = '';
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) { rowOK = false; msg = '日期必须是 YYYY-MM-DD'; }
    if (!GRID_OPS[op]) { rowOK = false; msg = msg || `未知操作 ${op}`; }
    if (!(code in ETF_CATALOG)) { rowOK = false; msg = msg || `未知资产 ${code}`; }
    const p = parseFloat(price);
    if (!(p > 0)) { rowOK = false; msg = msg || '价格必须 > 0'; } else { row.price = p; }
    if (op === 'tp') {
      row.shares = 0;   // 服务端忽略 tp 的数量
    } else {
      const s = parseFloat(shares);
      if (!(s > 0)) { rowOK = false; msg = msg || `${op} 必须填数量 (> 0)`; } else { row.shares = s; }
    }
    if (note) row.note = note;

    // ---- 前置条件 (与服务端一致) ----
    if (rowOK) {
      const pre = gridOpPrecondition(code, op, inCycleOf(code));
      if (pre) { rowOK = false; msg = pre; }
    }

    if (rowOK) {
      // 推进模拟状态: open → 进入周期; tp → 结束周期; add → 不变
      if (op === 'open') sim[code] = true;
      else if (op === 'tp') sim[code] = false;
      ops.push(row);
    } else {
      errors.push({ card, msg });
    }
    // 每次都重算红框 —— 上一轮标红的行, 用户改好了就得把红框去掉
    // (只 add 不 remove 的话, 卡片会一直红着, 看着像还没通过校验)。
    card.classList.toggle('invalid', !rowOK);
  });
  return { ops, errors };
}

async function submitGridOp() {
  const statusEl = document.getElementById('grid-status');
  const btn = document.getElementById('grid-save-btn');
  const { ops, errors } = collectGridRows();
  if (errors.length) {
    statusEl.className = 'err';
    statusEl.textContent = '❌ ' + errors[0].msg
      + (errors.length > 1 ? ` (另有 ${errors.length - 1} 处问题, 都已标红)` : '');
    return;
  }
  if (ops.length === 0) {
    statusEl.className = 'err';
    statusEl.textContent = '❌ 没有要提交的操作, 请先点「+ 加一行」录入复合网格操作.';
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
        `✅ 已派发 ${ops.length} 个复合网格操作 (Action 约 30 秒后 commit). ` +
        `若 1 分钟内「操作记录」没有新增, 说明批次被服务端拒绝 —— ` +
        `请到 Actions 日志看 apply_grid 的报错.`;
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
    document.getElementById('cash-list').appendChild(makeCashCard());
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
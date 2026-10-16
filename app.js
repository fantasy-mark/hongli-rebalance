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
    return `<tr>
      <td>${s.name}</td>
      <td class="${sigClass}">${sigText}</td>
      <td>${s.num_buys}</td>
      <td>¥${s.avg_cost.toFixed(3)}</td>
      <td>¥${s.price.toFixed(3)}</td>
      <td class="${s.unrealized_pnl >= 0 ? 'ok' : 'warn'}">${s.return_pct >= 0 ? '+' : ''}${s.return_pct.toFixed(2)}%</td>
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
          <th>名称</th><th>信号</th><th>层数</th><th>均价</th><th>现价</th><th>浮盈%</th>
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

let originalHoldings = null;   // last fetched snapshot for "reset"

function renderEditor(holdings) {
  originalHoldings = JSON.parse(JSON.stringify(holdings));  // deep copy
  const tbody = document.getElementById('holdings-tbody');
  tbody.innerHTML = '';
  for (const p of holdings.positions) {
    tbody.appendChild(makeRow(p));
  }
  document.getElementById('cash-input').value = holdings.cash ?? 0;
  document.getElementById('save-status').textContent = '';
  document.getElementById('save-status').className = 'muted';
}

function makeRow(p = { name: '', code: '', shares: 0 }) {
  const tr = document.createElement('tr');
  tr.innerHTML = `
    <td><input data-k="name"   type="text"   value="${escapeAttr(p.name)}" placeholder="名称" /></td>
    <td>
      <input data-k="code"   type="text"   list="etf-codes" value="${escapeAttr(p.code)}"
             placeholder="6 位代码" maxlength="6" pattern="\\d{6}" />
      <datalist id="etf-codes">
        ${Object.entries(ETF_CATALOG).map(([c, n]) => `<option value="${c}">${n}</option>`).join('')}
      </datalist>
    </td>
    <td><input data-k="shares" type="number" value="${p.shares ?? 0}" min="0" step="1" style="text-align:right;" /></td>
    <td class="del-cell"><button type="button" class="row-del-btn" title="删除该行">✕</button></td>
  `;
  tr.querySelector('.row-del-btn').addEventListener('click', () => tr.remove());
  return tr;
}

function escapeAttr(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function collectRows() {
  const rows = [...document.querySelectorAll('#holdings-tbody tr')].map(tr => {
    const get = k => tr.querySelector(`[data-k="${k}"]`).value;
    return {
      name:   get('name').trim(),
      code:   get('code').trim(),
      shares: parseFloat(get('shares')) || 0,
    };
  }).filter(r => r.code);   // drop rows with empty code
  return rows;
}

async function loadHoldings() {
  const res = await fetch(HOLDINGS_URL, { cache: 'no-store' });
  if (!res.ok) throw new Error(`HTTP ${res.status} loading ${HOLDINGS_URL}`);
  return await res.json();
}

async function saveHoldings() {
  const status = document.getElementById('save-status');
  const btn = document.getElementById('save-btn');
  const rows = collectRows();
  const cash = parseFloat(document.getElementById('cash-input').value) || 0;

  // Client-side validation (server re-validates too)
  for (const r of rows) {
    if (!/^\d{6}$/.test(r.code)) {
      status.className = 'err';
      status.textContent = `❌ 代码必须是 6 位数字: "${r.code}"`;
      return;
    }
    if (r.shares < 0) {
      status.className = 'err';
      status.textContent = `❌ 数量不能为负: ${r.code}`;
      return;
    }
  }
  if (cash < 0) {
    status.className = 'err';
    status.textContent = '❌ 现金不能为负';
    return;
  }

  // Auth check
  const pat = getPat();
  if (!pat) {
    promptForPat();
    status.className = 'err';
    status.textContent = '❌ 需要先设置 GitHub PAT (页面顶部的提示).';
    return;
  }

  btn.disabled = true;
  status.className = 'muted';
  status.textContent = '提交中…';

  try {
    const res = await fetch(DISPATCH_URL, {
      method: 'POST',
      headers: {
        'Accept': 'application/vnd.github+json',
        'Authorization': `Bearer ${pat}`,
      },
      body: JSON.stringify({
        event_type: 'update_holdings',
        client_payload: { rows, cash },
      }),
    });
    if (res.status === 204 || res.ok) {
      status.className = 'ok';
      status.textContent = '✅ 已提交. Action 约 30 秒后 commit 完成, 邮件会基于新持仓重发. 刷新页面查看新数据.';
    } else {
      const text = await res.text();
      if (res.status === 401 || res.status === 403) {
        status.className = 'err';
        status.textContent = `❌ HTTP ${res.status} 认证失败 — PAT 无效/过期/或没勾选 Contents:Write. 顶部点「设置 PAT」重试.`;
        updatePatBadge();
      } else {
        status.className = 'err';
        status.textContent = `❌ 提交失败: HTTP ${res.status} — ${text.slice(0, 200)}`;
      }
    }
  } catch (e) {
    status.className = 'err';
    status.textContent = `❌ 网络错误: ${e.message}`;
  } finally {
    btn.disabled = false;
  }
}

function initEditor() {
  document.getElementById('add-row-btn').addEventListener('click', () => {
    document.getElementById('holdings-tbody').appendChild(makeRow());
  });
  document.getElementById('reset-btn').addEventListener('click', () => {
    if (originalHoldings) renderEditor(originalHoldings);
  });
  document.getElementById('save-btn').addEventListener('click', saveHoldings);

  // Load initial state
  loadHoldings()
    .then(renderEditor)
    .catch(e => {
      const s = document.getElementById('save-status');
      s.className = 'err';
      s.textContent = `加载失败: ${e.message}`;
    });

  updatePatBadge();
}

main();
initEditor();
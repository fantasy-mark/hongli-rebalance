/* 策略追踪站点前端逻辑
 * 数据来源: data/site/summary.json, data/site/trades.json
 * 渲染: ① 全景图摘要 ② 回测交易记录 ③ 持仓（再平衡 / 网格 分开） ④ 策略说明
 */

const ASSET_COLORS = {
  '515180': '#C0392B',
  '159937': '#C9A961',
  '511380': '#2C5F8D',
  '513300': '#48C9B0',
};

const ACTION_LABELS = {
  open: '建仓',
  add: '加仓',
  reduce: '减仓',
  take_profit: '止盈',
  buy: '买入',
  sell: '卖出',
};

const SIDE_LABELS = { buy: '买入', sell: '卖出' };

const STRATEGY_LABELS = {
  grid: '复合网格',
  rebalance: '阈值再平衡',
};

const state = {
  summary: null,
  trades: [],
  strategyFilter: '',
  assetFilter: '',
  sideFilter: '',
};

/* ------------------------------------------------------------------ 工具 */
const pct = (v, digits = 2) =>
  `${v >= 0 ? '+' : ''}${(v * 100).toFixed(digits)}%`;

/* 无符号百分比（波动率、占比等） */
const plainPct = (v, digits = 2) => `${(v * 100).toFixed(digits)}%`;

const money = (v, digits = 0) =>
  '¥' + Number(v).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });

const shares = (v) =>
  Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 0 });

const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));

const colorOf = (code) => ASSET_COLORS[code] || '#8E44AD';

const signClass = (v) => (v >= 0 ? 'up' : 'down');

/* ------------------------------------------------------------------ 启动 */
document.addEventListener('DOMContentLoaded', init);

async function init() {
  const badge = document.getElementById('status-badge');
  try {
    const [summaryRes, tradesRes] = await Promise.all([
      fetch('data/site/summary.json'),
      fetch('data/site/trades.json').catch(() => null),
    ]);

    if (!summaryRes.ok) throw new Error(`summary.json HTTP ${summaryRes.status}`);
    state.summary = await summaryRes.json();
    state.trades = tradesRes && tradesRes.ok
      ? ((await tradesRes.json()).trades || [])
      : [];

    renderStatus(badge, state.summary);
    renderChartSummary(state.summary);
    setupTradeFilters();
    renderTrades();
    renderHoldings();
    renderStrategySpecs(state.summary);
  } catch (err) {
    console.error(err);
    badge.className = 'err';
    badge.innerHTML =
      `<span class="status-main">数据加载失败：${esc(err.message)}</span>` +
      `<span class="status-error">请先运行 <code>python scripts/build_pages.py</code> 生成 pages/data/</span>`;
  }
}

/* ------------------------------------------------------- 状态徽章 */
function renderStatus(badge, s) {
  badge.className = 'ok';
  badge.innerHTML =
    `<span class="status-main">数据更新 ${esc(s.generated_at)}` +
    ` · 区间 ${esc(s.window.start)} → ${esc(s.window.end)}</span>` +
    `<span class="status-breakdown">` +
    `<span class="status-pool">组合 <strong>${pct(s.combined.total_return)}</strong></span>` +
    `<span class="status-pool">再平衡 <strong>${pct(s.rebalance.total_return)}</strong></span>` +
    `<span class="status-pool">网格 <strong>${pct(s.grid.total_return)}</strong></span>` +
    `</span>`;
}

/* ------------------------------------------------------- ① 全景图摘要 */
function renderChartSummary(s) {
  const box = document.getElementById('chart-summary');
  const grid = s.grid;
  const chips = [
    ['组合合计', s.combined.total_return, money(s.combined.total_value)],
    ['再平衡', s.rebalance.total_return, money(s.rebalance.total_value)],
    ['网格', grid.total_return, money(grid.total_value)],
  ];

  let html = chips
    .map(([label, ret, val]) => `
      <span class="chip">
        <span class="chip-label">${esc(label)}</span>
        <span class="${signClass(ret)}">${pct(ret)}</span>
        <span class="chip-label">${esc(val)}</span>
      </span>`)
    .join('');

  if (grid.completed_rounds !== undefined) {
    html += `
      <span class="chip">
        <span class="chip-label">网格轮次</span>
        <strong>${grid.completed_rounds}/${grid.total_rounds}</strong>
        <span class="chip-label">完成 · 买 ${grid.buy_count} 卖 ${grid.sell_count}</span>
      </span>`;
  }

  box.innerHTML = html;
}

/* ------------------------------------------------------- ② 回测交易记录 */
function setupTradeFilters() {
  const strategySel = document.getElementById('trade-strategy-filter');
  const assetSel = document.getElementById('trade-asset-filter');
  const sideSel = document.getElementById('trade-side-filter');

  // 策略下拉：按出现顺序
  const strategies = [...new Set(state.trades.map((t) => t.strategy))];
  const strategyNameOf = {};
  state.trades.forEach((t) => { strategyNameOf[t.strategy] = t.strategy_name; });

  strategies.forEach((key) => {
    const opt = document.createElement('option');
    opt.value = key;
    opt.textContent = strategyNameOf[key] || STRATEGY_LABELS[key] || key;
    strategySel.appendChild(opt);
  });

  const codes = [...new Set(state.trades.map((t) => t.asset_code))].sort();
  const nameOf = {};
  state.trades.forEach((t) => { nameOf[t.asset_code] = t.asset_name; });

  codes.forEach((code) => {
    const opt = document.createElement('option');
    opt.value = code;
    opt.textContent = nameOf[code] || code;
    assetSel.appendChild(opt);
  });

  strategySel.addEventListener('change', () => {
    state.strategyFilter = strategySel.value;
    renderTrades();
  });
  assetSel.addEventListener('change', () => {
    state.assetFilter = assetSel.value;
    renderTrades();
  });
  sideSel.addEventListener('change', () => {
    state.sideFilter = sideSel.value;
    renderTrades();
  });
}

function filteredTrades() {
  return state.trades.filter((t) => {
    if (state.strategyFilter && t.strategy !== state.strategyFilter) return false;
    if (state.assetFilter && t.asset_code !== state.assetFilter) return false;
    if (state.sideFilter && t.side !== state.sideFilter) return false;
    return true;
  });
}

function renderTrades() {
  const container = document.getElementById('trades-table');
  const counter = document.getElementById('trade-count');
  const chronologicalTrades = filteredTrades();

  const gridCount = chronologicalTrades.filter((t) => t.strategy === 'grid').length;
  const rebCount = chronologicalTrades.length - gridCount;
  counter.textContent =
    `共 ${chronologicalTrades.length} 笔（网格 ${gridCount} · 再平衡 ${rebCount}）`;

  if (!chronologicalTrades.length) {
    container.innerHTML = '<p class="empty-note">暂无符合条件的交易记录</p>';
    return;
  }

  // 计算每轮的深度：按(资产,轮次)分组，每笔买入深度+1
  // 必须在正序（时间早->晚）上计算，否则早的买入会被编号为大数字
  const depthByRound = {};
  chronologicalTrades.forEach((t) => {
    if (!t.round_no) return;
    const key = `${t.asset_code}-${t.round_no}`;
    if (!(key in depthByRound)) depthByRound[key] = 0;
    if (t.side === 'buy') {
      depthByRound[key] += 1;
      t.depth = depthByRound[key];
    }
  });

  const reversedTrades = chronologicalTrades.slice().reverse();
  const body = reversedTrades
    .map((t) => {
      const action = ACTION_LABELS[t.action] || t.action || '';
      const side = SIDE_LABELS[t.side] || t.side || '';
      const sideCls = t.side === 'sell' ? 'side-sell' : 'side-buy';
      const stratName = t.strategy_name || STRATEGY_LABELS[t.strategy] || t.strategy;
      const stratCls = t.strategy === 'grid' ? 'strat-grid' : 'strat-rebalance';
      // 步长：网格专有，显示绝对值 + 占价格百分比（如 0.0231 (0.62%)）
      let stepCell = '<td class="num muted">—</td>';
      if (t.strategy === 'grid' && t.step !== null && t.step !== undefined) {
        const stepPct = (t.step / t.price * 100).toFixed(2);
        stepCell = `<td class="num">${Number(t.step).toFixed(4)}<br><span class="muted" style="font-size:0.85em">(${stepPct}%)</span></td>`;
      }
      // 轮次/深度：买入显示 轮次/深度（每买一次深度+1），卖出/再平衡只显示轮次
      let roundCell = '<td class="num muted">—</td>';
      if (t.round_no) {
        roundCell = t.depth
          ? `<td class="num">${t.round_no}/${t.depth}</td>`
          : `<td class="num">${t.round_no}</td>`;
      }

      return `
        <tr>
          <td>${esc(t.date)}</td>
          <td><span class="strat-tag ${stratCls}">${esc(stratName)}</span></td>
          <td>
            <span class="asset-dot" style="background:${colorOf(t.asset_code)}"></span>
            ${esc(t.asset_name || t.asset_code)}
          </td>
          <td class="${sideCls}">${esc(side)}</td>
          <td>${esc(action)}</td>
          <td class="num">${Number(t.price).toFixed(4)}</td>
          <td class="num">${shares(t.shares)}</td>
          <td class="num">${money(t.amount, 2)}</td>
          ${stepCell}
          ${roundCell}
        </tr>`;
    })
    .join('');

  container.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>日期</th>
          <th>策略</th>
          <th>资产</th>
          <th>方向</th>
          <th>类型</th>
          <th class="num">价格</th>
          <th class="num">数量</th>
          <th class="num">金额</th>
          <th class="num">步长</th>
          <th class="num">轮次/深度</th>
        </tr>
      </thead>
      <tbody>${body}</tbody>
    </table>`;
}

/* ------------------------------------------------------- ③ 持仓 */
function renderHoldings() {
  const s = state.summary;

  renderStrategyCard('rebalance', s.rebalance);
  renderStrategyCard('grid', s.grid);

  document.getElementById('combined-holdings').innerHTML =
    holdingsTable(s.combined, '组合');
  document.getElementById('combined-stats').innerHTML =
    statBlock(s.combined, [
      ['初始资金', money(s.combined.initial_capital)],
      ['总市值', money(s.combined.total_value)],
      ['累计收益', pct(s.combined.total_return)],
      ['年化收益', pct(s.combined.annualized_return)],
      ['最大回撤', pct(s.combined.max_drawdown)],
      ['夏普比率', Number(s.combined.sharpe_ratio).toFixed(2)],
      ['波动率', plainPct(s.combined.volatility)],
      ['成交笔数', s.combined.trade_count],
    ]);
}

function renderStrategyCard(key, data) {
  const holdingsEl = document.getElementById(`${key}-holdings`);
  const statsEl = document.getElementById(`${key}-stats`);

  holdingsEl.innerHTML = holdingsTable(data, key === 'grid' ? '网格' : '再平衡');

  const stats = [
    ['初始资金', money(data.initial_capital)],
    ['总市值', money(data.total_value)],
    ['累计收益', pct(data.total_return)],
    ['年化收益', pct(data.annualized_return)],
    ['最大回撤', pct(data.max_drawdown)],
    ['夏普比率', Number(data.sharpe_ratio).toFixed(2)],
    ['波动率', plainPct(data.volatility)],
    ['成交笔数', data.trade_count],
  ];

  if (key === 'grid') {
    stats.push(
      ['买入/卖出', `${data.buy_count} / ${data.sell_count}`],
      ['完成轮次', `${data.completed_rounds}/${data.total_rounds}`],
    );
  }

  statsEl.innerHTML = statBlock(data, stats);
}

function holdingsTable(data, scopeLabel) {
  const positions = data.positions || [];
  const maxWeight = positions.reduce(
    (m, p) => Math.max(m, Math.abs(p.weight || 0)), 0) || 1;

  let rows = '';
  if (!positions.length) {
    rows = `<tr><td colspan="4" class="empty-note">当前无持仓</td></tr>`;
  } else {
    rows = positions
      .map((p) => {
        const w = Math.abs(p.weight || 0);
        const barW = Math.min(100, (w / maxWeight) * 100);
        return `
          <tr>
            <td>
              <span class="asset-dot" style="background:${colorOf(p.code)}"></span>
              ${esc(p.name || p.code)}
            </td>
            <td class="num">${shares(p.shares)}</td>
            <td class="num">${money(p.market_value)}</td>
            <td class="num">
              ${(w * 100).toFixed(1)}%
              <div class="bar-wrap"><div class="bar-fill"
                   style="width:${barW.toFixed(1)}%;background:${colorOf(p.code)}"></div></div>
            </td>
          </tr>`;
      })
      .join('');
  }

  return `
    <table>
      <thead>
        <tr>
          <th>资产</th>
          <th class="num">数量</th>
          <th class="num">市值</th>
          <th class="num">占比</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="totals">
      <div class="row"><span>持仓市值</span><span>${money(data.market_value)}</span></div>
      <div class="row"><span>现金</span><span>${money(data.cash)}</span></div>
      <div class="row grand"><span>${esc(scopeLabel)}总市值</span><span>${money(data.total_value)}</span></div>
    </div>`;
}

function statBlock(data, pairs) {
  return pairs
    .map(([k, v]) => {
      let cls = '';
      if (k === '累计收益' || k === '年化收益') {
        cls = data.total_return >= 0 ? 'up' : 'down';
      } else if (k === '最大回撤') {
        cls = 'down';
      }
      return `
        <div class="stat-item">
          <span class="k">${esc(k)}</span>
          <span class="v ${cls}">${v}</span>
        </div>`;
    })
    .join('');
}

/* ------------------------------------------------- ④ 策略说明（规格） */
function renderStrategySpecs(summary) {
  const box = document.getElementById('strategy-specs');
  const specs = summary.strategies || {};
  const order = ['rebalance', 'grid'];
  const list = order.filter((k) => specs[k]);

  if (!list.length) {
    box.innerHTML = '<p class="empty-note">暂无策略规格</p>';
    return;
  }

  box.innerHTML = list.map((key) => renderSpecCard(specs[key], key)).join('');
}

function renderSpecCard(spec, key) {
  const cls = key === 'grid' ? 'spec-grid' : 'spec-rebalance';
  const rules = spec.rules || [];

  const rulesHtml = rules.map((r) => {
    const params = (r.parameters || []).map((p) => `
        <div class="rule-param">
          <span class="rp-name">${esc(p.name)}</span>
          <span class="rp-value">${esc(p.value)}</span>
          <span class="rp-meaning">${esc(p.meaning)}</span>
        </div>`).join('');

    return `
      <div class="rule">
        <div class="rule-head">
          <span class="rule-title">${esc(r.title)}</span>
          <code class="rule-id">${esc(r.rule_id)}</code>
        </div>
        <p class="rule-desc">${esc(r.description)}</p>
        ${r.formula ? `<pre class="rule-formula">${esc(r.formula)}</pre>` : ''}
        ${params ? `<div class="rule-params">${params}</div>` : ''}
        <div class="rule-impl">
          <span class="ri-label">规则实现</span>
          <code>${esc(r.implementation)}</code>
        </div>
      </div>`;
  }).join('');

  const impls = (spec.implementation || []).map((i) =>
    `<code class="impl-ref">${esc(i)}</code>`).join('');

  return `
    <article class="spec-card ${cls}">
      <header class="spec-head">
        <div>
          <span class="spec-name">${esc(spec.name)}</span>
          <code class="spec-id">${esc(spec.strategy_id)}</code>
        </div>
        <p class="spec-summary">${esc(spec.summary)}</p>
      </header>
      <div class="rule-list">${rulesHtml}</div>
      <footer class="spec-foot">
        <span class="sf-label">策略实现位置</span>
        <div class="sf-impls">${impls}</div>
      </footer>
    </article>`;
}

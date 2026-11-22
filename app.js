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
  backtestTrades: [],  // 回测产生的交易记录（用于②回测交易记录）
  trades: [],          // 用户提交的真实交易记录（用于③真实持仓）
  prices: {},          // 各资产最新价格 {code: price}
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
    const [summaryRes, backtestTradesRes, tradesRes, pricesRes] = await Promise.all([
      fetch('data/site/summary.json'),
      fetch('data/site/backtest_trades.json').catch(() => null),
      fetch('data/site/trades.json').catch(() => null),
      fetch('data/site/prices.json').catch(() => null),
    ]);

    if (!summaryRes.ok) throw new Error(`summary.json HTTP ${summaryRes.status}`);
    state.summary = await summaryRes.json();
    state.backtestTrades = backtestTradesRes && backtestTradesRes.ok
      ? ((await backtestTradesRes.json()).trades || [])
      : [];
    state.trades = tradesRes && tradesRes.ok
      ? ((await tradesRes.json()).trades || [])
      : [];
    // 最新价格：prices.json 优先（含当日实时报价），其次回测的最后收盘价
    state.prices = (pricesRes && pricesRes.ok)
      ? ((await pricesRes.json()).prices || {})
      : (state.summary.latest_prices || {});

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
  // 解析 ISO 时间字符串，显示日期和时间
  const dt = new Date(s.generated_at);
  const dateStr = dt.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' });
  const timeStr = dt.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  badge.innerHTML =
    `<span class="status-main">数据更新 ${esc(dateStr)} ${esc(timeStr)}` +
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
  const strategies = [...new Set(state.backtestTrades.map((t) => t.strategy))];
  const strategyNameOf = {};
  state.backtestTrades.forEach((t) => { strategyNameOf[t.strategy] = t.strategy_name; });

  strategies.forEach((key) => {
    const opt = document.createElement('option');
    opt.value = key;
    opt.textContent = strategyNameOf[key] || STRATEGY_LABELS[key] || key;
    strategySel.appendChild(opt);
  });

  const codes = [...new Set(state.backtestTrades.map((t) => t.asset_code))].sort();
  const nameOf = {};
  state.backtestTrades.forEach((t) => { nameOf[t.asset_code] = t.asset_name; });

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
  return state.backtestTrades.filter((t) => {
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

  // 计算再平衡序号：按日期去重，每个再平衡日期一个序号（自回测开始第几次）
  const rebalanceDates = new Set();
  chronologicalTrades.forEach((t) => {
    if (t.strategy === 'rebalance' && t.side === 'buy') {
      rebalanceDates.add(t.date);
    }
  });
  const sortedRebalanceDates = [...rebalanceDates].sort();
  const rebalanceIndex = {};
  sortedRebalanceDates.forEach((date, i) => {
    rebalanceIndex[date] = i + 1;
  });

  // 给再平衡交易设置序号
  chronologicalTrades.forEach((t) => {
    if (t.strategy === 'rebalance') {
      t.rebalance_no = rebalanceIndex[t.date];
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
      // 轮次/深度：网格显示 轮次/深度，再平衡显示第几次再平衡
      let roundCell = '<td class="num muted">—</td>';
      if (t.strategy === 'rebalance' && t.rebalance_no) {
        roundCell = `<td class="num">${t.rebalance_no}</td>`;
      } else if (t.round_no) {
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
          <td class="num">${shares(t.position_shares)}<br><span class="muted" style="font-size:0.85em">(${shares(t.shares)})</span></td>
          <td class="num">${money(t.position_value, 2)}<br><span class="muted" style="font-size:0.85em">(${money(t.amount, 2)})</span></td>
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
          <th class="num">市值/交易金额</th>
          <th class="num">步长</th>
          <th class="num">轮次/深度</th>
        </tr>
      </thead>
      <tbody>${body}</tbody>
    </table>`;
}

/* ------------------------------------------------------- ③ 真实持仓（交易记录） */
function renderHoldings() {
  // 显示再平衡组合的交易记录（真实交易）
  const rebalanceTrades = state.trades.filter(t => t.strategy === 'rebalance');
  document.getElementById('rebalance-holdings').innerHTML = 
    tradeHistoryTable(rebalanceTrades, '再平衡', false);
  
  // 计算再平衡组合统计信息
  const rebalanceStats = calculateRebalanceStats(rebalanceTrades);
  document.getElementById('rebalance-stats').innerHTML =
    statBlock(rebalanceStats, rebalanceStats.pairs);

  // 显示网格组合的交易记录（真实交易）
  const gridTrades = state.trades.filter(t => t.strategy === 'grid');
  document.getElementById('grid-holdings').innerHTML = 
    tradeHistoryTable(gridTrades, '网格', false);
  
  // 计算网格组合统计信息
  const gridStats = calculateGridStats(gridTrades);
  document.getElementById('grid-stats').innerHTML = 
    statBlock(gridStats, gridStats.pairs);

  // 初始化表单提交处理
  initTradeForm();
}

// 计算再平衡组合统计信息
function calculateRebalanceStats(trades) {
  if (trades.length === 0) {
    return {
      pairs: [
        ['初始市值', money(0)],
        ['总市值', money(0)],
        ['累计收益', pct(0)],
        ['成交笔数', 0],
      ]
    };
  }

  // 计算初始市值：各资产**建仓**交易的市值之和
  // （再平衡策略限制只能开仓一次，所以建仓交易就是初始建仓）
  const openingTrades = trades.filter(t => t.action === 'open');
  const initialMarketValue = openingTrades.reduce((sum, t) => sum + t.amount, 0);

  // 计算当前持仓
  const positions = {};
  trades.forEach(t => {
    const asset = t.asset_code;
    if (!positions[asset]) {
      positions[asset] = { shares: 0, cost: 0 };
    }
    if (t.side === 'buy') {
      positions[asset].shares += t.shares;
      positions[asset].cost += t.amount;
    } else if (t.side === 'sell') {
      positions[asset].shares -= t.shares;
      // 按比例减少成本
      const avgCost = positions[asset].cost / (positions[asset].shares + t.shares);
      positions[asset].cost -= avgCost * t.shares;
    }
  });

  // 计算总市值（使用最新资产价格）
  const latestPrices = state.prices || state.summary?.latest_prices || {};
  let totalMarketValue = 0;
  for (const asset in positions) {
    if (positions[asset].shares > 0) {
      // 优先使用最新价格
      if (latestPrices[asset]) {
        totalMarketValue += positions[asset].shares * latestPrices[asset];
      } else {
        // 如果没有最新价格，回退到使用最后交易价格
        const assetTrades = trades.filter(t => t.asset_code === asset);
        const latestTrade = assetTrades[assetTrades.length - 1];
        totalMarketValue += positions[asset].shares * latestTrade.price;
      }
    }
  }

  // 计算累计收益
  const totalReturn = initialMarketValue > 0 ? (totalMarketValue - initialMarketValue) / initialMarketValue : 0;

  return {
    initial_capital: initialMarketValue,
    total_value: totalMarketValue,
    total_return: totalReturn,
    trade_count: trades.length,
    pairs: [
      ['初始市值', money(initialMarketValue)],
      ['总市值', money(totalMarketValue)],
      ['累计收益', pct(totalReturn)],
      ['成交笔数', trades.length],
    ]
  };
}

// 计算网格组合统计信息
function calculateGridStats(trades) {
  if (trades.length === 0) {
    return {
      pairs: [
        ['初始资金', money(0)],
        ['总市值', money(0)],
        ['累计收益', pct(0)],
        ['成交笔数', 0],
        ['买入/卖出', '0 / 0'],
      ]
    };
  }

  // 计算初始资金（所有买入交易的金额之和）
  const buyTrades = trades.filter(t => t.side === 'buy');
  const initialCapital = buyTrades.reduce((sum, t) => sum + t.amount, 0);

  // 计算当前持仓
  const positions = {};
  trades.forEach(t => {
    const asset = t.asset_code;
    if (!positions[asset]) {
      positions[asset] = { shares: 0, cost: 0 };
    }
    if (t.side === 'buy') {
      positions[asset].shares += t.shares;
      positions[asset].cost += t.amount;
    } else if (t.side === 'sell') {
      positions[asset].shares -= t.shares;
      const avgCost = positions[asset].cost / (positions[asset].shares + t.shares);
      positions[asset].cost -= avgCost * t.shares;
    }
  });

  // 计算总市值（使用最新资产价格）
  const latestPrices = state.prices || state.summary?.latest_prices || {};
  let totalMarketValue = 0;
  for (const asset in positions) {
    if (positions[asset].shares > 0) {
      // 优先使用最新价格
      if (latestPrices[asset]) {
        totalMarketValue += positions[asset].shares * latestPrices[asset];
      } else {
        // 如果没有最新价格，回退到使用最后交易价格
        const assetTrades = trades.filter(t => t.asset_code === asset);
        const latestTrade = assetTrades[assetTrades.length - 1];
        totalMarketValue += positions[asset].shares * latestTrade.price;
      }
    }
  }

  // 计算累计收益
  const totalReturn = initialCapital > 0 ? (totalMarketValue - initialCapital) / initialCapital : 0;

  // 统计买入/卖出次数
  const buyCount = trades.filter(t => t.side === 'buy').length;
  const sellCount = trades.filter(t => t.side === 'sell').length;

  return {
    initial_capital: initialCapital,
    total_value: totalMarketValue,
    total_return: totalReturn,
    trade_count: trades.length,
    buy_count: buyCount,
    sell_count: sellCount,
    pairs: [
      ['初始资金', money(initialCapital)],
      ['总市值', money(totalMarketValue)],
      ['累计收益', pct(totalReturn)],
      ['成交笔数', trades.length],
      ['买入/卖出', `${buyCount} / ${sellCount}`],
    ]
  };
}

/* ------------------------------------------------------- 交易表单处理 */
function initTradeForm() {
  const form = document.getElementById('trade-form');
  if (!form) return;

  // 设置日期输入框默认值为今天
  const dateInput = document.getElementById('trade-date');
  if (dateInput) {
    const today = new Date().toISOString().split('T')[0];
    dateInput.value = today;
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    
    const date = document.getElementById('trade-date').value;
    const strategy = document.getElementById('trade-strategy').value;
    const asset = document.getElementById('trade-asset').value;
    const action = document.getElementById('trade-action').value;
    const shares = parseFloat(document.getElementById('trade-shares').value);
    const price = parseFloat(document.getElementById('trade-price').value);

    // 构建交易记录
    const newTrade = {
      date: date,
      strategy: strategy,
      strategy_name: strategy === 'rebalance' ? '阈值再平衡' : '复合网格',
      asset_code: asset,
      asset_name: {
        '515180': '红利ETF',
        '159937': '黄金ETF',
        '511380': '可转债ETF',
        '513300': '纳指ETF'
      }[asset],
      action: action,
      side: (action === 'reduce' || action === 'take_profit') ? 'sell' : 'buy',
      round_no: 0,
      price: price,
      shares: shares,
      amount: shares * price,
      step: null,
      position_value: null,
      position_shares: null
    };

    try {
      const response = await fetch('/api/trades', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newTrade)
      });

      if (response.ok) {
        // 添加到本地状态并重新渲染
        state.trades.push(newTrade);
        renderHoldings();
        form.reset();
        // 重新设置日期默认值
        const today = new Date().toISOString().split('T')[0];
        dateInput.value = today;
        alert('交易记录添加成功！');
      } else {
        alert('添加失败，请重试');
      }
    } catch (error) {
      console.error('Error:', error);
      alert('添加失败：' + error.message);
    }
  });
}

function tradeHistoryTable(trades, scopeLabel, isBacktest = true) {
  // 按时间倒序显示
  const sortedTrades = [...trades].sort((a, b) => b.date.localeCompare(a.date));
  
  let rows = '';
  if (!sortedTrades.length) {
    const colSpan = isBacktest ? 10 : 6;
    rows = `<tr><td colspan="${colSpan}" class="empty-note">暂无交易记录</td></tr>`;
  } else if (isBacktest) {
    // 回测交易记录 - 显示完整信息
    rows = sortedTrades
      .map((t) => {
        const action = ACTION_LABELS[t.action] || t.action || '';
        const side = SIDE_LABELS[t.side] || t.side || '';
        const sideCls = t.side === 'sell' ? 'side-sell' : 'side-buy';
        const stratName = t.strategy_name || STRATEGY_LABELS[t.strategy] || t.strategy;
        const stratCls = t.strategy === 'grid' ? 'strat-grid' : 'strat-rebalance';
        // 步长：网格专有，显示绝对值 + 占价格百分比
        let stepCell = '<td class="num muted">—</td>';
        if (t.strategy === 'grid' && t.step !== null && t.step !== undefined) {
          const stepPct = (t.step / t.price * 100).toFixed(2);
          stepCell = `<td class="num">${Number(t.step).toFixed(4)}<br><span class="muted" style="font-size:0.85em">(${stepPct}%)</span></td>`;
        }
        // 轮次/深度：网格显示 轮次/深度，再平衡显示第几次再平衡
        let roundCell = '<td class="num muted">—</td>';
        if (t.strategy === 'rebalance' && t.rebalance_no) {
          roundCell = `<td class="num">${t.rebalance_no}</td>`;
        } else if (t.round_no) {
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
            <td class="num">${shares(t.position_shares)}<br><span class="muted" style="font-size:0.85em">(${shares(t.shares)})</span></td>
            <td class="num">${money(t.position_value, 2)}<br><span class="muted" style="font-size:0.85em">(${money(t.amount, 2)})</span></td>
            ${stepCell}
            ${roundCell}
          </tr>`;
      })
      .join('');
  } else {
    // 真实交易记录 - 合并方向和类型为交易类型
    rows = sortedTrades
      .map((t) => {
        // 账本投影已翻译好业务语义（action_label）
        const action = t.action_label || ACTION_LABELS[t.action] || t.action || '';
        const sideCls = t.side === 'sell' ? 'side-sell' : 'side-buy';
        return `
          <tr>
            <td>${esc(t.date)}</td>
            <td>
              <span class="asset-dot" style="background:${colorOf(t.asset_code)}"></span>
              ${esc(t.asset_name || t.asset_code)}
            </td>
            <td class="${sideCls}">${esc(action)}</td>
            <td class="num">${shares(t.shares)}</td>
            <td class="num">${Number(t.price).toFixed(4)}</td>
          </tr>`;
      })
      .join('');
  }

  if (isBacktest) {
    return `
      <table>
        <thead>
          <tr>
            <th>日期</th>
            <th>策略</th>
            <th>资产</th>
            <th>方向</th>
            <th>类型</th>
            <th class="num">价格</th>
            <th class="num">总数量/本次数量</th>
            <th class="num">市值/交易金额</th>
            <th class="num">步长</th>
            <th class="num">轮次/深度</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
      <p class="muted" style="font-size:0.9em; margin-top:0.5em;">
        共 ${sortedTrades.length} 笔${scopeLabel}交易记录
      </p>`;
  } else {
    return `
      <table>
        <thead>
          <tr>
            <th>日期</th>
            <th>资产</th>
            <th>交易类型</th>
            <th class="num">数量</th>
            <th class="num">价格</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
      <p class="muted" style="font-size:0.9em; margin-top:0.5em;">
        共 ${sortedTrades.length} 笔${scopeLabel}交易记录
      </p>`;
  }
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

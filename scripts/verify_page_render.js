/**
 * 页面渲染校验（Node，无浏览器）
 *
 * 用极简 DOM 桩加载真实的 app.js，跑一遍 renderHoldings()，
 * 检查真实持仓区的关键数字是否与账本一致。
 *
 * 用法: node scripts/verify_page_render.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');

/* ---------------------------------------------------------- DOM 桩 */
const elements = {};
function makeElement(id) {
  return {
    id,
    innerHTML: '',
    textContent: '',
    value: '',
    className: '',
    style: {},
    addEventListener() {},
    appendChild() {},
    setAttribute() {},
  };
}

const document = {
  getElementById(id) {
    if (!elements[id]) elements[id] = makeElement(id);
    return elements[id];
  },
  createElement() { return makeElement('created'); },
  addEventListener() {},
  querySelector() { return makeElement('q'); },
  querySelectorAll() { return []; },
};

/* ---------------------------------------------------------- 运行环境 */
const sandbox = {
  document,
  window: {},
  console,
  fetch: async () => ({ ok: true, json: async () => ({}) }),
  alert() {},
  setTimeout,
  Date,
  Math,
  Number,
  JSON,
  parseFloat,
  parseInt,
  isNaN,
  module: undefined,
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;
vm.createContext(sandbox);

function loadScript(file) {
  const code = fs.readFileSync(path.join(ROOT, file), 'utf8');
  vm.runInContext(code, sandbox, { filename: file });
}

loadScript('ledger-stats.js');
loadScript('app.js');

/* app.js 的 const 声明不会挂到 global 上，用同上下文的表达式取出绑定 */
vm.runInContext(
  'globalThis.__app = { state, normalizeLedgerTrades, renderHoldings, ' +
  'money, pct, plainPct, shares, esc, initialCapitalOf, LedgerStats, ' +
  'tradeHistoryTable, statBlock, poolStatPairs, assetDetailTable };',
  sandbox);

/* ---------------------------------------------------------- 准备数据 */
const ledger = JSON.parse(
  fs.readFileSync(path.join(ROOT, 'data', 'trades.json'), 'utf8'));
const pricePayload = JSON.parse(
  fs.readFileSync(path.join(ROOT, 'data', 'site', 'prices.json'), 'utf8'));
const prices = pricePayload.prices;
const summaryData = JSON.parse(
  fs.readFileSync(path.join(ROOT, 'data', 'site', 'summary.json'), 'utf8'));

// 模拟页面加载完成后的状态
const app = sandbox.__app;
app.state.summary = summaryData;
app.state.assetNames = summaryData.asset_names || {};
app.state.priceDates = summaryData.latest_price_dates || {};
app.state.prices = prices;
app.state.rawPrices = pricePayload.raw_prices || null;
app.state.rawPriceDates = pricePayload.raw_price_dates || null;
app.state.trades = app.normalizeLedgerTrades(ledger.trades || []);

app.renderHoldings();

/* ---------------------------------------------------------- 校验 */
const failures = [];

function check(label, condition, detail) {
  const mark = condition ? 'OK ' : '!! ';
  console.log(`  ${mark}${label}${detail ? '  ' + detail : ''}`);
  if (!condition) failures.push(label);
}

function statValues(html) {
  const out = {};
  const re = /<span class="k">([^<]+)<\/span>\s*<span class="v[^"]*">([^<]+)<\/span>/g;
  let m;
  while ((m = re.exec(html)) !== null) out[m[1].trim()] = m[2].trim();
  return out;
}

console.log('='.repeat(64));
console.log('页面渲染校验（真实 app.js + 真实账本）');
console.log('='.repeat(64));

const rebStatsHtml = elements['rebalance-stats'].innerHTML;
const gridStatsHtml = elements['grid-stats'].innerHTML;
const rebAssetsHtml = elements['rebalance-assets'].innerHTML;

const reb = statValues(rebStatsHtml);
const grid = statValues(gridStatsHtml);

console.log('\n[再平衡组合 stat 块]');
Object.entries(reb).forEach(([k, v]) => console.log(`    ${k} = ${v}`));

console.log('\n[网格组合 stat 块]');
Object.entries(grid).forEach(([k, v]) => console.log(`    ${k} = ${v}`));

console.log('\n[校验]');

/* 期望值由账本 + 行情现场推导（不写死数字，价格每天会变） */
/* 真实持仓按**实际成交价**（不复权）估值，与页面口径一致 */
const expected = sandbox.__app.LedgerStats.computePoolStats(
  'real', app.state.trades.filter(t => t.strategy === 'rebalance'),
  app.state.rawPrices || prices, app.initialCapitalOf('real'));
const money0 = (v) => '¥' + Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 0 });
const pct0 = (v) => `${v >= 0 ? '+' : ''}${(v * 100).toFixed(2)}%`;

// 初始资金是总金额
check('再平衡组合初始资金 = ¥200,000', reb['初始资金'] === '¥200,000', reb['初始资金']);
check('网格预算 = ¥200,000', grid['网格预算'] === '¥200,000', grid['网格预算']);

// 现金是剩余的钱
check('现金余额 = 剩余的钱', reb['现金余额'] === money0(expected.cash),
  `${reb['现金余额']} (期望 ${money0(expected.cash)})`);

// 持仓市值 = 各资产 数量 × 最新价
check('持仓市值 = 各资产最新市值之和', reb['持仓市值'] === money0(expected.market_value),
  `${reb['持仓市值']} (期望 ${money0(expected.market_value)})`);

// 总资产 = 现金 + 市值
check('总资产 = 现金 + 持仓市值', reb['总资产'] === money0(expected.total_value),
  `${reb['总资产']} (期望 ${money0(expected.total_value)})`);

// 累计收益 = (总资产 − 初始资金) / 初始资金
check('累计收益 = (总资产 − 初始资金)/初始资金',
  reb['累计收益'] === pct0(expected.total_return),
  `${reb['累计收益']} (期望 ${pct0(expected.total_return)})`);

// 成交笔数
check('成交笔数与账本一致', reb['成交笔数'] === String(expected.trade_count),
  `${reb['成交笔数']} (期望 ${expected.trade_count})`);

// 资产明细表
console.log('\n[资产明细表]');
const assetRows = rebAssetsHtml.match(/<tr>[\s\S]*?<\/tr>/g) || [];
assetRows.slice(1).forEach((row) => {
  const cells = (row.match(/<td[^>]*>([\s\S]*?)<\/td>/g) || [])
    .map((c) => c.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim());
  console.log('   ', cells.join(' | '));
});
check('资产明细表有 4 行', assetRows.length - 1 === 4, `${assetRows.length - 1} 行`);

// 权重之和 = 100%
const weights = (rebAssetsHtml.match(/>([\d.]+)%<\/td>/g) || [])
  .map((s) => parseFloat(s.replace(/[>%]/g, '')));
const weightSum = weights.reduce((a, b) => a + b, 0);
// 显示保留 2 位小数，4 行最多累积 0.02% 的显示误差
check('权重之和 = 100%', Math.abs(weightSum - 100) < 0.05, weightSum.toFixed(4) + '%');

console.log('\n' + '='.repeat(64));
if (failures.length) {
  console.log('渲染校验失败：');
  failures.forEach((f) => console.log('  - ' + f));
  process.exit(1);
}
console.log('渲染校验通过：页面显示与账本一致');

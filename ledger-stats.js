/* 账本统计（前端版 · 与 Python src/strategy/ledger/stats.py 口径一致）
 *
 * 真相源只有 data/trades.json；这里现场重演，不落任何派生文件。
 *
 * 口径:
 *   初始资金   = 该池的总金额（真实现金 / 网格虚拟预算）
 *   现金余额   = 初始资金 + 卖出收入 − 买入支出 − 费用
 *   持仓市值   = Σ(各资产 数量 × 最新价)
 *   总资产     = 现金余额 + 持仓市值
 *   累计收益率 = (总资产 − 初始资金) / 初始资金
 *
 * 同一函数同时被页面与 scripts/verify_ledger_stats.js 使用，
 * 保证前端显示与后端算出的数字一致。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.LedgerStats = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  /* 成交金额：账本 JSON 对证券流水只存 数量/价格，金额现场推导 */
  function tradeAmount(trade) {
    if (trade.side === 'deposit' || trade.side === 'withdraw' ||
        trade.side === 'dividend') {
      return Number(trade.amount || 0);
    }
    return Number(trade.shares || 0) * Number(trade.price || 0);
  }

  /* 资产代码：兼容原始账本流水（code）与页面行（asset_code） */
  function codeOf(trade) {
    return trade.code || trade.asset_code || '';
  }

  /* 最新价：优先行情价；无行情则回退该资产最近一笔成交价 */
  function latestPrice(code, ordered, prices) {
    if (prices && prices[code] != null) return Number(prices[code]);
    for (let i = ordered.length - 1; i >= 0; i -= 1) {
      const t = ordered[i];
      if (codeOf(t) === code && Number(t.price) > 0) return Number(t.price);
    }
    return 0;
  }

  /* 计算单个资金池的统计 */
  function computePoolStats(pool, trades, prices, initialCapital) {
    const capital = Number(initialCapital) || 0;
    const ordered = [...trades].sort((a, b) => a.date.localeCompare(b.date));

    let cash = capital;
    let buyCount = 0;
    let sellCount = 0;
    const shares = {};
    const cost = {};

    ordered.forEach((t) => {
      const code = codeOf(t);
      const amount = tradeAmount(t);
      const fee = Number(t.fee || 0);

      if (t.side === 'deposit') { cash += amount; return; }
      if (t.side === 'withdraw') { cash -= amount; return; }
      if (t.side === 'dividend') { cash += amount; return; }

      if (t.side === 'buy') {
        cash -= amount + fee;
        shares[code] = (shares[code] || 0) + Number(t.shares || 0);
        cost[code] = (cost[code] || 0) + amount + fee;
        buyCount += 1;
        return;
      }

      if (t.side === 'sell') {
        cash += amount - fee;
        const before = shares[code] || 0;
        const released = before > 0 ? (cost[code] || 0) * Number(t.shares || 0) / before : 0;
        shares[code] = before - Number(t.shares || 0);
        cost[code] = (cost[code] || 0) - released;
        sellCount += 1;
      }
    });

    /* 持仓市值（最新价） */
    const positions = [];
    let marketValue = 0;
    Object.keys(shares).sort().forEach((code) => {
      const qty = shares[code];
      if (qty <= 0) return;
      const price = latestPrice(code, ordered, prices);
      const value = qty * price;
      marketValue += value;
      const basis = cost[code] || 0;
      positions.push({
        code,
        shares: qty,
        price,
        market_value: value,
        weight: 0,
        cost: basis,
        average_cost: qty > 0 ? basis / qty : 0,
      });
    });

    /* 权重 = 各资产市值 / 最新总市值 */
    positions.forEach((p) => {
      p.weight = marketValue > 0 ? p.market_value / marketValue : 0;
    });

    const totalValue = cash + marketValue;
    const totalReturn = capital > 0 ? (totalValue - capital) / capital : 0;

    return {
      pool,
      initial_capital: capital,
      cash,
      market_value: marketValue,
      total_value: totalValue,
      total_return: totalReturn,
      positions,
      buy_count: buyCount,
      sell_count: sellCount,
      trade_count: ordered.length,
    };
  }

  return { computePoolStats, tradeAmount, latestPrice, codeOf };
});

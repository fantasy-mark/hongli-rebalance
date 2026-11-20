# 交易流水与持仓设计（DDD）

## 1. 设计目标

参考真实账本实践，建立一套**可核对、可重建、可扩展**的持仓与交易模型：

1. **流水是唯一真相源** —— 持仓不是独立维护的数据，而是由流水重演得出
2. **追加式记账** —— 历史流水不可修改，纠错靠新增相反流水
3. **双资金池** —— 真实现金与网格虚拟预算严格分离
4. **出入金与买卖正交** —— 用方向枚举区分，而不是"买入 0 股"

## 2. 分层

```
┌──────────────────────────────────────────────────────────────┐
│ Infrastructure                                               │
│  JsonFileTradeLedgerRepository                               │
│    · load(): 读 trades.json -> TradeLedger                   │
│    · save(): 校验 -> 写 trades.json -> 重演 -> 写 holdings.json│
└──────────────────────────────────────────────────────────────┘
        │
┌──────────────────────────────────────────────────────────────┐
│ Domain                                                       │
│  trading/trade.py   Trade / TradeSide / TradeId              │
│  trading/ledger.py  TradeLedger（聚合根）/ LedgerSnapshot      │
│  portfolio/         Portfolio / Position（持仓与现金）         │
└──────────────────────────────────────────────────────────────┘
```

持仓复用已有的 `Portfolio` 共享内核：账本重演时把每笔流水"喂"给对应池的
`Portfolio`，不变量（卖出超持仓、成本为负）由 `Portfolio` 统一保护。

## 3. 领域模型

### 3.1 `TradeSide`（值对象）

| 方向 | 含义 | 现金影响 | 持仓影响 |
|---|---|---|---|
| `BUY` | 买入 | `-(成交额 + 费用)` | `+shares` |
| `SELL` | 卖出 | `+(成交额 - 费用)` | `-shares` |
| `DEPOSIT` | 入金 | `+amount` | 不变 |
| `WITHDRAW` | 出金 | `-amount` | 不变 |
| `DIVIDEND` | 分红 | `+amount` | 不变 |

`is_security` 区分两类：证券买卖需要 `code/shares/price`，
出入金只需要 `amount`。

### 3.2 `CashFlowType`（值对象）

| 池 | 含义 |
|---|---|
| `REAL` | 真实现金池（实盘买卖用） |
| `GRID` | 网格虚拟预算池（网格记账用，不代表真实现金） |

**隔离规则**：每笔流水归属一个池；卖出所得回到该笔流水所属的池；
任一池卖出不得超过该池持仓。

### 3.3 `Trade`（值对象，不可变）

```
Trade
├── id: TradeId            {日期}-{方向}-{序号}，如 2026-10-19-sell-002
├── on_date: date
├── side: TradeSide
├── pool: CashFlowType
├── asset_code / shares / price     证券字段（仅买卖）
├── amount                          出入金金额
├── fee                             费用
├── note
└── 推导: gross_amount / cash_delta / shares_delta
```

不变量：
- 证券买卖必须有 `asset_code`、`shares > 0`、`price >= 0`
- 纯现金流水 `amount != 0`
- `fee >= 0`
- 未指定 `amount` 时按 `shares * price` 派生（避免两者不一致）

### 3.4 `TradeLedger`（聚合根）

```
TradeLedger
├── trades: List[Trade]        只追加
├── record(trade)              校验流水号唯一后追加
├── replay(as_of) -> LedgerSnapshot
└── validate() -> List[str]    自洽性校验
```

`replay()` 是核心行为：按日期稳定排序后逐笔应用到各池的 `Portfolio`，
产出 `LedgerSnapshot`（各池现金 + 持仓 + 累计统计）。

**重演即校验**：若出现卖出超持仓或现金不足，抛 `LedgerViolation`。

### 3.5 `LedgerSnapshot`（值对象）

| 属性 | 说明 |
|---|---|
| `pools` | 各池的 `Portfolio`（现金 + 持仓） |
| `balances` | 各池余额统计（入金/出金/分红/费用/已实现盈亏） |
| `total_positions()` | 跨池持仓合计 |
| `as_of` / `trade_count` | 快照时点与参与重演的流水数 |

## 4. 关键设计决策

### 4.1 为什么持仓是派生数据

参考数据里 `holdings.json` 的注释写着
「REBUILT from data/trades.json」——这说明实践上持仓本就是流水的投影。
设计上把它固化为规则：

```
trades.json   ← 唯一真相源（持久化）
holdings.json ← 派生视图（每次 save 时由 replay 生成，仅供人看）
```

**验证**：用 13 笔真实流水重演，现金与 4 个持仓数量与参考快照**完全一致**。

### 4.2 为什么出入金要独立于买卖

若把入金建模成"买入 0 股"，会带来两个问题：
- `shares=0` 违反买入不变量
- 盈亏计算会被污染（没有成本基数却产生"成交额"）

因此用方向枚举正交表达，`amount` 与 `shares*price` 二选一。

### 4.3 旧账本兼容

旧数据用 `deposit_rebalance` / `deposit_grid` 这类复合方向，
新模型归一为：

```
deposit_rebalance -> TradeSide.DEPOSIT + CashFlowType.REAL
deposit_grid      -> TradeSide.DEPOSIT + CashFlowType.GRID
```

由 `TradeLedger.from_dict()` 在加载时完成迁移，无需人工改写历史数据。

## 5. 持久化格式

### 5.1 `trades.json`（真相源）

```json
{
  "schema": "trade-ledger-v1",
  "trades": [
    {"id": "2026-10-12-deposit-001", "date": "2026-10-12",
     "side": "deposit", "pool": "real", "fee": 0.0, "note": "", "amount": 175900.6},
    {"id": "2026-10-12-buy-001", "date": "2026-10-12",
     "side": "buy", "pool": "real", "fee": 0.0, "note": "",
     "code": "515180", "shares": 40600.0, "price": 1.446}
  ]
}
```

### 5.2 `holdings.json`（派生视图）

```json
{
  "_schema": "holdings-v3",
  "_comment": "派生视图：由 trades.json 重演生成，请勿手工编辑",
  "pools": {
    "real": {"cash": 328.4, "positions": [
      {"code": "515180", "shares": 32000, "cost_basis": ..., "average_cost": ...}
    ]},
    "grid": {"cash": 15000.0, "positions": []}
  },
  "totals": {"positions": {"515180": 32000}}
}
```

## 6. 使用示例

```python
from src.strategy.ledger.store import JsonFileTradeLedgerRepository
from src.strategy.ledger.trade import Trade, TradeId, TradeSide, CashFlowType
from decimal import Decimal
from datetime import date

repo = JsonFileTradeLedgerRepository("data")
ledger = repo.load()

# 入金
ledger.record(Trade(
    id=TradeId.build(date.today(), TradeSide.DEPOSIT, 1),
    on_date=date.today(), side=TradeSide.DEPOSIT,
    pool=CashFlowType.REAL, amount=Decimal("10000"),
))

# 买入
ledger.record(Trade(
    id=TradeId.build(date.today(), TradeSide.BUY, 1),
    on_date=date.today(), side=TradeSide.BUY,
    pool=CashFlowType.REAL, asset_code="515180",
    shares=Decimal("100"), price=Decimal("1.75"), fee=Decimal("5"),
))

repo.save(ledger)          # 校验 + 写账本 + 重建持仓视图

snap = repo.load().replay()
print(snap.portfolio(CashFlowType.REAL).cash)
print(snap.balances[CashFlowType.REAL].realized_pnl)
```

## 7. 测试

`tests/test_trading_ledger.py` —— 25 个离线用例：

- `Trade` 五方向推导与不变量（买入/卖出/入金/出金/分红）
- 旧方向字符串归一（`deposit_rebalance` / `deposit_grid`）
- 双池隔离（现金、持仓互不影响）
- 追加式（流水号重复被拒）
- 重演校验（卖出超持仓抛 `LedgerViolation`）
- `as_of` 截断重演、跨池持仓合计、序列化往返
- **真实参考账本回归**：13 笔流水重演与持仓快照完全一致

## 8. 与回测的关系

- **回测**用 `Portfolio`（内存对象）逐日推进，不落盘
- **实盘/账本**用 `TradeLedger` 记录真实流水，持仓由重演得出

两者共用 `Portfolio` 作为持仓载体，保证"回测口径"与"实盘口径"一致：
同一套 `buy/sell` 语义、同一套不变量校验。

## 9. 后续可扩展点

| 需求 | 扩展方式 |
|---|---|
| 分红再投资 | 新增 `TradeSide.DIVIDEND` + 一笔 `BUY`，流水天然可追溯 |
| 拆股/合并 | 新增 `TradeSide.SPLIT`，`shares_delta` 按比例调整 |
| 多账户 | `CashFlowType` 泛化为 `Account` 聚合，账本按账户过滤 |
| 成本核算方法 | `Portfolio.sell()` 已支持 `cost_basis_released`，可切换 FIFO/均价 |
| 对账 | 用 `replay(as_of)` 与券商对账单逐日比对 |

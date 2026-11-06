# 价格领域模型 DDD 设计

## 1. 战略设计 - 边界上下文划分

### 1.1 核心域 (Core Domain)

**价格上下文 (Price Context)**
- 核心价值：提供准确、一致的价格数据和收益率计算
- 关键能力：后复权价格获取、收益率计算、技术指标

**持仓上下文 (Position Context)**
- 核心价值：管理用户实际持仓和交易记录
- 依赖价格上下文获取估值

**策略上下文 (Strategy Context)**
- 核心价值：定义和执行交易策略
- 依赖价格上下文进行回测

### 1.2 支撑域 (Supporting Domain)

**数据源上下文 (DataSource Context)**
- 职责：封装外部数据源（东财、腾讯等）
- 为价格上下文提供原始数据

**图表上下文 (Chart Context)**
- 职责：可视化渲染
- 消费价格上下文的数据

---

## 2. 战术设计 - 领域模型

### 2.1 核心实体 (Entities)

#### `AdjustedPriceSeries` (聚合根)
```
代表一个资产的完整后复权价格序列

属性:
- asset_code: str (资产代码)
- asset_type: AssetType (ETF/STOCK/INDEX)
- prices: List[PricePoint] (有序价格点集合)
- source: DataSource (数据来源)
- calculated_at: datetime (计算时间)

行为:
- get_price(date): PricePoint | None
- get_latest_price(): PricePoint
- calculate_return(start_date, end_date): ReturnMetrics
- to_dataframe(): pd.DataFrame
```

#### `PricePoint` (值对象)
```
单个交易日的价格数据

属性:
- date: date
- open: Decimal
- high: Decimal
- low: Decimal
- close: Decimal  # 后复权收盘价
- volume: int
- amount: Decimal

不变量:
- high >= open, high >= close
- low <= open, low <= close
- high >= low
- close > 0
```

### 2.2 值对象 (Value Objects)

#### `ReturnMetrics` (收益率指标)
```
计算得出的收益率统计

属性:
- total_return: Decimal  # 累计收益率
- annualized_return: Decimal  # 年化收益率 (CAGR)
- volatility: Decimal  # 波动率 (年化标准差)
- sharpe_ratio: Decimal  # 夏普比率
- max_drawdown: Decimal  # 最大回撤
- start_date: date
- end_date: date
- trading_days: int
```

#### `DateRange` (日期范围)
```
属性:
- start: date
- end: date

行为:
- contains(date): bool
- duration_days(): int
- intersects(other): bool
```

#### `AssetCode` (资产代码)
```
属性:
- code: str (6位数字)
- exchange: Exchange (SH/SZ)

行为:
- is_etf(): bool
- to_sina_symbol(): str
- validate(): bool
```

### 2.3 枚举类型

```python
class AssetType(Enum):
    ETF = "etf"
    STOCK = "stock"
    INDEX = "index"
    BOND = "bond"

class Exchange(Enum):
    SH = "sh"  # 上交所
    SZ = "sz"  # 深交所

class DataSource(Enum):
    EASTMONEY_HFQ = "eastmoney_hfq"  # 东财后复权
    EASTMONEY_UNADJ = "eastmoney_unadj"  # 东财不复权
    TENCENT_QFQ = "tencent_qfq"  # 腾讯前复权
    CALCULATED = "calculated"  # 自算总收益
```

---

## 3. 领域服务 (Domain Services)

### 3.1 `PriceCalculator` (价格计算器)

**职责**：执行复杂的收益率和技术指标计算

```python
class PriceCalculator:
    """纯领域逻辑，无副作用"""
    
    def calculate_total_return(series: AdjustedPriceSeries, 
                                start: date, 
                                end: date) -> Decimal:
        """计算累计收益率"""
        
    def calculate_cagr(series: AdjustedPriceSeries,
                       start: date,
                       end: date) -> Decimal:
        """计算年化收益率"""
        
    def calculate_sharpe_ratio(series: AdjustedPriceSeries,
                                risk_free_rate: Decimal = Decimal("0.03")) -> Decimal:
        """计算夏普比率"""
        
    def calculate_max_drawdown(series: AdjustedPriceSeries) -> Tuple[Decimal, date, date]:
        """计算最大回撤及发生时间"""
        
    def calculate_moving_average(series: AdjustedPriceSeries,
                                  window: int) -> List[Decimal]:
        """计算移动平均线"""
```

### 3.2 `PriceSeriesFactory` (价格序列工厂)

**职责**：创建和调整价格序列

```python
class PriceSeriesFactory:
    """负责从数据源获取并构建价格序列"""
    
    def create_from_source(asset_code: AssetCode,
                           adjust_type: AdjustType = AdjustType.HFQ) -> AdjustedPriceSeries:
        """从数据源创建价格序列"""
        
    def merge_series(primary: AdjustedPriceSeries,
                     secondary: AdjustedPriceSeries) -> AdjustedPriceSeries:
        """合并多个数据源的价格序列"""
        
    def resample(series: AdjustedPriceSeries,
                 frequency: Frequency = Frequency.WEEKLY) -> AdjustedPriceSeries:
        """重采样到不同频率"""
```

---

## 4. 仓储接口 (Repository Interface)

### 4.1 `PriceSeriesRepository` (价格序列仓储)

```python
class PriceSeriesRepository(ABC):
    """价格序列的持久化接口"""
    
    @abstractmethod
    def save(self, series: AdjustedPriceSeries) -> None:
        """保存价格序列"""
        
    @abstractmethod
    def find_by_code(self, asset_code: AssetCode) -> Optional[AdjustedPriceSeries]:
        """根据资产代码查找"""
        
    @abstractmethod
    def find_by_date_range(self, 
                           asset_code: AssetCode,
                           date_range: DateRange) -> AdjustedPriceSeries:
        """根据日期范围查找"""
        
    @abstractmethod
    def exists(self, asset_code: AssetCode) -> bool:
        """检查是否存在"""
```

**实现**：
- `InMemoryPriceRepository`: 内存缓存（进程内）
- `JsonFilePriceRepository`: JSON文件存储
- `DatabasePriceRepository`: 数据库存储（未来扩展）

---

## 5. 应用服务 (Application Services)

### 5.1 `PriceQueryService` (价格查询服务)

```python
class PriceQueryService:
    """编排领域对象完成用例"""
    
    def __init__(self, 
                 repository: PriceSeriesRepository,
                 factory: PriceSeriesFactory,
                 calculator: PriceCalculator):
        self.repository = repository
        self.factory = factory
        self.calculator = calculator
    
    def get_adjusted_prices(self, 
                            code: str,
                            start_date: Optional[str] = None,
                            end_date: Optional[str] = None) -> pd.DataFrame:
        """获取后复权价格数据（用于图表）"""
        
    def calculate_returns(self,
                          code: str,
                          start_date: Optional[str] = None,
                          end_date: Optional[str] = None) -> ReturnMetrics:
        """计算收益率指标"""
        
    def backtest_strategy(self,
                          code: str,
                          strategy: TradingStrategy,
                          start_date: str,
                          end_date: str) -> BacktestResult:
        """回测交易策略"""
```

---

## 6. 领域事件 (Domain Events)

```python
@dataclass
class PriceSeriesUpdated:
    """价格序列更新事件"""
    asset_code: AssetCode
    update_time: datetime
    new_data_points: int
    source: DataSource

@dataclass
class CacheMissed:
    """缓存未命中事件"""
    asset_code: AssetCode
    requested_range: DateRange
```

---

## 7. 业务规则 (Business Rules)

### 7.1 价格数据完整性规则

1. **连续性检查**：价格序列不应有超过5个交易日的空缺
2. **单调性检查**：后复权价格的长期趋势应与不复权一致
3. **异常值检测**：单日涨跌幅不应超过50%（除新股上市首日）

### 7.2 收益率计算规则

1. **后复权优先**：所有收益率计算必须使用后复权价格
2. **分红处理**：ETF分红已在后复权中体现，无需额外处理
3. **交易日对齐**：多资产对比时必须对齐交易日

### 7.3 缓存策略

1. **TTL**: 日线数据缓存24小时
2. **增量更新**: 只获取最新数据，追加到现有序列
3. **预加载**: 常用资产（篮子里的4个ETF）启动时预加载

---

## 8. 目录结构建议

```
src/
├── domain/
│   └── price/
│       ├── entities.py          # AdjustedPriceSeries, PricePoint
│       ├── value_objects.py     # ReturnMetrics, DateRange, AssetCode
│       ├── services.py          # PriceCalculator, PriceSeriesFactory
│       ├── events.py            # 领域事件
│       └── exceptions.py        # 领域异常
│
├── application/
│   └── price/
│       ├── query_service.py     # PriceQueryService
│       ├── dto.py               # 数据传输对象
│       └── commands.py          # 命令对象
│
├── infrastructure/
│   └── price/
│       ├── repositories.py      # 仓储实现
│       ├── adjusted_price_provider.py  # 数据源适配器
│       └── cache.py             # 缓存实现
│
└── interfaces/
    └── api/
        └── price_routes.py      # API路由（如果需要）
```

---

## 9. 使用示例

### 场景1: 绘制价格曲线

```python
from src.application.price.query_service import PriceQueryService

service = PriceQueryService(...)

# 获取数据
df = service.get_adjusted_prices("515180", start_date="2023-01-01")

# 绘图
import matplotlib.pyplot as plt
plt.plot(df.index, df['close'])
plt.title("红利ETF后复权价格")
plt.show()
```

### 场景2: 计算收益率

```python
metrics = service.calculate_returns("515180", 
                                     start_date="2020-01-01",
                                     end_date="2026-11-06")

print(f"累计收益: {metrics.total_return:.2%}")
print(f"年化收益: {metrics.annualized_return:.2%}")
print(f"夏普比率: {metrics.sharpe_ratio:.2f}")
print(f"最大回撤: {metrics.max_drawdown:.2%}")
```

### 场景3: 多资产对比

```python
codes = ["515180", "159937", "511380", "513300"]
dfs = [service.get_adjusted_prices(code) for code in codes]

# 归一化到起点为1
for df in dfs:
    df['normalized'] = df['close'] / df['close'].iloc[0]

# 绘制对比图
for df, code in zip(dfs, codes):
    plt.plot(df.index, df['normalized'], label=code)
plt.legend()
plt.show()
```

---

## 10. 与现有系统的集成

### 10.1 保持向后兼容

现有的 `adjusted_price_provider.py` 作为基础设施层的数据源适配器，新的领域模型在其之上构建。

### 10.2 渐进式迁移

1. **第一阶段**: 实现核心实体和值对象
2. **第二阶段**: 实现仓储和缓存
3. **第三阶段**: 实现应用服务和业务规则
4. **第四阶段**: 替换现有调用点

---

## 11. 测试策略

### 11.1 单元测试

- `PricePoint` 的不变量验证
- `PriceCalculator` 的计算准确性
- `DateRange` 的边界条件

### 11.2 集成测试

- 从真实数据源获取数据
- 仓储的保存和读取
- 缓存的命中和失效

### 11.3 回归测试

- 与现有 `price_data_sources.md` 中的已知数据对比
- 确保 515180 的最新价格为 1.762

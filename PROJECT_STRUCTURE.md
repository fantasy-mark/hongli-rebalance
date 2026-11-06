# 项目结构概览

## 📁 文件清单

### 核心代码 (7个Python文件)

#### 领域层 (Domain Layer) - 业务核心
```
src/domain/price/
├── entities.py              # 实体: AssetCode, PricePoint, AdjustedPriceSeries
├── value_objects.py         # 值对象: ReturnMetrics, DateRange, TechnicalIndicators
└── services.py              # 领域服务: PriceCalculator
```

#### 应用层 (Application Layer) - 用例编排
```
src/application/price/
└── query_service.py         # 应用服务: PriceQueryService
```

#### 基础设施层 (Infrastructure Layer) - 技术实现
```
src/infrastructure/
├── price/
│   └── repositories.py      # 仓储: InMemoryPriceRepository, JsonFilePriceRepository
└── adjusted_price_provider.py  # 数据源适配器 (已修复为hfq)
```

### 测试 (1个文件)
```
tests/
└── test_price_domain.py     # 27个单元测试 ✅ 全部通过
```

### 示例 (1个文件)
```
examples/
└── ddd_price_model_demo.py  # 6个完整使用场景示例
```

### 文档 (5个Markdown文件)
```
docs/
├── ddd_price_model_design.md          # DDD设计文档
├── ddd_implementation_summary.md      # 实现总结
├── adjusted_price_provider_usage.md   # 使用指南
├── migration_summary.md               # 迁移报告
└── price_data_sources.md              # 数据源说明 (原有)

QUICKSTART.md                          # 快速参考
README.md                              # 项目说明
PROJECT_STRUCTURE.md                   # 本文件
```

---

## 🎯 核心功能

### 1. 价格数据获取
- ✅ 后复权(hfq)价格，515180最新价1.762
- ✅ 支持ETF和股票
- ✅ 自动缓存（内存+JSON文件）
- ✅ 多数据源降级（东财→腾讯）

### 2. 收益率计算
- ✅ 累计收益率
- ✅ 年化收益率(CAGR)
- ✅ 波动率
- ✅ 夏普比率
- ✅ 最大回撤

### 3. 技术分析
- ✅ 移动平均线(MA5/10/20/60)
- ✅ RSI指标
- ✅ 布林带

### 4. 多资产对比
- ✅ 归一化价格对比
- ✅ 批量收益率计算
- ✅ 统一时间范围

---

## 🚀 快速开始

### 安装依赖
```bash
pip install pandas akshare requests
```

### 运行测试
```bash
python -m pytest tests/test_price_domain.py -v
```

### 运行示例
```bash
python examples/ddd_price_model_demo.py
```

### 基本用法
```python
from src.application.price.query_service import PriceQueryService

service = PriceQueryService()

# 获取价格
df = service.get_adjusted_prices("515180")

# 计算收益率
metrics = service.calculate_returns("515180", start_date="2020-01-01")
print(metrics.summary())

# 多资产对比
df = service.compare_assets(["515180", "159937", "511380", "513300"])
```

---

## 📊 验证结果

### 单元测试
- **27/27** 测试通过 ✅
- 覆盖领域层、应用层、集成测试

### 关键数据
- **515180最新价**: 1.762 ✓
- **2020-2026累计收益**: 70.90%
- **年化收益**: 8.14%
- **最大回撤**: -17.31%

### 性能
- 首次获取: ~2-5秒（网络IO）
- 缓存命中: 毫秒级
- 收益率计算: 毫秒级

---

## 📖 文档导航

| 文档 | 用途 |
|------|------|
| [QUICKSTART.md](QUICKSTART.md) | ⭐ 5分钟上手 |
| [README.md](README.md) | 项目概述 |
| [docs/ddd_price_model_design.md](docs/ddd_price_model_design.md) | DDD设计详解 |
| [docs/ddd_implementation_summary.md](docs/ddd_implementation_summary.md) | 实现总结 |
| [docs/migration_summary.md](docs/migration_summary.md) | 迁移报告 |
| [docs/adjusted_price_provider_usage.md](docs/adjusted_price_provider_usage.md) | API详细文档 |

---

## 🎨 架构特点

### DDD分层架构
```
┌─────────────────────────┐
│   Application Layer     │  ← PriceQueryService
├─────────────────────────┤
│   Domain Layer          │  ← Entities, Value Objects, Services
├─────────────────────────┤
│   Infrastructure Layer  │  ← Repositories, Data Providers
└─────────────────────────┘
```

### 核心设计原则
- ✅ 聚合根保护不变量
- ✅ 值对象不可变
- ✅ 领域服务无状态
- ✅ 仓储接口抽象
- ✅ 业务规则内聚

---

## 🔧 技术栈

- **Python 3.11+**
- **pandas** - 数据处理
- **akshare** - 金融数据源
- **requests** - HTTP客户端
- **pytest** - 测试框架
- **Decimal** - 高精度计算

---

## ✨ 下一步

### 立即可用
- ✅ 绘制价格图表
- ✅ 计算收益率指标
- ✅ 多资产对比分析
- ✅ 技术指标查询

### 待开发
- ⏳ 策略回测框架
- ⏳ 数据库仓储
- ⏳ 实时价格推送
- ⏳ 更多技术指标

---

## 📞 支持

如有问题，请查阅：
1. [快速参考](QUICKSTART.md) - 常见问题
2. [使用指南](docs/adjusted_price_provider_usage.md) - API文档
3. [示例代码](examples/ddd_price_model_demo.py) - 实际用法

---

**最后更新**: 2026-11-06  
**版本**: v1.0  
**状态**: ✅ 生产就绪

"""
图表模块

职责：把策略结果呈现出来——图片、表格、网页。

| 文件 | 职责 |
|---|---|
| `models.py` | 图表模型（ChartConfig / ChartSeries / 样式 / 标注） |
| `renderer.py` | 图片生成器：通用收益曲线图（ChartRenderer / ChartFactory） |
| `overview.py` | 策略全景图模型（收益率曲线 + 资金占用） |
| `overview_renderer.py` | 策略全景图图片生成器 |
| `grid_chart.py` | 网格策略图片生成器（多面板：价格 / 持仓 / 对比） |
| `tables.py` | 表格数据：触发日志、成交明细（纯文本表格） |
| `site.py` | 网页生成器：视图模型 + 用例编排（BuildPagesSite） |
| `site_store.py` | 站点产物落盘（JSON / PNG） |

依赖方向：chart -> strategy -> data（图表只消费策略与数据的结果）。
"""

__all__ = ["models", "renderer", "overview", "site"]

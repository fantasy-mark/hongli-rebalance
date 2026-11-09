"""
图表领域实体

核心聚合根: ChartConfig
值对象: ChartSeries, Annotation, ChartStyle
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import List, Dict, Optional, Any
import uuid


class SeriesType(Enum):
    """曲线类型"""
    LINE = "line"  # 实线
    DASHED = "dashed"  # 虚线
    DOTTED = "dotted"  # 点线


class SeriesRole(Enum):
    """曲线角色"""
    PORTFOLIO = "portfolio"  # 组合
    ASSET = "asset"  # 单个资产
    BENCHMARK = "benchmark"  # 基准


class YAxisScale(Enum):
    """Y轴刻度类型"""
    LINEAR = "linear"  # 线性刻度（默认）
    LOGARITHMIC = "log"  # 对数刻度


@dataclass(frozen=True)
class ChartStyle:
    """
    图表样式（值对象）
    
    不变量:
    - 所有颜色必须是有效的matplotlib颜色字符串
    - 线宽必须>0
    """
    color: str = "#1f77b4"
    line_width: float = 2.0
    line_style: str = "-"  # '-', '--', '-.', ':'
    marker: Optional[str] = None
    alpha: float = 1.0
    
    def __post_init__(self):
        if self.line_width <= 0:
            raise ValueError(f"line_width必须>0: {self.line_width}")
        if not (0 < self.alpha <= 1):
            raise ValueError(f"alpha必须在0-1之间: {self.alpha}")
    
    @classmethod
    def for_portfolio(cls) -> 'ChartStyle':
        """组合曲线默认样式"""
        return cls(
            color="#1f77b4",  # 蓝色
            line_width=2.5,
            line_style="-",
        )
    
    @classmethod
    def for_asset(cls, index: int = 0) -> 'ChartStyle':
        """单资产曲线默认样式（虚线）"""
        colors = ["#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]
        return cls(
            color=colors[index % len(colors)],
            line_width=1.5,
            line_style="--",  # 虚线
            alpha=0.7,
        )
    
    @classmethod
    def for_benchmark(cls) -> 'ChartStyle':
        """基准曲线默认样式"""
        return cls(
            color="#7f7f7f",  # 灰色
            line_width=1.0,
            line_style="-.",
            alpha=0.5,
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'color': self.color,
            'line_width': self.line_width,
            'line_style': self.line_style,
            'marker': self.marker,
            'alpha': self.alpha,
        }


@dataclass(frozen=True)
class Annotation:
    """
    图表标注（值对象）
    
    用于在曲线上添加文本标注
    """
    text: str
    x_position: float  # 相对位置 0-1
    y_position: float  # 相对位置 0-1
    fontsize: int = 10
    fontweight: str = "normal"
    bbox_style: Optional[str] = "round,pad=0.3"
    bbox_facecolor: str = "wheat"
    bbox_alpha: float = 0.5
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'text': self.text,
            'x_position': self.x_position,
            'y_position': self.y_position,
            'fontsize': self.fontsize,
            'fontweight': self.fontweight,
            'bbox_style': self.bbox_style,
            'bbox_facecolor': self.bbox_facecolor,
            'bbox_alpha': self.bbox_alpha,
        }


@dataclass(frozen=True)
class ChartSeries:
    """
    图表数据序列（值对象）
    
    代表一条要绘制的曲线
    """
    name: str
    role: SeriesRole
    dates: List[date]
    values: List[Decimal]  # 归一化值（起点=1）
    style: ChartStyle
    annotation: Optional[Annotation] = None
    marker_dates: Optional[List[date]] = None  # 标记点日期列表（如再平衡触发日）
    marker_style: Optional[Dict[str, Any]] = None  # 标记点样式 {color, size, marker}
    series_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    
    def __post_init__(self):
        if len(self.dates) != len(self.values):
            raise ValueError(f"dates和values长度必须相同: {len(self.dates)} vs {len(self.values)}")
        
        if not self.dates:
            raise ValueError("dates不能为空")
        
        if not self.name:
            raise ValueError("name不能为空")
    
    @property
    def current_return(self) -> Decimal:
        """当前累计收益率"""
        if not self.values:
            return Decimal("0")
        return self.values[-1] - Decimal("1")
    
    @property
    def start_date(self) -> date:
        """起始日期"""
        return self.dates[0]
    
    @property
    def end_date(self) -> date:
        """结束日期"""
        return self.dates[-1]
    
    def normalize_values(self, base_value: Decimal = Decimal("1")) -> List[Decimal]:
        """
        归一化值序列
        
        Args:
            base_value: 基准值，默认1
            
        Returns:
            归一化后的值列表
        """
        if not self.values or self.values[0] == 0:
            return self.values
        
        factor = base_value / self.values[0]
        return [v * factor for v in self.values]
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'series_id': self.series_id,
            'name': self.name,
            'role': self.role.value,
            'dates': [d.isoformat() for d in self.dates],
            'values': [float(v) for v in self.values],
            'style': self.style.to_dict(),
            'annotation': self.annotation.to_dict() if self.annotation else None,
            'current_return': float(self.current_return),
        }


@dataclass
class ChartConfig:
    """
    图表配置（聚合根）
    
    职责:
    - 管理所有要绘制的曲线
    - 配置图表标题、标签等
    - 验证配置有效性
    """
    title: str
    series_list: List[ChartSeries] = field(default_factory=list)
    xlabel: str = "日期"
    ylabel: str = "归一化市值"
    figsize: tuple = (14, 8)
    dpi: int = 150
    show_grid: bool = True
    show_legend: bool = True
    legend_location: str = "upper left"
    y_axis_scale: YAxisScale = YAxisScale.LINEAR  # Y轴刻度类型
    config_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=datetime.now)
    
    def add_series(self, series: ChartSeries) -> None:
        """添加曲线"""
        # 检查名称是否重复
        if any(s.name == series.name for s in self.series_list):
            raise ValueError(f"曲线名称已存在: {series.name}")
        
        self.series_list.append(series)
    
    def remove_series(self, series_name: str) -> bool:
        """移除曲线"""
        initial_count = len(self.series_list)
        self.series_list = [s for s in self.series_list if s.name != series_name]
        return len(self.series_list) < initial_count
    
    def get_series_by_role(self, role: SeriesRole) -> List[ChartSeries]:
        """根据角色获取曲线"""
        return [s for s in self.series_list if s.role == role]
    
    def validate(self) -> List[str]:
        """
        验证配置有效性
        
        Returns:
            错误消息列表
        """
        errors = []
        
        if not self.title:
            errors.append("图表标题不能为空")
        
        if not self.series_list:
            errors.append("至少需要一条曲线")
        
        # 检查所有曲线的日期范围是否一致（排除BENCHMARK角色，如纯标注系列）
        if len(self.series_list) > 1:
            data_series = [s for s in self.series_list if s.role != SeriesRole.BENCHMARK]
            if len(data_series) > 1:
                first_dates = set(data_series[0].dates)
                for series in data_series[1:]:
                    series_dates = set(series.dates)
                    if first_dates != series_dates:
                        errors.append(f"曲线'{series.name}'的日期范围与其他曲线不一致")
                        break
        
        # 检查是否有组合曲线（PORTFOLIO角色或名为"组合"的ASSET系列）
        portfolio_series = self.get_series_by_role(SeriesRole.PORTFOLIO)
        if not portfolio_series:
            # 也接受名为"组合"的ASSET系列
            combo_series = [s for s in self.series_list if s.name == "组合"]
            if not combo_series:
                errors.append("缺少组合曲线")
        
        return errors
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'config_id': self.config_id,
            'title': self.title,
            'series_list': [s.to_dict() for s in self.series_list],
            'xlabel': self.xlabel,
            'ylabel': self.ylabel,
            'figsize': self.figsize,
            'dpi': self.dpi,
            'show_grid': self.show_grid,
            'show_legend': self.show_legend,
            'legend_location': self.legend_location,
            'created_at': self.created_at.isoformat(),
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ChartConfig':
        """从字典创建"""
        config = cls(
            title=data['title'],
            xlabel=data.get('xlabel', '日期'),
            ylabel=data.get('ylabel', '归一化市值'),
            figsize=tuple(data.get('figsize', (14, 8))),
            dpi=data.get('dpi', 150),
            show_grid=data.get('show_grid', True),
            show_legend=data.get('show_legend', True),
            legend_location=data.get('legend_location', 'upper left'),
            config_id=data.get('config_id', str(uuid.uuid4())),
            created_at=datetime.fromisoformat(data['created_at']) if data.get('created_at') else datetime.now(),
        )
        
        for s_data in data.get('series_list', []):
            series = ChartSeries(
                name=s_data['name'],
                role=SeriesRole(s_data['role']),
                dates=[date.fromisoformat(d) for d in s_data['dates']],
                values=[Decimal(str(v)) for v in s_data['values']],
                style=ChartStyle(**s_data['style']),
                annotation=Annotation(**s_data['annotation']) if s_data.get('annotation') else None,
                series_id=s_data.get('series_id', str(uuid.uuid4())),
            )
            config.add_series(series)
        
        return config
    
    def __repr__(self) -> str:
        return (
            f"ChartConfig(id={self.config_id}, title={self.title}, "
            f"series={len(self.series_list)})"
        )

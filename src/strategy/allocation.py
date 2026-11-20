"""
总资产配置（策略模块）

核心聚合根: AllocationPlan（总资产配置）
值对象:     AssetAllocation（单个资产的配置）

定位:
- 这是再平衡策略与网格策略**共用的共享内核（Shared Kernel）**
- 再平衡策略使用 target_weight 决定目标配置
- 网格策略只使用资产清单（监控同一批资产），不关心权重
- 由 AllocationPlan 保证两个策略监控/配置的资产集合完全一致
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class AssetAllocation:
    """
    单个资产的配置（值对象）

    不变量:
    - code 必须非空
    - target_weight 必须 > 0 且 <= 1
    """
    code: str
    name: str
    target_weight: Decimal

    def __post_init__(self):
        if not self.code:
            raise ValueError("资产代码不能为空")
        if not self.name:
            raise ValueError("资产名称不能为空")
        if not isinstance(self.target_weight, Decimal):
            object.__setattr__(self, 'target_weight', Decimal(str(self.target_weight)))
        if self.target_weight <= 0 or self.target_weight > 1:
            raise ValueError(f"目标权重必须在0-1之间: {self.target_weight}")

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'code': self.code,
            'name': self.name,
            'target_weight': float(self.target_weight),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AssetAllocation':
        """从字典创建"""
        return cls(
            code=data['code'],
            name=data['name'],
            target_weight=Decimal(str(data['target_weight'])),
        )


@dataclass
class AllocationPlan:
    """
    总资产配置（聚合根）

    职责:
    - 定义组合包含哪些资产、各自的目标权重
    - 作为再平衡策略与网格策略共享的唯一资产来源
    - 保证两个策略监控的资产集合一致

    不变量:
    - 至少包含一个资产
    - 目标权重之和为 1（容差 1%）
    """
    allocations: List[AssetAllocation] = field(default_factory=list)
    name: str = "总资产配置"

    def __post_init__(self):
        if not self.allocations:
            raise ValueError("总资产配置不能为空")

        total = sum(a.target_weight for a in self.allocations)
        if abs(total - Decimal("1")) > Decimal("0.01"):
            raise ValueError(f"目标权重总和必须为1，当前为{total}")

        codes = [a.code for a in self.allocations]
        if len(set(codes)) != len(codes):
            raise ValueError(f"资产代码重复: {codes}")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def asset_codes(self) -> List[str]:
        """资产代码列表（网格监控清单）"""
        return [a.code for a in self.allocations]

    @property
    def asset_names(self) -> Dict[str, str]:
        """资产代码 -> 名称"""
        return {a.code: a.name for a in self.allocations}

    @property
    def target_weights(self) -> Dict[str, Decimal]:
        """资产代码 -> 目标权重"""
        return {a.code: a.target_weight for a in self.allocations}

    def allocation_for(self, code: str) -> Optional[AssetAllocation]:
        """获取单个资产配置"""
        for a in self.allocations:
            if a.code == code:
                return a
        return None

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------

    @classmethod
    def equal_weight(cls,
                     assets: List[tuple],
                     name: str = "等权配置") -> 'AllocationPlan':
        """
        创建等权配置

        Args:
            assets: [(code, name), ...] 列表
            name: 配置名称
        """
        if not assets:
            raise ValueError("资产列表不能为空")

        weight = Decimal("1") / Decimal(str(len(assets)))
        allocations = [
            AssetAllocation(code=code, name=asset_name, target_weight=weight)
            for code, asset_name in assets
        ]
        return cls(allocations=allocations, name=name)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AllocationPlan':
        """从字典创建"""
        return cls(
            allocations=[AssetAllocation.from_dict(a) for a in data['allocations']],
            name=data.get('name', '总资产配置'),
        )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'name': self.name,
            'allocations': [a.to_dict() for a in self.allocations],
        }

    def validate(self) -> List[str]:
        """验证配置有效性"""
        errors = []
        if not self.allocations:
            errors.append("总资产配置为空")

        total = sum(a.target_weight for a in self.allocations)
        if abs(total - Decimal("1")) > Decimal("0.01"):
            errors.append(f"目标权重总和必须为1，当前为{total}")

        return errors

    def __repr__(self) -> str:
        return (
            f"AllocationPlan(name={self.name}, assets={len(self.allocations)}, "
            f"codes={','.join(self.asset_codes)})"
        )

"""
网格策略状态仓储实现

提供网格策略状态的持久化能力（内存 / JSON 文件）
用于实盘场景保存网格档位进度，避免重启后网格状态丢失
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional
import json
import logging
from pathlib import Path

from src.domain.grid.entities import GridStrategy

logger = logging.getLogger(__name__)


class GridStateRepository(ABC):
    """网格策略仓储接口"""

    @abstractmethod
    def save(self, strategy: GridStrategy) -> None:
        """保存网格策略"""

    @abstractmethod
    def find_by_id(self, strategy_id: str) -> Optional[GridStrategy]:
        """根据ID查找"""

    @abstractmethod
    def find_all(self) -> List[GridStrategy]:
        """查找所有"""

    @abstractmethod
    def delete(self, strategy_id: str) -> None:
        """删除"""

    @abstractmethod
    def clear(self) -> None:
        """清空"""


class InMemoryGridStateRepository(GridStateRepository):
    """
    内存网格仓储

    特点:
    - 快速访问
    - 进程内缓存
    - 重启后丢失
    """

    def __init__(self):
        self._cache: Dict[str, GridStrategy] = {}

    def save(self, strategy: GridStrategy) -> None:
        """保存到内存"""
        self._cache[strategy.strategy_id] = strategy
        logger.debug(f"已保存网格策略 {strategy.name}")

    def find_by_id(self, strategy_id: str) -> Optional[GridStrategy]:
        """从内存查找"""
        return self._cache.get(strategy_id)

    def find_all(self) -> List[GridStrategy]:
        """返回全部"""
        return list(self._cache.values())

    def delete(self, strategy_id: str) -> None:
        """删除"""
        if strategy_id in self._cache:
            del self._cache[strategy_id]
            logger.debug(f"已删除网格策略 {strategy_id}")

    def clear(self) -> None:
        """清空"""
        count = len(self._cache)
        self._cache.clear()
        logger.info(f"已清空网格策略缓存 ({count}条)")


class JsonFileGridStateRepository(GridStateRepository):
    """
    JSON文件网格仓储

    特点:
    - 持久化存储
    - 可跨进程共享
    - 适合少量网格策略
    """

    def __init__(self, storage_dir: str = "data/grid_states"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._memory_cache: InMemoryGridStateRepository = InMemoryGridStateRepository()

    def _get_file_path(self, strategy_id: str) -> Path:
        """获取文件路径"""
        return self.storage_dir / f"{strategy_id}.json"

    def save(self, strategy: GridStrategy) -> None:
        """保存到JSON文件"""
        file_path = self._get_file_path(strategy.strategy_id)

        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(strategy.to_dict(), f, ensure_ascii=False, indent=2)

        self._memory_cache.save(strategy)
        logger.debug(f"已保存网格策略到 {file_path}")

    def find_by_id(self, strategy_id: str) -> Optional[GridStrategy]:
        """从文件或内存加载"""
        cached = self._memory_cache.find_by_id(strategy_id)
        if cached:
            return cached

        file_path = self._get_file_path(strategy_id)
        if not file_path.exists():
            return None

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            strategy = GridStrategy.from_dict(data)
            self._memory_cache.save(strategy)
            return strategy

        except Exception as e:
            logger.error(f"加载网格策略 {strategy_id} 失败: {e}")
            return None

    def find_all(self) -> List[GridStrategy]:
        """加载全部策略"""
        strategies = list(self._memory_cache.find_all())

        for file_path in self.storage_dir.glob("*.json"):
            strategy = self._memory_cache.find_by_id(file_path.stem)
            if strategy is None:
                strategy = self.find_by_id(file_path.stem)
            if strategy is not None:
                strategies.append(strategy)

        # 去重
        unique = {s.strategy_id: s for s in strategies}
        return list(unique.values())

    def delete(self, strategy_id: str) -> None:
        """删除文件和缓存"""
        file_path = self._get_file_path(strategy_id)
        if file_path.exists():
            file_path.unlink()
            logger.debug(f"已删除 {file_path}")

        self._memory_cache.delete(strategy_id)

    def clear(self) -> None:
        """清空所有文件"""
        self._memory_cache.clear()

        for file_path in self.storage_dir.glob("*.json"):
            file_path.unlink()

        logger.info(f"已清空 {self.storage_dir} 下的所有网格策略文件")

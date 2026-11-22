"""
交易账本落盘（策略模块）

设计要点:
- **账本（trades.json）是唯一持久化的真相源**
- 持仓**不落盘**：由账本重演得出（`ledger.replay()`），需要时现算
- 追加式：保存时整体写回（账本通常很小），不做逐条更新
"""

from abc import ABC, abstractmethod
from pathlib import Path
import json
import logging

from src.strategy.ledger.book import LedgerSnapshot, TradeLedger

logger = logging.getLogger(__name__)


class TradeLedgerRepository(ABC):
    """交易账本仓储接口"""

    @abstractmethod
    def load(self) -> TradeLedger:
        """加载账本（不存在时返回空账本）"""

    @abstractmethod
    def save(self, ledger: TradeLedger) -> None:
        """保存账本（覆盖写，保持流水顺序）"""


class JsonFileTradeLedgerRepository(TradeLedgerRepository):
    """
    JSON 文件账本仓储

    文件:
    - `trades.json`   账本（唯一真相源）

    持仓快照**不落盘**：用 `replay()` 现算，避免两份真相来源。
    """

    def __init__(self, storage_dir: str = "data"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.trades_path = self.storage_dir / "trades.json"

    # ------------------------------------------------------------------

    def load(self) -> TradeLedger:
        if not self.trades_path.exists():
            return TradeLedger()

        with open(self.trades_path, encoding="utf-8") as handle:
            data = json.load(handle)

        ledger = TradeLedger.from_dict(data)
        logger.info(f"已加载账本：{len(ledger)} 笔流水，"
                    f"截至 {ledger.last_date()}")
        return ledger

    def save(self, ledger: TradeLedger) -> None:
        errors = ledger.validate()
        if errors:
            raise ValueError(f"账本不自洽，拒绝保存：{errors}")

        # 写入前补全缺失的交易类型（建仓/加仓/减仓/止盈）
        normalized = ledger.with_resolved_actions()

        with open(self.trades_path, "w", encoding="utf-8") as handle:
            json.dump(normalized.to_dict(), handle, ensure_ascii=False, indent=2)

        logger.info(f"已保存账本：{len(normalized)} 笔流水")

    # ------------------------------------------------------------------
    # 派生视图（不落盘，现算）

    def replay(self, as_of=None) -> LedgerSnapshot:
        """重演账本得到持仓快照（现算，不落盘）"""
        return self.load().replay(as_of)

    def holdings(self) -> LedgerSnapshot:
        """当前持仓快照（便捷方法）"""
        return self.replay()

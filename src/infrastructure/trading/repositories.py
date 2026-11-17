"""
交易账本仓储

设计要点:
- **账本（trades）是唯一持久化的真相源**
- 持仓文件（holdings）只是派生视图，由账本重演后写盘，便于人工查看
- 追加式：保存时整体写回（账本通常很小），不做逐条更新
"""

from abc import ABC, abstractmethod
from decimal import Decimal
from pathlib import Path
import json
import logging
from typing import Optional

from src.domain.trading.ledger import LedgerSnapshot, TradeLedger
from src.domain.trading.trade import CashFlowType

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
    - `holdings.json` 持仓快照（派生视图，仅供人看，可随时重建）
    """

    def __init__(self, storage_dir: str = "data"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.trades_path = self.storage_dir / "trades.json"
        self.holdings_path = self.storage_dir / "holdings.json"

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

        with open(self.trades_path, "w", encoding="utf-8") as handle:
            json.dump(ledger.to_dict(), handle, ensure_ascii=False, indent=2)

        # 派生视图：持仓快照（由账本重演得出）
        self._write_holdings(ledger.replay())

        logger.info(f"已保存账本：{len(ledger)} 笔流水")

    # ------------------------------------------------------------------

    def _write_holdings(self, snapshot: LedgerSnapshot) -> None:
        """把重演结果写成持仓快照（派生数据，不参与计算）"""
        data = {
            "_schema": "holdings-v3",
            "_comment": (
                "派生视图：由 trades.json 重演生成，请勿手工编辑。"
                "资金池 real=真实现金, grid=网格虚拟预算。"
            ),
            "pools": {
                pool.value: {
                    "cash": float(snapshot.portfolio(pool).cash),
                    "positions": [
                        {
                            "code": code,
                            "shares": float(position.shares),
                            "cost_basis": float(position.cost_basis),
                            "average_cost": float(position.average_cost),
                            "realized_pnl": float(position.realized_pnl),
                        }
                        for code, position in snapshot.portfolio(pool).positions.items()
                        if position.shares > 0
                    ],
                }
                for pool in CashFlowType
            },
            "totals": {
                "positions": {
                    code: float(shares)
                    for code, shares in snapshot.total_positions().items()
                },
            },
        }

        with open(self.holdings_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------

    def rebuild_holdings(self) -> LedgerSnapshot:
        """仅重建持仓快照（不修改账本）"""
        snapshot = self.load().replay()
        self._write_holdings(snapshot)
        return snapshot

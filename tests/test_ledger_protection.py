"""
账本保护测试（离线）

**核心保证**：`data/trades.json` 是用户真实资产的唯一真相源，
任何构建 / 站点生成流程都**不得**改写它。

覆盖:
- 站点仓储只写 data/site/*，不碰账本
- build_pages.py 不写账本
- 账本仓储只在显式 save() 时写，且内容保真
"""

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.strategy.ledger.store import JsonFileTradeLedgerRepository  # noqa: E402

LEDGER_REL = Path("data") / "trades.json"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestBuildNeverTouchesLedger(unittest.TestCase):
    """构建流程不得改写用户账本"""

    def test_build_pages_script_does_not_write_ledger(self):
        """build_pages.py 源码里不得出现对账本路径的写入"""
        source = (ROOT / "scripts" / "build_pages.py").read_text(encoding="utf-8")
        # 只允许在注释/文档里提到 data/trades.json，不得有写操作
        code_lines = [
            line for line in source.splitlines()
            if "trades.json" in line and not line.strip().startswith("#")
        ]
        for line in code_lines:
            self.assertNotIn(
                "open(", line,
                f"build_pages.py 疑似打开账本文件写入: {line}")
            self.assertNotIn(
                "write_text", line,
                f"build_pages.py 疑似写账本: {line}")
            self.assertNotIn(
                "json.dump", line,
                f"build_pages.py 疑似写账本: {line}")

    def test_site_store_only_writes_site_dir(self):
        """站点仓储只往 data/site/ 写"""
        from src.chart.site_store import PagesSiteRepository

        repo = PagesSiteRepository(pages_dir=".")
        written = set()
        with mock.patch.object(
            PagesSiteRepository, "_write_json",
            side_effect=lambda path, payload: written.add(Path(path)),
        ):
            # 记录会走哪条路径
            repo.save_summary({"a": 1})
            repo.save_trades([{"id": "x"}])

        for path in written:
            self.assertNotEqual(Path(path).name, LEDGER_REL.name,
                                "站点仓储写了账本文件")
            self.assertIn("site", str(path).replace("\\", "/"),
                          f"站点产物应落在 data/site/ 下: {path}")

    def test_no_derived_trades_file_in_site(self):
        """站点目录不应有真实交易的副本（真相源唯一）"""
        site_trades = ROOT / "data" / "site" / "trades.json"
        self.assertFalse(
            site_trades.exists(),
            "data/site/trades.json 不该存在——真实交易只有 data/trades.json 一份")


class TestLedgerRoundTrip(unittest.TestCase):
    """账本读写保真"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ledger_path = Path(self.tmp) / "trades.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_preserves_all_trades(self):
        """save() 之后每一笔都还在，字段不丢"""
        payload = {
            "schema": "trade-ledger-v1",
            "trades": [
                {"id": "2026-10-19-buy-001", "date": "2026-10-19",
                 "side": "buy", "pool": "real", "fee": 0.0, "note": "",
                 "action": "open", "code": "515180",
                 "shares": 32000.0, "price": 1.396},
                {"id": "2026-10-20-buy-001", "date": "2026-10-20",
                 "side": "buy", "pool": "real", "fee": 0.0, "note": "",
                 "action": "open", "code": "513300",
                 "shares": 16100.0, "price": 2.729},
            ],
        }
        self.ledger_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        repo = JsonFileTradeLedgerRepository(self.tmp)
        ledger = repo.load()
        repo.save(ledger)

        saved = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["trades"]), 2)
        self.assertEqual(
            [t["id"] for t in saved["trades"]],
            ["2026-10-19-buy-001", "2026-10-20-buy-001"],
        )
        # 关键字段不丢
        first = saved["trades"][0]
        self.assertEqual(first["action"], "open")
        self.assertEqual(first["code"], "515180")
        self.assertEqual(first["shares"], 32000.0)
        self.assertEqual(first["price"], 1.396)

    def test_load_does_not_modify_file(self):
        """load() 是纯读"""
        original = ('{"schema": "trade-ledger-v1", "trades": [{"id": "x", '
                    '"date": "2026-10-19", "side": "buy", "pool": "real", '
                    '"code": "515180", "shares": 100, "price": 1.0, '
                    '"fee": 0.0, "note": ""}]}')
        self.ledger_path.write_text(original, encoding="utf-8")

        JsonFileTradeLedgerRepository(self.tmp).load()

        self.assertEqual(
            self.ledger_path.read_text(encoding="utf-8"), original)


class TestUserLedgerUntouched(unittest.TestCase):
    """仓库里的真实账本必须完好"""

    def test_repository_ledger_is_intact(self):
        """data/trades.json 能正常加载且自洽"""
        ledger_path = ROOT / LEDGER_REL
        if not ledger_path.exists():
            self.skipTest("本地无真实账本")

        repo = JsonFileTradeLedgerRepository(str(ROOT / "data"))
        ledger = repo.load()
        self.assertGreaterEqual(len(ledger), 0)
        # 载入不应改动文件
        before = _digest(ledger_path)
        repo.load()
        self.assertEqual(_digest(ledger_path), before)


if __name__ == '__main__':
    unittest.main()

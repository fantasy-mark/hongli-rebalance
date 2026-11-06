"""
DDD价格模型单元测试

测试核心领域对象和服务
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from datetime import date, datetime
from decimal import Decimal

from src.domain.price.entities import AssetCode, PricePoint, AdjustedPriceSeries, AssetType, DataSource
from src.domain.price.value_objects import ReturnMetrics, DateRange
from src.domain.price.services import PriceCalculator
from src.application.price.query_service import PriceQueryService


class TestAssetCode(unittest.TestCase):
    """测试资产代码值对象"""
    
    def test_create_from_string_sh(self):
        """测试上交所代码"""
        code = AssetCode.from_string("515180")
        self.assertEqual(code.code, "515180")
        self.assertEqual(code.exchange.value, "sh")
        self.assertTrue(code.is_etf())
    
    def test_create_from_string_sz(self):
        """测试深交所代码"""
        code = AssetCode.from_string("159937")
        self.assertEqual(code.code, "159937")
        self.assertEqual(code.exchange.value, "sz")
        self.assertTrue(code.is_etf())
    
    def test_invalid_code(self):
        """测试无效代码"""
        with self.assertRaises(ValueError):
            AssetCode.from_string("12345")  # 不是6位
    
    def test_to_sina_symbol(self):
        """测试转换为新浪符号"""
        code = AssetCode.from_string("515180")
        self.assertEqual(code.to_sina_symbol(), "sh515180")


class TestPricePoint(unittest.TestCase):
    """测试价格点值对象"""
    
    def test_create_valid_price(self):
        """测试创建有效价格"""
        pp = PricePoint(
            date=date(2024, 1, 2),
            open=Decimal("1.430"),
            high=Decimal("1.450"),
            low=Decimal("1.420"),
            close=Decimal("1.440"),
            volume=1000000,
            amount=Decimal("1440000"),
        )
        self.assertEqual(pp.close, Decimal("1.440"))
    
    def test_invalid_high_low(self):
        """测试最高价低于最低价"""
        with self.assertRaises(ValueError):
            PricePoint(
                date=date(2024, 1, 2),
                open=Decimal("1.430"),
                high=Decimal("1.400"),  # 错误：high < low
                low=Decimal("1.420"),
                close=Decimal("1.440"),
                volume=1000000,
            )
    
    def test_invalid_close(self):
        """测试收盘价为0"""
        with self.assertRaises(ValueError):
            PricePoint(
                date=date(2024, 1, 2),
                open=Decimal("1.430"),
                high=Decimal("1.450"),
                low=Decimal("1.420"),
                close=Decimal("0"),  # 错误：close <= 0
                volume=1000000,
            )
    
    def test_daily_return(self):
        """测试日收益率计算"""
        pp = PricePoint(
            date=date(2024, 1, 2),
            open=Decimal("1.430"),
            high=Decimal("1.450"),
            low=Decimal("1.420"),
            close=Decimal("1.440"),
            volume=1000000,
        )
        prev_close = Decimal("1.400")
        daily_ret = pp.daily_return(prev_close)
        expected = (Decimal("1.440") - Decimal("1.400")) / Decimal("1.400")
        self.assertAlmostEqual(float(daily_ret), float(expected), places=6)


class TestAdjustedPriceSeries(unittest.TestCase):
    """测试价格序列聚合根"""
    
    def setUp(self):
        """准备测试数据"""
        self.asset_code = AssetCode.from_string("515180")
        self.prices = [
            PricePoint(date=date(2024, 1, 2), open=Decimal("1.430"), high=Decimal("1.450"),
                      low=Decimal("1.420"), close=Decimal("1.440"), volume=1000000),
            PricePoint(date=date(2024, 1, 3), open=Decimal("1.440"), high=Decimal("1.460"),
                      low=Decimal("1.435"), close=Decimal("1.455"), volume=1100000),
            PricePoint(date=date(2024, 1, 4), open=Decimal("1.455"), high=Decimal("1.470"),
                      low=Decimal("1.450"), close=Decimal("1.465"), volume=1200000),
        ]
        self.series = AdjustedPriceSeries(
            asset_code=self.asset_code,
            asset_type=AssetType.ETF,
            prices=self.prices.copy(),
            source=DataSource.EASTMONEY_HFQ,
        )
    
    def test_latest_price(self):
        """测试获取最新价格"""
        latest = self.series.latest_price
        self.assertIsNotNone(latest)
        self.assertEqual(latest.date, date(2024, 1, 4))
        self.assertEqual(latest.close, Decimal("1.465"))
    
    def test_get_price(self):
        """测试获取指定日期价格"""
        pp = self.series.get_price(date(2024, 1, 3))
        self.assertIsNotNone(pp)
        self.assertEqual(pp.close, Decimal("1.455"))
    
    def test_get_price_not_found(self):
        """测试获取不存在的价格"""
        pp = self.series.get_price(date(2024, 1, 10))
        self.assertIsNone(pp)
    
    def test_add_price(self):
        """测试添加新价格"""
        new_price = PricePoint(
            date=date(2024, 1, 5),
            open=Decimal("1.465"),
            high=Decimal("1.480"),
            low=Decimal("1.460"),
            close=Decimal("1.475"),
            volume=1300000,
        )
        self.series.add_price(new_price)
        self.assertEqual(len(self.series), 4)
        self.assertEqual(self.series.latest_price.close, Decimal("1.475"))
    
    def test_add_duplicate_price(self):
        """测试添加重复日期价格"""
        duplicate = PricePoint(
            date=date(2024, 1, 2),
            open=Decimal("1.430"),
            high=Decimal("1.450"),
            low=Decimal("1.420"),
            close=Decimal("1.440"),
            volume=1000000,
        )
        with self.assertRaises(ValueError):
            self.series.add_price(duplicate)
    
    def test_validate_empty(self):
        """测试空序列验证"""
        empty_series = AdjustedPriceSeries(
            asset_code=self.asset_code,
            asset_type=AssetType.ETF,
            prices=[],
        )
        errors = empty_series.validate()
        self.assertIn("价格序列为空", errors)
    
    def test_to_dataframe(self):
        """测试转换为DataFrame"""
        df = self.series.to_dataframe()
        self.assertEqual(len(df), 3)
        self.assertIn('close', df.columns)
        self.assertIn('open', df.columns)


class TestPriceCalculator(unittest.TestCase):
    """测试价格计算器"""
    
    def setUp(self):
        """准备测试数据"""
        asset_code = AssetCode.from_string("515180")
        prices = [
            PricePoint(date=date(2024, 1, i), open=Decimal(str(1.4 + i*0.01)),
                      high=Decimal(str(1.45 + i*0.01)), low=Decimal(str(1.35 + i*0.01)),
                      close=Decimal(str(1.42 + i*0.01)), volume=1000000)
            for i in range(2, 12)
        ]
        self.series = AdjustedPriceSeries(
            asset_code=asset_code,
            asset_type=AssetType.ETF,
            prices=prices,
            source=DataSource.EASTMONEY_HFQ,
        )
        self.calculator = PriceCalculator()
    
    def test_total_return(self):
        """测试累计收益率"""
        total_ret = self.calculator.calculate_total_return(self.series)
        self.assertGreater(total_ret, 0)
    
    def test_cagr(self):
        """测试年化收益率"""
        cagr = self.calculator.calculate_cagr(self.series)
        self.assertIsInstance(cagr, Decimal)
    
    def test_volatility(self):
        """测试波动率"""
        vol = self.calculator.calculate_volatility(self.series, annualize=True)
        self.assertGreaterEqual(vol, 0)
    
    def test_max_drawdown(self):
        """测试最大回撤"""
        max_dd, start, end = self.calculator.calculate_max_drawdown(self.series)
        self.assertLessEqual(max_dd, 0)  # 回撤应该是负值或0


class TestDateRange(unittest.TestCase):
    """测试日期范围值对象"""
    
    def test_create_valid_range(self):
        """测试创建有效日期范围"""
        dr = DateRange(start=date(2024, 1, 1), end=date(2024, 12, 31))
        self.assertEqual(dr.duration_days(), 365)
    
    def test_invalid_range(self):
        """测试无效日期范围"""
        with self.assertRaises(ValueError):
            DateRange(start=date(2024, 12, 31), end=date(2024, 1, 1))
    
    def test_contains(self):
        """测试包含判断"""
        dr = DateRange(start=date(2024, 1, 1), end=date(2024, 12, 31))
        self.assertTrue(dr.contains(date(2024, 6, 15)))
        self.assertFalse(dr.contains(date(2025, 1, 1)))
    
    def test_intersects(self):
        """测试交集判断"""
        dr1 = DateRange(start=date(2024, 1, 1), end=date(2024, 6, 30))
        dr2 = DateRange(start=date(2024, 6, 1), end=date(2024, 12, 31))
        self.assertTrue(dr1.intersects(dr2))


class TestPriceQueryService(unittest.TestCase):
    """测试价格查询服务（集成测试）"""
    
    def test_get_adjusted_prices(self):
        """测试获取价格数据"""
        service = PriceQueryService()
        df = service.get_adjusted_prices("515180", start_date="2026-11-01")
        
        self.assertFalse(df.empty)
        self.assertIn('close', df.columns)
        # DataFrame中的值是float，需要转换后比较
        self.assertAlmostEqual(float(df['close'].iloc[-1]), 1.762, places=3)
    
    def test_get_latest_price(self):
        """测试获取最新价格"""
        service = PriceQueryService()
        latest = service.get_latest_price("515180")
        
        self.assertEqual(latest['code'], "515180")
        self.assertEqual(latest['close'], Decimal("1.762"))
        self.assertIn('date', latest)
        self.assertIn('change_pct', latest)
    
    def test_calculate_returns(self):
        """测试收益率计算"""
        service = PriceQueryService()
        metrics = service.calculate_returns("515180", start_date="2026-01-01")
        
        self.assertIsInstance(metrics, ReturnMetrics)
        self.assertGreater(metrics.trading_days, 0)
        self.assertIsInstance(metrics.total_return, Decimal)
    
    def test_compare_assets(self):
        """测试多资产对比"""
        service = PriceQueryService()
        codes = ["515180", "159937"]
        df = service.compare_assets(codes, start_date="2026-11-01", normalize=True)
        
        self.assertFalse(df.empty)
        self.assertEqual(len(df.columns), 2)
        self.assertIn("515180", df.columns)
        self.assertIn("159937", df.columns)


if __name__ == '__main__':
    unittest.main()

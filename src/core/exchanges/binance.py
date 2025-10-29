"""
币安交易所实现

提供币安交易所的完整功能支持，包括：
- 现货交易
- Alpha 2.0 流动性（替换 Simple Earn）
- 精度处理
- 时间同步

作者: GridBNB Team
版本: 1.0.0
"""

import ccxt.async_support as ccxt
import time
from typing import Dict, Optional
from src.core.exchanges.base import (
    BaseExchange,
    ISavingsFeature,
    ExchangeCapabilities
)
from src.core.exchanges.factory import ExchangeConfig


class BinanceExchange(BaseExchange, ISavingsFeature):
    """
    币安交易所实现

    特性：
    - 完整的现货交易支持
    - Alpha 2.0 流动性操作
    - 灵活的精度处理
    - 自动时间同步
    """

    def __init__(self, config: ExchangeConfig):
        """
        初始化币安交易所

        Args:
            config: 交易所配置
        """
        # 调用基类初始化
        super().__init__('binance', config)

        # 理财余额缓存
        self.funding_balance_cache = {'timestamp': 0, 'data': {}}
        self._alpha_exchange_cache = None

        self.logger.info("币安交易所初始化完成")

    def _create_ccxt_instance(self):
        """创建币安CCXT实例"""
        return ccxt.binance({
            **self.config.to_ccxt_config(),
            'options': {
                'defaultType': 'spot',
                'fetchMarkets': {
                    'spot': True,
                    'margin': self.config.enable_margin,
                    'swap': False,
                    'future': False
                },
                'recvWindow': 5000,
                'adjustForTimeDifference': True,
                'warnOnFetchOpenOrdersWithoutSymbol': False,
                'createMarketBuyOrderRequiresPrice': False
            }
        })

    @property
    def capabilities(self):
        """币安支持的功能"""
        caps = [
            ExchangeCapabilities.SPOT_TRADING,
            ExchangeCapabilities.SAVINGS,
        ]
        if self.config.enable_margin:
            caps.append(ExchangeCapabilities.MARGIN_TRADING)
        return caps

    # ========================================================================
    # 理财功能实现 (ISavingsFeature)
    # ========================================================================

    async def fetch_funding_balance(self) -> Dict[str, float]:
        """获取 Alpha 2.0 钱包余额"""
        if not self.config.enable_savings:
            return {}

        now = time.time()
        if now - self.funding_balance_cache['timestamp'] < self.cache_ttl:
            return self.funding_balance_cache['data']

        try:
            params = {
                'timestamp': int(time.time() * 1000 + self.time_diff),
                'recvWindow': 5000,
            }
            result = await self.exchange.request(
                'v1/asset/get-alpha-asset', 'sapi', 'GET', params
            )

            balances: Dict[str, float] = {}
            for item in result:
                asset_code = item.get('cexAssetCode') or item.get('alphaId')
                if not asset_code:
                    continue
                amount = float(item.get('amount', 0) or 0)
                if amount > 0:
                    balances[asset_code] = amount

            self.funding_balance_cache = {'timestamp': now, 'data': balances}
            self.logger.debug(f"Alpha 余额: {balances}")
            return balances
        except Exception as exc:
            self.logger.error(f"获取 Alpha 余额失败: {exc}")
            return self.funding_balance_cache.get('data', {})

    async def transfer_to_savings(self, asset: str, amount: float) -> dict:
        """通过 Alpha 2.0 买入资产以提供流动性"""
        if not self.config.enable_savings:
            raise RuntimeError("理财功能未启用")

        try:
            symbol_info = await self._get_alpha_symbol_info(asset)
            price_value = await self._get_alpha_ticker_price(symbol_info['symbol'])
            if price_value <= 0:
                raise ValueError(f"Alpha 交易对 {symbol_info['symbol']} 缺少有效价格")

            quantity_value = amount / price_value
            quantity = self._format_alpha_value(
                quantity_value, symbol_info.get('quantityPrecision', 8)
            )
            price = self._format_alpha_value(
                price_value, symbol_info.get('pricePrecision', 8)
            )

            params = {
                'baseAsset': symbol_info['baseAsset'],
                'quoteAsset': asset,
                'side': 'BUY',
                'quantity': quantity,
                'price': price,
                'timestamp': int(time.time() * 1000 + self.time_diff),
                'recvWindow': 5000,
            }

            self.logger.info(
                f"Alpha 买入: {quantity} {symbol_info['baseAsset']} @ {price} ({asset})"
            )
            result = await self.exchange.request(
                'v1/alpha-trade/order/place', 'sapi', 'POST', params
            )

            self._clear_balance_cache()

            self.logger.info(f"Alpha 买入成功: {result}")
            return result
        except Exception as exc:
            self.logger.error(f"Alpha 买入失败: {exc}")
            raise

    async def transfer_to_spot(self, asset: str, amount: float) -> dict:
        """通过 Alpha 2.0 卖出资产回收流动性"""
        if not self.config.enable_savings:
            raise RuntimeError("理财功能未启用")

        try:
            symbol_info = await self._get_alpha_symbol_info(asset)
            price_value = await self._get_alpha_ticker_price(symbol_info['symbol'])
            if price_value <= 0:
                raise ValueError(f"Alpha 交易对 {symbol_info['symbol']} 缺少有效价格")

            quantity_value = amount / price_value
            quantity = self._format_alpha_value(
                quantity_value, symbol_info.get('quantityPrecision', 8)
            )
            price = self._format_alpha_value(
                price_value, symbol_info.get('pricePrecision', 8)
            )

            params = {
                'baseAsset': symbol_info['baseAsset'],
                'quoteAsset': asset,
                'side': 'SELL',
                'quantity': quantity,
                'price': price,
                'timestamp': int(time.time() * 1000 + self.time_diff),
                'recvWindow': 5000,
            }

            self.logger.info(
                f"Alpha 卖出: {quantity} {symbol_info['baseAsset']} @ {price} ({asset})"
            )
            result = await self.exchange.request(
                'v1/alpha-trade/order/place', 'sapi', 'POST', params
            )

            self._clear_balance_cache()

            self.logger.info(f"Alpha 卖出成功: {result}")
            return result
        except Exception as exc:
            self.logger.error(f"Alpha 卖出失败: {exc}")
            raise

    # ========================================================================
    # 币安特定辅助方法
    # ========================================================================

    async def _get_alpha_symbol_info(self, quote_asset: str) -> Dict[str, Any]:
        cache = self._alpha_exchange_cache
        now = time.time()
        if cache and now - cache[0] < 30:
            exchange_info = cache[1]
        else:
            params = {
                'timestamp': int(time.time() * 1000 + self.time_diff),
                'recvWindow': 5000,
            }
            exchange_info = await self.exchange.request(
                'v1/alpha-trade/get-exchange-info', 'sapi', 'GET', params
            )
            self._alpha_exchange_cache = (now, exchange_info)

        for symbol in exchange_info.get('symbols', []):
            if symbol.get('quoteAsset') == quote_asset and symbol.get('status') == 'TRADING':
                return symbol

        raise ValueError(f"未找到报价资产为 {quote_asset} 的 Alpha 交易对")

    async def _get_alpha_ticker_price(self, symbol: str) -> float:
        params = {
            'symbol': symbol,
            'timestamp': int(time.time() * 1000 + self.time_diff),
            'recvWindow': 5000,
        }
        ticker = await self.exchange.request(
            'v1/alpha-trade/market/ticker-price', 'sapi', 'GET', params
        )
        return float(ticker.get('price', 0) or 0)

    @staticmethod
    def _format_alpha_value(value: float, precision: int) -> str:
        return format(value, f'.{precision}f')

    def _clear_balance_cache(self):
        """清除余额缓存"""
        self.balance_cache = {'timestamp': 0, 'data': None}
        self.funding_balance_cache = {'timestamp': 0, 'data': {}}

"""
币安交易所适配器

实现币安交易所的完整功能，包括：
- 现货交易
- Alpha 2.0 流动性 (替换原 Simple Earn)
- 账户划转
"""

from typing import Dict, List, Optional, Any, Tuple
import time
import ccxt.async_support as ccxt
from src.core.exchange.base import (
    BaseExchangeAdapter,
    ExchangeType,
    ExchangeFeature,
    ExchangeCapabilities
)


class BinanceAdapter(BaseExchangeAdapter):
    """
    币安交易所适配器

    支持功能：
    - ✅ 现货交易
    - ✅ Alpha 2.0 流动性
    - ✅ 账户划转
    """

    @property
    def exchange_type(self) -> ExchangeType:
        return ExchangeType.BINANCE

    @property
    def capabilities(self) -> ExchangeCapabilities:
        """币安支持现货交易和理财功能"""
        return ExchangeCapabilities([
            ExchangeFeature.SPOT_TRADING,
            ExchangeFeature.FUNDING_ACCOUNT,
        ])

    async def initialize(self) -> None:
        """初始化币安连接"""
        self.logger.info("正在初始化币安交易所连接...")

        self._alpha_exchange_cache = None

        self._exchange = ccxt.binance({
            'apiKey': self.api_key,
            'secret': self.api_secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot',
                'adjustForTimeDifference': True,
            }
        })

        # 加载市场数据
        await self._exchange.load_markets()

        # 验证连接
        balance = await self._exchange.fetch_balance()
        self.logger.info(
            f"✅ 币安连接成功 | "
            f"账户资产: {len([k for k, v in balance['free'].items() if float(v) > 0])} 种"
        )

    async def close(self) -> None:
        """关闭连接"""
        if self._exchange:
            await self._exchange.close()
            self.logger.info("币安连接已关闭")

    # ==================== 核心交易接口实现 ====================

    async def fetch_balance(self, account_type: str = 'spot') -> Dict[str, Any]:
        """获取账户余额"""
        return await self._exchange.fetch_balance({'type': account_type})

    async def fetch_ticker(self, symbol: str) -> Dict[str, Any]:
        """获取行情"""
        return await self._exchange.fetch_ticker(symbol)

    async def fetch_order_book(self, symbol: str, limit: int = 5) -> Dict[str, Any]:
        """获取订单簿"""
        return await self._exchange.fetch_order_book(symbol, limit)

    async def create_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
        params: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """创建订单"""
        # 币安特定：amount必须是字符串格式（CCXT会自动处理）
        return await self._exchange.create_order(
            symbol, order_type, side, amount, price, params
        )

    async def cancel_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        """取消订单"""
        return await self._exchange.cancel_order(order_id, symbol)

    async def fetch_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        """查询订单"""
        return await self._exchange.fetch_order(order_id, symbol)

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取未成交订单"""
        return await self._exchange.fetch_open_orders(symbol)

    async def fetch_my_trades(self, symbol: str, limit: int = 50) -> List[Dict[str, Any]]:
        """获取成交历史"""
        return await self._exchange.fetch_my_trades(symbol, limit=limit)

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = '1h',
        limit: int = 100
    ) -> List[List]:
        """获取K线数据"""
        return await self._exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

    # ==================== 精度处理 ====================

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        """调整数量精度"""
        return self._exchange.amount_to_precision(symbol, amount)

    def price_to_precision(self, symbol: str, price: float) -> str:
        """调整价格精度"""
        return self._exchange.price_to_precision(symbol, price)

    async def load_markets(self, reload: bool = False) -> Dict[str, Any]:
        """加载市场信息"""
        return await self._exchange.load_markets(reload)

    # ==================== Alpha 2.0 流动性接口 ====================

    async def fetch_funding_balance(self) -> Dict[str, float]:
        """获取 Alpha 2.0 钱包余额"""
        try:
            params = {
                'timestamp': self._exchange.milliseconds(),
                'recvWindow': 5000,
            }
            response = await self._exchange.request(
                'v1/asset/get-alpha-asset', 'sapi', 'GET', params
            )

            balances: Dict[str, float] = {}
            for item in response:
                code = item.get('cexAssetCode') or item.get('alphaId')
                if not code:
                    continue
                amount = float(item.get('amount', 0) or 0)
                if amount > 0:
                    balances[code] = amount

            return balances
        except Exception as exc:
            self.logger.error(f"获取 Alpha 资产余额失败: {exc}")
            return {}

    async def transfer_to_funding(self, asset: str, amount: float) -> bool:
        """通过 Alpha 2.0 下单将资金转入流动性"""
        try:
            base_asset, quote_asset, price, quantity = await self._build_alpha_order(
                asset, amount
            )

            params = {
                'baseAsset': base_asset,
                'quoteAsset': quote_asset,
                'side': 'BUY',
                'quantity': quantity,
                'price': price,
                'timestamp': self._exchange.milliseconds(),
                'recvWindow': 5000,
            }

            await self._exchange.request(
                'v1/alpha-trade/order/place', 'sapi', 'POST', params
            )
            return True
        except Exception as exc:
            self.logger.error(f"Alpha 下单失败（BUY）: {exc}")
            return False

    async def transfer_to_spot(self, asset: str, amount: float) -> bool:
        """通过 Alpha 2.0 下单将资金转回现货"""
        try:
            base_asset, quote_asset, price, quantity = await self._build_alpha_order(
                asset, amount
            )

            params = {
                'baseAsset': base_asset,
                'quoteAsset': quote_asset,
                'side': 'SELL',
                'quantity': quantity,
                'price': price,
                'timestamp': self._exchange.milliseconds(),
                'recvWindow': 5000,
            }

            await self._exchange.request(
                'v1/alpha-trade/order/place', 'sapi', 'POST', params
            )
            return True
        except Exception as exc:
            self.logger.error(f"Alpha 下单失败（SELL）: {exc}")
            return False

    async def get_alpha_exchange_info(self) -> Dict[str, Any]:
        """获取 Alpha 交易所信息"""
        cache = getattr(self, '_alpha_exchange_cache', None)
        now = time.time()
        if cache and now - cache[0] < 30:
            return cache[1]

        params = {
            'timestamp': self._exchange.milliseconds(),
            'recvWindow': 5000,
        }
        info = await self._exchange.request(
            'v1/alpha-trade/get-exchange-info', 'sapi', 'GET', params
        )
        self._alpha_exchange_cache = (now, info)
        return info

    async def get_alpha_ticker_price(self, symbol: str) -> float:
        """获取 Alpha 交易对的最新价格"""
        params = {
            'symbol': symbol,
            'timestamp': self._exchange.milliseconds(),
            'recvWindow': 5000,
        }
        ticker = await self._exchange.request(
            'v1/alpha-trade/market/ticker-price', 'sapi', 'GET', params
        )
        return float(ticker.get('price', 0) or 0)

    async def _build_alpha_order(self, quote_asset: str, amount: float) -> Tuple[str, str, str, str]:
        """根据报价资产和金额构建 Alpha 下单信息"""
        exchange_info = await self.get_alpha_exchange_info()
        symbols = exchange_info.get('symbols', [])
        symbol_info = next(
            (
                s for s in symbols
                if s.get('quoteAsset') == quote_asset and s.get('status') == 'TRADING'
            ),
            None,
        )

        if not symbol_info:
            raise ValueError(f"未找到可交易的 Alpha 交易对（quote={quote_asset}）")

        symbol_name = symbol_info['symbol']
        price_value = await self.get_alpha_ticker_price(symbol_name)
        if price_value <= 0:
            raise ValueError(f"Alpha 交易对 {symbol_name} 缺少有效价格")

        quantity_value = amount / price_value
        price = self._format_with_precision(
            price_value, symbol_info.get('pricePrecision', 8)
        )
        quantity = self._format_with_precision(
            quantity_value, symbol_info.get('quantityPrecision', 8)
        )

        return symbol_info['baseAsset'], quote_asset, price, quantity

    @staticmethod
    def _format_with_precision(value: float, precision: int) -> str:
        return format(value, f'.{precision}f')

    # ==================== 币安特定工具方法 ====================

    async def get_account_status(self) -> Dict[str, Any]:
        """获取账户状态（币安特定）"""
        try:
            return await self._exchange.sapiGetV1AccountStatus()
        except Exception as e:
            self.logger.error(f"获取账户状态失败: {e}")
            return {}

    async def get_api_trading_status(self) -> Dict[str, Any]:
        """获取API交易状态（币安特定）"""
        try:
            return await self._exchange.sapiGetV1AccountApiTradingStatus()
        except Exception as e:
            self.logger.error(f"获取API交易状态失败: {e}")
            return {}

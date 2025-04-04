import json
from abc import abstractmethod
from collections import deque
from datamodel import Listing, Observation, Order, OrderDepth, ProsperityEncoder, Symbol, Trade, TradingState
from typing import Any, TypeAlias, Dict

JSON: TypeAlias = dict[str, "JSON"] | list["JSON"] | str | int | float | bool | None

class Logger:
    def __init__(self) -> None:
        self.logs = ""
        self.max_log_length = 3750

    def print(self, *objects: Any, sep: str = " ", end: str = "\n") -> None:
        self.logs += sep.join(map(str, objects)) + end

    def flush(self, state: TradingState, orders: dict[Symbol, list[Order]], conversions: int, trader_data: str) -> None:
        base_length = len(self.to_json([
            self.compress_state(state, ""),
            self.compress_orders(orders),
            conversions,
            "",
            "",
        ]))

        # We truncate state.traderData, trader_data, and self.logs to the same max. length to fit the log limit
        max_item_length = (self.max_log_length - base_length) // 3

        print(self.to_json([
            self.compress_state(state, self.truncate(state.traderData, max_item_length)),
            self.compress_orders(orders),
            conversions,
            self.truncate(trader_data, max_item_length),
            self.truncate(self.logs, max_item_length),
        ]))

        self.logs = ""

    def compress_state(self, state: TradingState, trader_data: str) -> list[Any]:
        return [
            state.timestamp,
            trader_data,
            self.compress_listings(state.listings),
            self.compress_order_depths(state.order_depths),
            self.compress_trades(state.own_trades),
            self.compress_trades(state.market_trades),
            state.position,
            self.compress_observations(state.observations),
        ]

    def compress_listings(self, listings: dict[Symbol, Listing]) -> list[list[Any]]:
        compressed = []
        for listing in listings.values():
            compressed.append([listing.symbol, listing.product, listing.denomination])

        return compressed

    def compress_order_depths(self, order_depths: dict[Symbol, OrderDepth]) -> dict[Symbol, list[Any]]:
        compressed = {}
        for symbol, order_depth in order_depths.items():
            compressed[symbol] = [order_depth.buy_orders, order_depth.sell_orders]

        return compressed

    def compress_trades(self, trades: dict[Symbol, list[Trade]]) -> list[list[Any]]:
        compressed = []
        for arr in trades.values():
            for trade in arr:
                compressed.append([
                    trade.symbol,
                    trade.price,
                    trade.quantity,
                    trade.buyer,
                    trade.seller,
                    trade.timestamp,
                ])

        return compressed

    def compress_observations(self, observations: Observation) -> list[Any]:
        conversion_observations = {}
        for product, observation in observations.conversionObservations.items():
            conversion_observations[product] = [
                observation.bidPrice,
                observation.askPrice,
                observation.transportFees,
                observation.exportTariff,
                observation.importTariff,
                observation.sunlight,
                observation.humidity,
            ]

        return [observations.plainValueObservations, conversion_observations]

    def compress_orders(self, orders: dict[Symbol, list[Order]]) -> list[list[Any]]:
        compressed = []
        for arr in orders.values():
            for order in arr:
                compressed.append([order.symbol, order.price, order.quantity])

        return compressed

    def to_json(self, value: Any) -> str:
        return json.dumps(value, cls=ProsperityEncoder, separators=(",", ":"))

    def truncate(self, value: str, max_length: int) -> str:
        if len(value) <= max_length:
            return value

        return value[:max_length - 3] + "..."

logger = Logger()

class Strategy:
    def __init__(self, symbol: str, limit: int) -> None:
        self.symbol = symbol
        self.limit = limit

    @abstractmethod
    def act(self, state: TradingState) -> None:
        raise NotImplementedError()

    def run(self, state: TradingState) -> list[Order]:
        self.orders = []
        self.act(state)
        return self.orders

    def buy(self, price: int, quantity: int) -> None:
        self.orders.append(Order(self.symbol, price, quantity))

    def sell(self, price: int, quantity: int) -> None:
        self.orders.append(Order(self.symbol, price, -quantity))

    def save(self) -> JSON:
        return None

    def load(self, data: JSON) -> None:
        pass

class MarketMakingStrategy(Strategy):
    def __init__(self, symbol: Symbol, limit: int) -> None:
        super().__init__(symbol, limit)

        self.window = deque()
        self.window_size = 10

    @abstractmethod
    def get_true_value(state: TradingState) -> int:
        raise NotImplementedError()

    def act(self, state: TradingState) -> None:
        true_value = self.get_true_value(state)

        order_depth = state.order_depths[self.symbol]
        buy_orders = sorted(order_depth.buy_orders.items(), reverse=True)
        sell_orders = sorted(order_depth.sell_orders.items())

        position = state.position.get(self.symbol, 0)
        to_buy = self.limit - position
        to_sell = self.limit + position

        self.window.append(abs(position) == self.limit)
        if len(self.window) > self.window_size:
            self.window.popleft()

        soft_liquidate = len(self.window) == self.window_size and sum(self.window) >= self.window_size / 2 and self.window[-1]
        hard_liquidate = len(self.window) == self.window_size and all(self.window)

        max_buy_price = true_value - 1 if position > self.limit * 0.5 else true_value
        min_sell_price = true_value + 1 if position < self.limit * -0.5 else true_value

        for price, volume in sell_orders:
            if to_buy > 0 and price <= max_buy_price:
                quantity = min(to_buy, -volume)
                self.buy(price, quantity)
                to_buy -= quantity

        if to_buy > 0 and hard_liquidate:
            quantity = to_buy // 2
            self.buy(true_value, quantity)
            to_buy -= quantity

        if to_buy > 0 and soft_liquidate:
            quantity = to_buy // 2
            self.buy(true_value - 2, quantity)
            to_buy -= quantity

        if to_buy > 0:
            popular_buy_price = max(buy_orders, key=lambda tup: tup[1])[0]
            price = min(max_buy_price, popular_buy_price + 1)
            self.buy(price, to_buy)

        for price, volume in buy_orders:
            if to_sell > 0 and price >= min_sell_price:
                quantity = min(to_sell, volume)
                self.sell(price, quantity)
                to_sell -= quantity

        if to_sell > 0 and hard_liquidate:
            quantity = to_sell // 2
            self.sell(true_value, quantity)
            to_sell -= quantity

        if to_sell > 0 and soft_liquidate:
            quantity = to_sell // 2
            self.sell(true_value + 2, quantity)
            to_sell -= quantity

        if to_sell > 0:
            popular_sell_price = min(sell_orders, key=lambda tup: tup[1])[0]
            price = max(min_sell_price, popular_sell_price - 1)
            self.sell(price, to_sell)

    def save(self) -> JSON:
        return list(self.window)

    def load(self, data: JSON) -> None:
        self.window = deque(data)

class MACDRSIStrategy(Strategy):
    def __init__(self, symbol: str, limit: int, short_window: int = 12, long_window: int = 26, signal_window: int = 9, rsi_window: int = 14) -> None:
        super().__init__(symbol, limit)
        self.prices = deque(maxlen=max(long_window, rsi_window))
        self.short_window = short_window
        self.long_window = long_window
        self.signal_window = signal_window
        self.rsi_window = rsi_window
        self.ema_short = None
        self.ema_long = None
        self.signal_ema = None
        self.rsi_values = deque(maxlen=rsi_window)

    def calculate_macd(self) -> float:
        if len(self.prices) < self.long_window:
            return 0  # Not enough data
        
        # Compute EMAs
        if self.ema_short is None:
            self.ema_short = sum(list(self.prices)[-self.short_window:]) / self.short_window
        else:
            self.ema_short = (self.prices[-1] * (2 / (self.short_window + 1))) + (self.ema_short * (1 - (2 / (self.short_window + 1))))

        if self.ema_long is None:
            self.ema_long = sum(list(self.prices)[-self.long_window:]) / self.long_window
        else:
            self.ema_long = (self.prices[-1] * (2 / (self.long_window + 1))) + (self.ema_long * (1 - (2 / (self.long_window + 1))))

        macd = self.ema_short - self.ema_long

        if self.signal_ema is None:
            self.signal_ema = macd
        else:
            self.signal_ema = (macd * (2 / (self.signal_window + 1))) + (self.signal_ema * (1 - (2 / (self.signal_window + 1))))

        return macd - self.signal_ema

    def calculate_rsi(self) -> float:
        if len(self.prices) < self.rsi_window:
            return 50  # Neutral RSI when insufficient data
        
        gains = [max(0, self.prices[i] - self.prices[i - 1]) for i in range(1, len(self.prices))]
        losses = [max(0, self.prices[i - 1] - self.prices[i]) for i in range(1, len(self.prices))]
        avg_gain = sum(gains) / self.rsi_window if len(gains) >= self.rsi_window else sum(gains) / max(1, len(gains))
        avg_loss = sum(losses) / self.rsi_window if len(losses) >= self.rsi_window else sum(losses) / max(1, len(losses))

        if avg_loss == 0:
            return 100  # RSI at max
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def act(self, state: TradingState) -> None:
        order_depth = state.order_depths[self.symbol]
        position = state.position.get(self.symbol, 0)

        buy_orders = sorted(order_depth.buy_orders.items(), reverse=True)
        sell_orders = sorted(order_depth.sell_orders.items())
        
        if not buy_orders or not sell_orders:
            return
        
        current_price = (buy_orders[0][0] + sell_orders[0][0]) / 2
        self.prices.append(current_price)

        macd_signal = self.calculate_macd()
        rsi = self.calculate_rsi()

        to_buy = self.limit - position
        to_sell = self.limit + position

        if macd_signal > 0 and rsi < 30 and to_buy > 0:
            self.buy(int(current_price), min(to_buy, 10))

        if macd_signal < 0 and rsi > 70 and to_sell > 0:
            self.sell(int(current_price), min(to_sell, 10))

    def save(self) -> JSON:
        return list(self.prices)

    def load(self, data: JSON) -> None:
        self.prices = deque(data, maxlen=max(self.long_window, self.rsi_window))

class RainForestResinStrategy(MACDRSIStrategy):
    def __init__(self, symbol: str, limit: int) -> None:
        super().__init__(symbol, limit)

class KelpStrategy(MACDRSIStrategy):
    def __init__(self, symbol: str, limit: int) -> None:
        super().__init__(symbol, limit)

    
    
    
class Trader:
    def __init__(self) -> None:
        limits = {
            "RAINFOREST_RESIN": 50,
            "KELP": 50,
        }

        self.strategies = {symbol: clazz(symbol, limits[symbol]) for symbol, clazz in {
            "RAINFOREST_RESIN": RainForestResinStrategy,
            "KELP": KelpStrategy,
        }.items()}

    def run(self, state: TradingState) -> tuple[dict[Symbol, list[Order]], int, str]:
        logger.print(state.position)

        conversions = 0

        old_trader_data = json.loads(state.traderData) if state.traderData != "" else {}
        new_trader_data = {}

        orders = {}
        for symbol, strategy in self.strategies.items():
            if symbol in old_trader_data:
                strategy.load(old_trader_data.get(symbol, None))

            if symbol in state.order_depths:
                orders[symbol] = strategy.run(state)

            new_trader_data[symbol] = strategy.save()

        trader_data = json.dumps(new_trader_data, separators=(",", ":"))

        logger.flush(state, orders, conversions, trader_data)
        return orders, conversions, trader_data
"""
BacktestEngine - 基于VectorBT的回测引擎，带纯numpy回退实现
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any

import numpy as np
import pandas as pd

from backend.backtest.strategy import parse_strategy, generate_signals
from backend.backtest.report import generate_report

logger = logging.getLogger(__name__)

# 尝试导入vectorbt，失败则使用纯numpy回退
try:
    import vectorbt as vbt

    HAS_VBT = True
    logger.info("VectorBT 已加载，使用向量化回测引擎")
except ImportError:
    HAS_VBT = False
    logger.warning("VectorBT 未安装，使用纯numpy回退引擎")


class BacktestEngine:
    """回测引擎：支持VectorBT向量化回测与纯numpy回退"""

    def __init__(self, config: Optional[Dict] = None):
        config = config or {}
        self.initial_capital = config.get("initial_capital", 100000)
        self.commission = config.get("commission", 0.001)
        self.slippage = config.get("slippage", 0.0005)
        self.risk_free_rate = config.get("risk_free_rate", 0.03)

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    async def run(
        self,
        symbol: str,
        interval: str,
        start_date: str,
        end_date: str,
        strategy_code: str,
        strategy_type: str = "openscript",
    ) -> Dict[str, Any]:
        """
        执行单次回测。

        1. 从数据库加载历史K线
        2. 解析策略代码，生成买卖信号
        3. 用VectorBT/numpy执行模拟交易
        4. 计算统计指标
        5. 生成报告
        """
        # 1) 加载K线数据
        ohlcv = await self._load_ohlcv(symbol, interval, start_date, end_date)
        if ohlcv is None or ohlcv.empty:
            raise ValueError(f"未找到 {symbol} 在 {start_date}~{end_date} 的K线数据")

        # 2) 解析策略 & 生成信号
        strategy = parse_strategy(strategy_code, strategy_type)
        entries, exits = generate_signals(strategy, ohlcv)

        # 3) 执行模拟交易
        if HAS_VBT:
            trades, equity = self._run_vbt(ohlcv, entries, exits)
        else:
            trades, equity = self._run_numpy(ohlcv, entries, exits)

        # 4-5) 生成报告
        benchmark = self._calc_benchmark(ohlcv)
        report = generate_report(trades, equity, benchmark, ohlcv)

        report["meta"] = {
            "symbol": symbol,
            "interval": interval,
            "start_date": start_date,
            "end_date": end_date,
            "initial_capital": self.initial_capital,
            "commission": self.commission,
            "slippage": self.slippage,
        }
        return report

    async def optimize(
        self,
        symbol: str,
        interval: str,
        start_date: str,
        end_date: str,
        strategy_code: str,
        param_grid: Dict[str, List[Any]],
    ) -> Dict[str, Any]:
        """
        参数优化回测。

        遍历 param_grid 的笛卡尔积，对每组参数跑回测，
        返回所有组合结果 + 最优参数 + 热力图数据。
        """
        from itertools import product as iterproduct

        ohlcv = await self._load_ohlcv(symbol, interval, start_date, end_date)
        if ohlcv is None or ohlcv.empty:
            raise ValueError(f"未找到 {symbol} 在 {start_date}~{end_date} 的K线数据")

        strategy = parse_strategy(strategy_code, "openscript")

        param_names = list(param_grid.keys())
        param_values = list(param_grid.values())
        combinations = list(iterproduct(*param_values))

        results: List[Dict] = []
        best_sharpe = -np.inf
        best_params: Dict = {}
        best_result: Dict = {}

        for combo in combinations:
            params = dict(zip(param_names, combo))
            entries, exits = generate_signals(strategy, ohlcv, params)

            if HAS_VBT:
                trades, equity = self._run_vbt(ohlcv, entries, exits)
            else:
                trades, equity = self._run_numpy(ohlcv, entries, exits)

            benchmark = self._calc_benchmark(ohlcv)
            report = generate_report(trades, equity, benchmark, ohlcv)

            row = {**params, **report["summary"]}
            results.append(row)

            sharpe = report["summary"].get("sharpe_ratio", -np.inf)
            if sharpe > best_sharpe:
                best_sharpe = sharpe
                best_params = params
                best_result = report

        # 构建热力图数据（取前两个参数维度）
        heatmap_data = None
        if len(param_names) >= 2:
            heatmap_data = self._build_heatmap(results, param_names[0], param_names[1])

        return {
            "results": results,
            "best_params": best_params,
            "best_result": best_result,
            "heatmap": heatmap_data,
            "total_combinations": len(combinations),
        }

    # ------------------------------------------------------------------
    # VectorBT 引擎
    # ------------------------------------------------------------------

    def _run_vbt(self, ohlcv: pd.DataFrame, entries: np.ndarray, exits: np.ndarray) -> tuple:
        """使用VectorBT执行回测"""
        close = ohlcv["close"].values
        price = pd.Series(close, index=ohlcv.index)

        pf = vbt.Portfolio.from_signals(
            close=price,
            entries=pd.Series(entries, index=ohlcv.index),
            exits=pd.Series(exits, index=ohlcv.index),
            init_cash=self.initial_capital,
            fees=self.commission,
            slippage=self.slippage,
            freq="1D",
        )

        # 提取成交记录
        trades_df = pf.trades.records_readable
        trades_list = []
        if trades_df is not None and len(trades_df) > 0:
            for _, row in trades_df.iterrows():
                trades_list.append(
                    {
                        "entry_time": str(row.get("Entry Timestamp", "")),
                        "exit_time": str(row.get("Exit Timestamp", "")),
                        "entry_price": float(row.get("Avg Entry Price", 0)),
                        "exit_price": float(row.get("Avg Exit Price", 0)),
                        "size": float(row.get("Size", 0)),
                        "pnl": float(row.get("PnL", 0)),
                        "return_pct": float(row.get("Return", 0)) * 100,
                        "direction": "long",
                    }
                )

        equity = pf.value().values
        return trades_list, equity

    # ------------------------------------------------------------------
    # 纯numpy回退引擎
    # ------------------------------------------------------------------

    def _run_numpy(self, ohlcv: pd.DataFrame, entries: np.ndarray, exits: np.ndarray) -> tuple:
        """纯numpy模拟交易引擎"""
        close = ohlcv["close"].values
        n = len(close)

        cash = float(self.initial_capital)
        position = 0.0
        equity = np.zeros(n, dtype=np.float64)
        trades_list: List[Dict] = []

        entry_price = 0.0
        entry_idx = 0

        for i in range(n):
            price = close[i]

            # 开仓
            if entries[i] and position == 0:
                slip_price = price * (1 + self.slippage)
                affordable = cash / (slip_price * (1 + self.commission))
                position = affordable
                cost = position * slip_price
                fee = cost * self.commission
                cash -= cost + fee
                entry_price = slip_price
                entry_idx = i

            # 平仓
            elif exits[i] and position > 0:
                slip_price = price * (1 - self.slippage)
                revenue = position * slip_price
                fee = revenue * self.commission
                pnl = revenue - fee - position * entry_price
                return_pct = (slip_price / entry_price - 1) * 100

                trades_list.append(
                    {
                        "entry_time": str(ohlcv.index[entry_idx]),
                        "exit_time": str(ohlcv.index[i]),
                        "entry_price": round(entry_price, 6),
                        "exit_price": round(slip_price, 6),
                        "size": round(position, 6),
                        "pnl": round(pnl, 2),
                        "return_pct": round(return_pct, 4),
                        "direction": "long",
                    }
                )
                cash += revenue - fee
                position = 0.0

            equity[i] = cash + position * price

        return trades_list, equity

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    async def _load_ohlcv(self, symbol: str, interval: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """
        从数据库/CSV加载K线数据。
        返回包含 open/high/low/close/volume 列的 DataFrame，index 为 datetime。
        """
        try:
            from backend.db.database import get_database

            db = await get_database()
            collection_name = f"kline_{symbol.lower().replace('/', '_')}_{interval}"
            collection = db[collection_name]

            cursor = collection.find(
                {"timestamp": {"$gte": start_date, "$lte": end_date}},
                {"_id": 0},
            ).sort("timestamp", 1)

            records = await cursor.to_list(length=None)
            if not records:
                return None

            df = pd.DataFrame(records)
            if "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df.set_index("timestamp", inplace=True)
            return df

        except Exception as e:
            logger.warning(f"从数据库加载K线失败: {e}，尝试CSV回退")
            # CSV回退
            try:
                csv_path = f"data/{symbol.lower().replace('/', '_')}_{interval}.csv"
                df = pd.read_csv(csv_path, parse_dates=["timestamp"], index_col="timestamp")
                mask = (df.index >= start_date) & (df.index <= end_date)
                return df.loc[mask]
            except Exception as e2:
                logger.error(f"CSV加载也失败: {e2}")
                return None

    def _calc_benchmark(self, ohlcv: pd.DataFrame) -> np.ndarray:
        """计算基准收益曲线（买入并持有）"""
        close = ohlcv["close"].values
        return self.initial_capital * (close / close[0])

    def _build_heatmap(self, results: List[Dict], param_x: str, param_y: str) -> Dict[str, Any]:
        """构建两参数热力图数据"""
        df = pd.DataFrame(results)
        x_vals = sorted(df[param_x].unique())
        y_vals = sorted(df[param_y].unique())

        matrix = []
        for y in y_vals:
            row = []
            for x in x_vals:
                subset = df[(df[param_x] == x) & (df[param_y] == y)]
                val = float(subset["sharpe_ratio"].values[0]) if len(subset) > 0 else 0.0
                row.append(round(val, 4))
            matrix.append(row)

        return {
            "x_param": param_x,
            "y_param": param_y,
            "x_values": [float(v) for v in x_vals],
            "y_values": [float(v) for v in y_vals],
            "z_values": matrix,
            "metric": "sharpe_ratio",
        }

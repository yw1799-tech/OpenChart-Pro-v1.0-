"""
回测报告生成模块
- generate_report: 生成完整回测报告
- 包含 summary / equity_curve / benchmark_curve / drawdown_curve / trades / monthly_returns
"""

import logging
from typing import Any, Dict, List

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def generate_report(
    trades: List[Dict],
    equity: np.ndarray,
    benchmark: np.ndarray,
    ohlcv: pd.DataFrame,
) -> Dict[str, Any]:
    """
    生成完整回测报告。

    参数:
        trades: 交易记录列表
        equity: 权益曲线数组
        benchmark: 基准收益曲线数组
        ohlcv: 原始K线 DataFrame (index=datetime)

    返回:
        包含 summary, equity_curve, benchmark_curve, drawdown_curve, trades, monthly_returns 的字典
    """
    timestamps = [str(t) for t in ohlcv.index]

    # 回撤曲线
    drawdown, max_dd, max_dd_duration = _calc_drawdown(equity)

    # 统计指标
    summary = _calc_summary(trades, equity, benchmark, ohlcv, max_dd, max_dd_duration)

    # 月度收益
    monthly = calc_monthly_returns(equity, ohlcv.index)

    return {
        "summary": summary,
        "equity_curve": {
            "timestamps": timestamps,
            "values": [round(float(v), 2) for v in equity],
        },
        "benchmark_curve": {
            "timestamps": timestamps,
            "values": [round(float(v), 2) for v in benchmark],
        },
        "drawdown_curve": {
            "timestamps": timestamps,
            "values": [round(float(v), 4) for v in drawdown],
        },
        "trades": trades,
        "monthly_returns": monthly,
    }


# ======================================================================
# 统计指标计算
# ======================================================================


def _calc_summary(
    trades: List[Dict],
    equity: np.ndarray,
    benchmark: np.ndarray,
    ohlcv: pd.DataFrame,
    max_drawdown: float,
    max_dd_duration: int,
) -> Dict[str, Any]:
    """计算所有统计指标"""
    initial_capital = equity[0] if len(equity) > 0 else 1.0
    final_equity = equity[-1] if len(equity) > 0 else initial_capital

    total_return = (final_equity / initial_capital - 1) * 100
    benchmark_return = (benchmark[-1] / benchmark[0] - 1) * 100 if len(benchmark) > 0 else 0

    # 年化收益
    n_days = len(equity)
    years = max(n_days / 252, 1 / 252)  # 至少1天
    annual_return = ((final_equity / initial_capital) ** (1 / years) - 1) * 100

    # 日收益序列
    daily_returns = np.diff(equity) / equity[:-1] if len(equity) > 1 else np.array([0.0])
    daily_returns = np.nan_to_num(daily_returns, nan=0.0, posinf=0.0, neginf=0.0)

    # Sharpe比率 (年化，假设252交易日，无风险利率3%)
    rf_daily = 0.03 / 252
    excess = daily_returns - rf_daily
    sharpe = float(np.mean(excess) / np.std(excess) * np.sqrt(252)) if np.std(excess) > 0 else 0.0

    # Sortino比率 (只用下行波动率)
    downside = daily_returns[daily_returns < 0]
    downside_std = np.std(downside) if len(downside) > 0 else 1e-10
    sortino = float(np.mean(excess) / downside_std * np.sqrt(252)) if downside_std > 0 else 0.0

    # Calmar比率 (年化收益 / 最大回撤)
    calmar = float(annual_return / (max_drawdown * 100)) if max_drawdown > 0 else 0.0

    # 交易统计
    n_trades = len(trades)
    winning = [t for t in trades if t.get("pnl", 0) > 0]
    losing = [t for t in trades if t.get("pnl", 0) <= 0]
    win_rate = len(winning) / n_trades * 100 if n_trades > 0 else 0

    total_profit = sum(t["pnl"] for t in winning) if winning else 0
    total_loss = abs(sum(t["pnl"] for t in losing)) if losing else 0
    profit_factor = total_profit / total_loss if total_loss > 0 else float("inf") if total_profit > 0 else 0

    avg_win = total_profit / len(winning) if winning else 0
    avg_loss = total_loss / len(losing) if losing else 0
    payoff_ratio = avg_win / avg_loss if avg_loss > 0 else float("inf") if avg_win > 0 else 0

    # 最大连续盈利/亏损次数
    max_consec_wins, max_consec_losses = _calc_consecutive(trades)

    # 日均波动率
    volatility = float(np.std(daily_returns) * np.sqrt(252) * 100) if len(daily_returns) > 0 else 0

    return {
        "total_return": round(total_return, 2),
        "annual_return": round(annual_return, 2),
        "benchmark_return": round(benchmark_return, 2),
        "excess_return": round(total_return - benchmark_return, 2),
        "max_drawdown": round(max_drawdown * 100, 2),
        "max_drawdown_duration": max_dd_duration,
        "sharpe_ratio": round(sharpe, 4),
        "sortino_ratio": round(sortino, 4),
        "calmar_ratio": round(calmar, 4),
        "volatility": round(volatility, 2),
        "total_trades": n_trades,
        "winning_trades": len(winning),
        "losing_trades": len(losing),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 4),
        "payoff_ratio": round(payoff_ratio, 4),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "total_profit": round(total_profit, 2),
        "total_loss": round(total_loss, 2),
        "net_profit": round(total_profit - total_loss, 2),
        "max_consecutive_wins": max_consec_wins,
        "max_consecutive_losses": max_consec_losses,
        "initial_capital": round(float(equity[0]), 2) if len(equity) > 0 else 0,
        "final_equity": round(float(final_equity), 2),
    }


# ======================================================================
# 回撤计算
# ======================================================================


def _calc_drawdown(equity: np.ndarray) -> tuple:
    """
    计算回撤曲线。

    返回:
        (drawdown_array, max_drawdown, max_drawdown_duration_in_bars)
    """
    if len(equity) == 0:
        return np.array([]), 0.0, 0

    peak = np.maximum.accumulate(equity)
    drawdown = (peak - equity) / peak
    drawdown = np.nan_to_num(drawdown, nan=0.0)
    max_dd = float(np.max(drawdown))

    # 最大回撤持续时间（从峰值到恢复的K线数）
    max_duration = 0
    current_duration = 0
    for dd in drawdown:
        if dd > 0:
            current_duration += 1
            max_duration = max(max_duration, current_duration)
        else:
            current_duration = 0

    return drawdown, max_dd, max_duration


# ======================================================================
# 月度收益矩阵
# ======================================================================


def calc_monthly_returns(
    equity: np.ndarray,
    index: pd.DatetimeIndex,
) -> Dict[str, Any]:
    """
    计算月度收益矩阵。

    返回:
        {
            "years": [2023, 2024, ...],
            "months": [1, 2, ..., 12],
            "data": [[月收益%, ...], ...],   # 行=年, 列=月
            "yearly_returns": [年收益%, ...]
        }
    """
    if len(equity) == 0 or len(index) == 0:
        return {"years": [], "months": list(range(1, 13)), "data": [], "yearly_returns": []}

    # 构建权益Series
    eq_series = pd.Series(equity, index=index)

    # 月末重采样
    monthly_eq = eq_series.resample("ME").last().dropna()
    if len(monthly_eq) < 2:
        return {"years": [], "months": list(range(1, 13)), "data": [], "yearly_returns": []}

    monthly_ret = monthly_eq.pct_change().dropna() * 100

    years = sorted(monthly_ret.index.year.unique())
    months = list(range(1, 13))

    data = []
    yearly_returns = []

    for year in years:
        row = []
        year_mask = monthly_ret.index.year == year
        year_data = monthly_ret[year_mask]

        for month in months:
            month_mask = year_data.index.month == month
            vals = year_data[month_mask]
            if len(vals) > 0:
                row.append(round(float(vals.iloc[0]), 2))
            else:
                row.append(None)
        data.append(row)

        # 年收益
        year_equity = eq_series[eq_series.index.year == year]
        if len(year_equity) >= 2:
            yr = (year_equity.iloc[-1] / year_equity.iloc[0] - 1) * 100
            yearly_returns.append(round(float(yr), 2))
        else:
            yearly_returns.append(None)

    return {
        "years": [int(y) for y in years],
        "months": months,
        "data": data,
        "yearly_returns": yearly_returns,
    }


# ======================================================================
# 辅助
# ======================================================================


def _calc_consecutive(trades: List[Dict]) -> tuple:
    """计算最大连续盈利/亏损次数"""
    if not trades:
        return 0, 0

    max_wins = 0
    max_losses = 0
    cur_wins = 0
    cur_losses = 0

    for t in trades:
        if t.get("pnl", 0) > 0:
            cur_wins += 1
            cur_losses = 0
            max_wins = max(max_wins, cur_wins)
        else:
            cur_losses += 1
            cur_wins = 0
            max_losses = max(max_losses, cur_losses)

    return max_wins, max_losses

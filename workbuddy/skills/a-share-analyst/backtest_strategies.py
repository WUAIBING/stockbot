"""Pre-built strategy configs mapping legacy backtest versions to parameterized configs.

Each legacy version becomes a config dict — one file, tracked in git, down from 11.
"""
from backtest_framework import StrategyConfig, register_strategy

# V10: latest multi-tier/multi-mode system
register_strategy(StrategyConfig(
    name="v10",
    output_prefix="backtest_t5_v10",
    top_n_amount=200,
    winner_thresh=5.0,
    loser_thresh=-3.0,
    daily_bar_count=800,
    weekly_bar_count=100,
    ma_windows=(5, 10, 20, 60),
    bollinger_window=20,
    rsi_window=14,
    roc_windows=(3, 5, 10),
))

# V9: same as v10 but smaller universe
register_strategy(StrategyConfig(
    name="v9",
    output_prefix="backtest_t5_v9",
    top_n_amount=300,
    winner_thresh=5.0,
    loser_thresh=-3.0,
))

# V8: relaxed thresholds
register_strategy(StrategyConfig(
    name="v8",
    output_prefix="backtest_t5_v8",
    top_n_amount=200,
    winner_thresh=4.0,
    loser_thresh=-3.5,
    ma_windows=(5, 10, 20, 60, 120),
))

# V7: focused universe, tighter thresholds
register_strategy(StrategyConfig(
    name="v7",
    output_prefix="backtest_t5_v7",
    top_n_amount=150,
    winner_thresh=6.0,
    loser_thresh=-2.5,
    bollinger_window=20,
    bollinger_std_mult=2.5,
))

# V6: CSI1000 + Amount Top 500, flow pressure
register_strategy(StrategyConfig(
    name="v6",
    output_prefix="backtest_t5_v6",
    top_n_amount=500,
    winner_thresh=5.0,
    loser_thresh=-3.0,
    ma_windows=(5, 10, 20),
))

# V5: lean signals
register_strategy(StrategyConfig(
    name="v5",
    output_prefix="backtest_t5_v5",
    top_n_amount=200,
    winner_thresh=4.5,
    loser_thresh=-3.0,
    roc_windows=(3, 5),
))

# V4: baseline
register_strategy(StrategyConfig(
    name="v4",
    output_prefix="backtest_t5_v4",
    top_n_amount=200,
    winner_thresh=5.0,
    loser_thresh=-3.0,
))

# V3: original T5 pattern learner config
register_strategy(StrategyConfig(
    name="v3",
    output_prefix="backtest_t5_v3",
    top_n_amount=300,
    winner_thresh=5.0,
    loser_thresh=-3.0,
    daily_bar_count=600,
    weekly_bar_count=80,
    ma_windows=(5, 10, 20),
))

# Convenience: run all with one import
ALL_STRATEGIES = ["v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10"]

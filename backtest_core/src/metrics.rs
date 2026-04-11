use crate::types::{Metrics, TradeRecord};

/// Compute all metrics from completed trades and equity curve.
pub fn compute_metrics(
    trades: &[TradeRecord],
    equity_curve: &[(f64, f64)],
    _initial_capital: f64,
    signals_expired: u32,
    signals_cancelled: u32,
) -> Metrics {
    if trades.is_empty() {
        return Metrics {
            signals_expired,
            signals_cancelled,
            ..Metrics::default()
        };
    }

    let total = trades.len() as u32;
    let winners: Vec<&TradeRecord> = trades.iter().filter(|t| t.realized_pnl > 0.0).collect();
    let losers: Vec<&TradeRecord> = trades.iter().filter(|t| t.realized_pnl <= 0.0).collect();

    let win_count = winners.len() as u32;
    let loss_count = losers.len() as u32;

    let win_rate = win_count as f64 / total as f64 * 100.0;

    let total_pnl: f64 = trades.iter().map(|t| t.realized_pnl).sum();
    let total_pnl_pct: f64 = trades.iter().map(|t| t.realized_pnl_pct).sum();

    let gross_profit: f64 = winners.iter().map(|t| t.realized_pnl).sum();
    let gross_loss: f64 = losers.iter().map(|t| t.realized_pnl).sum::<f64>().abs();

    let profit_factor = if gross_loss > 0.001 {
        gross_profit / gross_loss
    } else if gross_profit > 0.0 {
        f64::INFINITY
    } else {
        0.0
    };

    let avg_win_pct = if !winners.is_empty() {
        winners.iter().map(|t| t.realized_pnl_pct).sum::<f64>() / winners.len() as f64
    } else {
        0.0
    };

    let avg_loss_pct = if !losers.is_empty() {
        losers.iter().map(|t| t.realized_pnl_pct).sum::<f64>() / losers.len() as f64
    } else {
        0.0
    };

    let wr = win_count as f64 / total as f64;
    let avg_win = if !winners.is_empty() {
        winners.iter().map(|t| t.realized_pnl).sum::<f64>() / winners.len() as f64
    } else {
        0.0
    };
    let avg_loss = if !losers.is_empty() {
        losers.iter().map(|t| t.realized_pnl).sum::<f64>() / losers.len() as f64
    } else {
        0.0
    };
    let expectancy = (wr * avg_win) + ((1.0 - wr) * avg_loss);

    // Max drawdown from equity curve
    let max_drawdown_pct = calc_max_drawdown(equity_curve);

    // Sharpe & Sortino
    let returns: Vec<f64> = trades.iter().map(|t| t.realized_pnl_pct).collect();
    let sharpe = calc_sharpe(&returns);
    let sortino = calc_sortino(&returns);

    // TP hit rates
    let tp1_hit_rate = trades.iter().filter(|t| t.tp1_hit).count() as f64 / total as f64 * 100.0;
    let tp2_hit_rate = trades.iter().filter(|t| t.tp2_hit).count() as f64 / total as f64 * 100.0;
    let tp3_hit_rate = trades.iter().filter(|t| t.tp3_hit).count() as f64 / total as f64 * 100.0;

    let total_funding_paid: f64 = trades.iter().map(|t| t.funding_paid).sum();
    let total_fees_paid: f64 = trades.iter().map(|t| t.fees_paid).sum();

    // Entry improvement (only for trades with signal_price > 0)
    let trailing_trades: Vec<&TradeRecord> = trades.iter()
        .filter(|t| t.signal_price > 0.0)
        .collect();
    let avg_entry_improvement_pct = if !trailing_trades.is_empty() {
        trailing_trades.iter().map(|t| t.entry_improvement_pct).sum::<f64>()
            / trailing_trades.len() as f64
    } else {
        0.0
    };

    Metrics {
        total_trades: total,
        winners: win_count,
        losers: loss_count,
        win_rate,
        total_pnl,
        total_pnl_pct,
        avg_win_pct,
        avg_loss_pct,
        profit_factor,
        max_drawdown_pct,
        sharpe,
        sortino,
        expectancy,
        tp1_hit_rate,
        tp2_hit_rate,
        tp3_hit_rate,
        total_funding_paid,
        total_fees_paid,
        avg_entry_improvement_pct,
        signals_expired,
        signals_cancelled,
    }
}

fn calc_max_drawdown(equity_curve: &[(f64, f64)]) -> f64 {
    if equity_curve.is_empty() {
        return 0.0;
    }

    let mut peak = equity_curve[0].1;
    let mut max_dd = 0.0_f64;

    for &(_, equity) in equity_curve {
        if equity > peak {
            peak = equity;
        }
        if peak > 0.0 {
            let dd = (peak - equity) / peak * 100.0;
            if dd > max_dd {
                max_dd = dd;
            }
        }
    }

    max_dd
}

fn calc_sharpe(returns: &[f64]) -> f64 {
    if returns.len() < 2 {
        return 0.0;
    }

    let n = returns.len() as f64;
    let mean = returns.iter().sum::<f64>() / n;
    let variance = returns.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / n;
    let std = variance.sqrt();

    if std > 0.0 { mean / std } else { 0.0 }
}

fn calc_sortino(returns: &[f64]) -> f64 {
    if returns.len() < 2 {
        return 0.0;
    }

    let n = returns.len() as f64;
    let mean = returns.iter().sum::<f64>() / n;

    // Downside deviation (only negative returns)
    let downside_sq: f64 = returns.iter()
        .filter(|&&r| r < 0.0)
        .map(|r| r.powi(2))
        .sum();
    let downside_dev = (downside_sq / n).sqrt();

    if downside_dev > 0.0 { mean / downside_dev } else { 0.0 }
}

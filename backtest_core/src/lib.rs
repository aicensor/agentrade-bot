use pyo3::prelude::*;
use pyo3::types::PyDict;

mod types;
mod strategy;
mod sim_exchange;
mod engine;
mod metrics;

use engine::BacktestEngine;
use types::{BacktestConfig, BacktestResult};

/// Python module — `import backtest_core`
#[pymodule]
mod backtest_core {
    use super::*;

    /// Run a full backtest on tick data.
    ///
    /// Args:
    ///     timestamps: list[float] — unix timestamps (seconds)
    ///     prices: list[float] — tick prices
    ///     config_json: str — JSON config string
    ///
    /// Returns:
    ///     dict with metrics, trades, equity_curve
    #[pyfunction]
    fn run_backtest(
        timestamps: Vec<f64>,
        prices: Vec<f64>,
        config_json: &str,
    ) -> PyResult<PyObject> {
        let config: BacktestConfig = serde_json::from_str(config_json)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid config: {e}")))?;

        let mut engine = BacktestEngine::new(config);
        let result = engine.run(&timestamps, &prices);

        Python::with_gil(|py| result_to_pydict(py, &result))
    }

    /// Run multiple backtests with different configs (for Optuna).
    /// Returns list of metric dicts.
    #[pyfunction]
    fn run_batch(
        timestamps: Vec<f64>,
        prices: Vec<f64>,
        configs_json: Vec<String>,
    ) -> PyResult<Vec<PyObject>> {
        let mut results = Vec::with_capacity(configs_json.len());

        for cfg_json in &configs_json {
            let config: BacktestConfig = serde_json::from_str(cfg_json)
                .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid config: {e}")))?;

            let mut engine = BacktestEngine::new(config);
            let result = engine.run(&timestamps, &prices);

            Python::with_gil(|py| {
                results.push(result_to_pydict(py, &result)?);
                Ok::<(), PyErr>(())
            })?;
        }

        Ok(results)
    }

    /// Quick metric-only run (no trade details). Fastest path for Optuna.
    #[pyfunction]
    fn run_metrics_only(
        timestamps: Vec<f64>,
        prices: Vec<f64>,
        config_json: &str,
    ) -> PyResult<PyObject> {
        let config: BacktestConfig = serde_json::from_str(config_json)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Invalid config: {e}")))?;

        let mut engine = BacktestEngine::new(config);
        let result = engine.run(&timestamps, &prices);
        let m = &result.metrics;

        Python::with_gil(|py| {
            let dict = PyDict::new(py);
            dict.set_item("total_trades", m.total_trades)?;
            dict.set_item("winners", m.winners)?;
            dict.set_item("losers", m.losers)?;
            dict.set_item("win_rate", m.win_rate)?;
            dict.set_item("total_pnl", m.total_pnl)?;
            dict.set_item("total_pnl_pct", m.total_pnl_pct)?;
            dict.set_item("profit_factor", m.profit_factor)?;
            dict.set_item("max_drawdown_pct", m.max_drawdown_pct)?;
            dict.set_item("sharpe", m.sharpe)?;
            dict.set_item("sortino", m.sortino)?;
            dict.set_item("expectancy", m.expectancy)?;
            dict.set_item("avg_win_pct", m.avg_win_pct)?;
            dict.set_item("avg_loss_pct", m.avg_loss_pct)?;
            dict.set_item("total_funding_paid", m.total_funding_paid)?;
            dict.set_item("total_fees_paid", m.total_fees_paid)?;
            dict.set_item("avg_entry_improvement_pct", m.avg_entry_improvement_pct)?;
            dict.set_item("signals_expired", m.signals_expired)?;
            dict.set_item("signals_cancelled", m.signals_cancelled)?;
            Ok(dict.into())
        })
    }
}

fn result_to_pydict(py: Python<'_>, result: &BacktestResult) -> PyResult<PyObject> {
    let dict = PyDict::new(py);
    let m = &result.metrics;

    // Metrics
    let metrics = PyDict::new(py);
    metrics.set_item("total_trades", m.total_trades)?;
    metrics.set_item("winners", m.winners)?;
    metrics.set_item("losers", m.losers)?;
    metrics.set_item("win_rate", m.win_rate)?;
    metrics.set_item("total_pnl", m.total_pnl)?;
    metrics.set_item("total_pnl_pct", m.total_pnl_pct)?;
    metrics.set_item("profit_factor", m.profit_factor)?;
    metrics.set_item("max_drawdown_pct", m.max_drawdown_pct)?;
    metrics.set_item("sharpe", m.sharpe)?;
    metrics.set_item("sortino", m.sortino)?;
    metrics.set_item("expectancy", m.expectancy)?;
    metrics.set_item("avg_win_pct", m.avg_win_pct)?;
    metrics.set_item("avg_loss_pct", m.avg_loss_pct)?;
    metrics.set_item("total_funding_paid", m.total_funding_paid)?;
    metrics.set_item("total_fees_paid", m.total_fees_paid)?;
    metrics.set_item("tp1_hit_rate", m.tp1_hit_rate)?;
    metrics.set_item("tp2_hit_rate", m.tp2_hit_rate)?;
    metrics.set_item("tp3_hit_rate", m.tp3_hit_rate)?;
    metrics.set_item("avg_entry_improvement_pct", m.avg_entry_improvement_pct)?;
    metrics.set_item("signals_expired", m.signals_expired)?;
    metrics.set_item("signals_cancelled", m.signals_cancelled)?;
    dict.set_item("metrics", metrics)?;

    // Trades
    let trades: Vec<PyObject> = result.trades.iter().map(|t| {
        let td = PyDict::new(py);
        td.set_item("trade_id", t.trade_id).unwrap();
        td.set_item("side", if t.is_long { "long" } else { "short" }).unwrap();
        td.set_item("entry_price", t.entry_price).unwrap();
        td.set_item("exit_price", t.exit_price).unwrap();
        td.set_item("entry_time", t.entry_time).unwrap();
        td.set_item("exit_time", t.exit_time).unwrap();
        td.set_item("exit_reason", &t.exit_reason).unwrap();
        td.set_item("realized_pnl", t.realized_pnl).unwrap();
        td.set_item("realized_pnl_pct", t.realized_pnl_pct).unwrap();
        td.set_item("initial_size", t.initial_size).unwrap();
        td.set_item("tp1_hit", t.tp1_hit).unwrap();
        td.set_item("tp2_hit", t.tp2_hit).unwrap();
        td.set_item("tp3_hit", t.tp3_hit).unwrap();
        td.set_item("max_favorable", t.max_favorable).unwrap();
        td.set_item("max_adverse", t.max_adverse).unwrap();
        td.set_item("funding_paid", t.funding_paid).unwrap();
        td.set_item("fees_paid", t.fees_paid).unwrap();
        td.set_item("signal_price", t.signal_price).unwrap();
        td.set_item("entry_improvement_pct", t.entry_improvement_pct).unwrap();
        td.into_any().unbind()
    }).collect();
    dict.set_item("trades", trades)?;

    // Equity curve (sampled)
    let eq: Vec<(f64, f64)> = result.equity_curve.clone();
    dict.set_item("equity_curve", eq)?;

    dict.set_item("tick_count", result.tick_count)?;
    dict.set_item("execution_time_ms", result.execution_time_ms)?;

    Ok(dict.into())
}

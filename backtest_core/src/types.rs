use serde::{Deserialize, Serialize};

// ─── Config ─────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Deserialize)]
pub struct BacktestConfig {
    pub strategy: StrategyConfig,

    #[serde(default = "default_side")]
    pub side: String,           // "long" or "short"

    #[serde(default = "default_size")]
    pub position_size: f64,

    #[serde(default = "default_leverage")]
    pub leverage: f64,

    #[serde(default = "default_capital")]
    pub initial_capital: f64,

    #[serde(default = "default_slippage")]
    pub slippage_pct: f64,

    #[serde(default = "default_fee")]
    pub fee_pct: f64,           // per side (maker/taker)

    #[serde(default = "default_entry_mode")]
    pub entry_mode: String,     // "single", "every_n"

    #[serde(default = "default_entry_interval")]
    pub entry_interval: usize,  // for "every_n" mode

    // Trailing entry
    #[serde(default)]
    pub trailing_entry: TrailingEntryConfig,

    // Funding rate simulation
    #[serde(default = "default_funding_rate")]
    pub funding_rate_pct: f64,  // per 8h interval (e.g., 0.01 = 0.01%)

    #[serde(default = "default_funding_interval")]
    pub funding_interval_sec: f64,  // 28800 = 8 hours

    // Liquidation
    #[serde(default)]
    pub enable_liquidation: bool,

    // Equity curve sampling
    #[serde(default = "default_equity_sample")]
    pub equity_sample_interval: usize,  // record every N ticks
}

// ─── Trailing Entry Config ──────────────────────────────────────────────────

#[derive(Debug, Clone, Deserialize)]
pub struct TrailingEntryConfig {
    /// Enable trailing entry (false = immediate entry like before)
    #[serde(default)]
    pub enabled: bool,

    /// Fixed rebound % to trigger entry.
    /// For longs: price must rebound UP from lowest by this %.
    /// For shorts: price must rebound DOWN from highest by this %.
    #[serde(default = "default_entry_trail_pct")]
    pub trail_pct: f64,

    /// Use ATR-based dynamic trail instead of fixed %.
    /// trail_distance = ATR(atr_period) * atr_multiplier
    #[serde(default)]
    pub atr_enabled: bool,

    #[serde(default = "default_atr_period")]
    pub atr_period: usize,

    #[serde(default = "default_atr_multiplier")]
    pub atr_multiplier: f64,

    /// Maximum time (in seconds) to wait for entry after signal.
    /// 0 = no timeout.
    #[serde(default = "default_entry_timeout")]
    pub timeout_sec: f64,

    /// Maximum deviation from signal price before cancelling.
    /// If price moves AGAINST signal by more than this %, cancel.
    /// 0 = no limit.
    #[serde(default)]
    pub max_adverse_pct: f64,
}

impl Default for TrailingEntryConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            trail_pct: 2.0,
            atr_enabled: false,
            atr_period: 14,
            atr_multiplier: 1.5,
            timeout_sec: 0.0,
            max_adverse_pct: 0.0,
        }
    }
}

fn default_entry_trail_pct() -> f64 { 2.0 }
fn default_atr_period() -> usize { 14 }
fn default_atr_multiplier() -> f64 { 1.5 }
fn default_entry_timeout() -> f64 { 0.0 }

fn default_side() -> String { "short".to_string() }
fn default_size() -> f64 { 1.0 }
fn default_leverage() -> f64 { 5.0 }
fn default_capital() -> f64 { 1000.0 }
fn default_slippage() -> f64 { 0.05 }
fn default_fee() -> f64 { 0.035 }
fn default_entry_mode() -> String { "single".to_string() }
fn default_entry_interval() -> usize { 100 }
fn default_funding_rate() -> f64 { 0.01 }
fn default_funding_interval() -> f64 { 28800.0 }
fn default_equity_sample() -> usize { 100 }

#[derive(Debug, Clone, Deserialize)]
pub struct StrategyConfig {
    #[serde(default = "default_tp1")]
    pub tp1_pct: f64,
    #[serde(default = "default_tp2")]
    pub tp2_pct: f64,
    #[serde(default = "default_tp3")]
    pub tp3_pct: f64,

    #[serde(default = "default_close_pct")]
    pub tp1_close_pct: f64,
    #[serde(default = "default_close_pct")]
    pub tp2_close_pct: f64,
    #[serde(default)]
    pub tp3_close_pct: f64,

    #[serde(default = "default_trail")]
    pub trail_pct: f64,

    #[serde(default = "default_min_sl_change")]
    pub min_sl_change_pct: f64,
}

fn default_tp1() -> f64 { 3.0 }
fn default_tp2() -> f64 { 5.0 }
fn default_tp3() -> f64 { 8.0 }
fn default_close_pct() -> f64 { 0.33 }
fn default_trail() -> f64 { 1.5 }
fn default_min_sl_change() -> f64 { 0.1 }

// ─── Position State ─────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct Position {
    pub is_long: bool,
    pub entry_price: f64,
    pub size: f64,
    pub initial_size: f64,
    pub leverage: f64,

    pub current_sl: f64,
    pub last_sl_update_price: f64,

    pub tp1_hit: bool,
    pub tp2_hit: bool,
    pub tp3_hit: bool,

    pub highest_since_entry: f64,
    pub lowest_since_entry: f64,

    pub entry_time: f64,
    pub trade_id: u32,

    // Funding tracking
    pub last_funding_time: f64,
    pub cumulative_funding: f64,
    pub total_fees: f64,

    // Realized PnL from partial closes
    pub partial_pnl: f64,

    // MFE / MAE
    pub max_favorable: f64,
    pub max_adverse: f64,

    // Trailing entry tracking
    pub signal_price: f64,
}

impl Position {
    pub fn new(
        is_long: bool,
        entry_price: f64,
        size: f64,
        leverage: f64,
        entry_time: f64,
        trade_id: u32,
    ) -> Self {
        Self {
            is_long,
            entry_price,
            size,
            initial_size: size,
            leverage,
            current_sl: 0.0,
            last_sl_update_price: 0.0,
            tp1_hit: false,
            tp2_hit: false,
            tp3_hit: false,
            highest_since_entry: entry_price,
            lowest_since_entry: entry_price,
            entry_time,
            trade_id,
            last_funding_time: entry_time,
            cumulative_funding: 0.0,
            total_fees: 0.0,
            partial_pnl: 0.0,
            max_favorable: 0.0,
            max_adverse: 0.0,
            signal_price: 0.0,
        }
    }

    #[inline(always)]
    pub fn update_extremes(&mut self, price: f64) {
        if price > self.highest_since_entry {
            self.highest_since_entry = price;
        }
        if price < self.lowest_since_entry {
            self.lowest_since_entry = price;
        }
    }

    #[inline(always)]
    pub fn pnl_pct(&self, price: f64) -> f64 {
        if self.is_long {
            (price - self.entry_price) / self.entry_price * 100.0
        } else {
            (self.entry_price - price) / self.entry_price * 100.0
        }
    }

    #[inline(always)]
    pub fn pnl_usd(&self, exit_price: f64, size: f64) -> f64 {
        if self.is_long {
            (exit_price - self.entry_price) * size
        } else {
            (self.entry_price - exit_price) * size
        }
    }

    /// Margin used for this position
    #[inline(always)]
    pub fn margin(&self) -> f64 {
        self.entry_price * self.size / self.leverage
    }

    /// Liquidation price (simplified)
    pub fn liquidation_price(&self) -> f64 {
        let margin = self.margin();
        if self.is_long {
            self.entry_price - margin / self.size
        } else {
            self.entry_price + margin / self.size
        }
    }
}

// ─── Trailing Entry State ────────────────────────────────────────────────────

/// State machine for a pending trailing entry signal.
#[derive(Debug, Clone)]
pub struct EntrySignal {
    pub is_long: bool,
    pub signal_price: f64,
    pub signal_time: f64,
    /// For longs: lowest price since signal. For shorts: highest since signal.
    pub extreme_since_signal: f64,
    /// Current ATR value at signal time (if ATR mode enabled)
    pub atr_at_signal: f64,
}

impl EntrySignal {
    pub fn new(is_long: bool, price: f64, ts: f64, atr: f64) -> Self {
        Self {
            is_long,
            signal_price: price,
            signal_time: ts,
            extreme_since_signal: price,
            atr_at_signal: atr,
        }
    }

    /// Update the extreme price tracked since signal.
    #[inline(always)]
    pub fn update_extreme(&mut self, price: f64) {
        if self.is_long {
            // Track lowest price (waiting for dip then rebound)
            if price < self.extreme_since_signal {
                self.extreme_since_signal = price;
            }
        } else {
            // Track highest price (waiting for spike then pullback)
            if price > self.extreme_since_signal {
                self.extreme_since_signal = price;
            }
        }
    }

    /// Calculate the rebound % from the extreme.
    #[inline(always)]
    pub fn rebound_pct(&self, price: f64) -> f64 {
        if self.extreme_since_signal <= 0.0 {
            return 0.0;
        }
        if self.is_long {
            // Rebound UP from lowest
            (price - self.extreme_since_signal) / self.extreme_since_signal * 100.0
        } else {
            // Rebound DOWN from highest
            (self.extreme_since_signal - price) / self.extreme_since_signal * 100.0
        }
    }

    /// How far price has moved against the signal direction.
    #[inline(always)]
    pub fn adverse_pct(&self, price: f64) -> f64 {
        if self.signal_price <= 0.0 {
            return 0.0;
        }
        if self.is_long {
            // For longs, adverse = price went UP from signal (missed the move)
            ((price - self.signal_price) / self.signal_price * 100.0).max(0.0)
        } else {
            // For shorts, adverse = price went DOWN from signal
            ((self.signal_price - price) / self.signal_price * 100.0).max(0.0)
        }
    }
}

// ─── Strategy Action ────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub enum Action {
    MoveSL { price: f64, reason: &'static str },
    PartialClose { close_pct: f64, new_sl: f64, reason: &'static str },
    Alert { message: &'static str },
    None,
}

// ─── Results ────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize)]
pub struct TradeRecord {
    pub trade_id: u32,
    pub is_long: bool,
    pub entry_price: f64,
    pub exit_price: f64,
    pub entry_time: f64,
    pub exit_time: f64,
    pub exit_reason: String,
    pub initial_size: f64,
    pub realized_pnl: f64,
    pub realized_pnl_pct: f64,
    pub tp1_hit: bool,
    pub tp2_hit: bool,
    pub tp3_hit: bool,
    pub max_favorable: f64,
    pub max_adverse: f64,
    pub funding_paid: f64,
    pub fees_paid: f64,
    /// Signal price (before trailing entry). 0 if trailing entry disabled.
    pub signal_price: f64,
    /// Entry improvement vs signal price (%).
    /// Positive = entered at better price than signal.
    pub entry_improvement_pct: f64,
}

#[derive(Debug, Clone)]
pub struct BacktestResult {
    pub metrics: Metrics,
    pub trades: Vec<TradeRecord>,
    pub equity_curve: Vec<(f64, f64)>,
    pub tick_count: u64,
    pub execution_time_ms: f64,
}

#[derive(Debug, Clone, Default)]
pub struct Metrics {
    pub total_trades: u32,
    pub winners: u32,
    pub losers: u32,
    pub win_rate: f64,
    pub total_pnl: f64,
    pub total_pnl_pct: f64,
    pub avg_win_pct: f64,
    pub avg_loss_pct: f64,
    pub profit_factor: f64,
    pub max_drawdown_pct: f64,
    pub sharpe: f64,
    pub sortino: f64,
    pub expectancy: f64,
    pub tp1_hit_rate: f64,
    pub tp2_hit_rate: f64,
    pub tp3_hit_rate: f64,
    pub total_funding_paid: f64,
    pub total_fees_paid: f64,
    /// Average entry improvement from trailing entry (%).
    pub avg_entry_improvement_pct: f64,
    /// Number of signals that timed out without entry.
    pub signals_expired: u32,
    /// Number of signals that were cancelled (adverse move).
    pub signals_cancelled: u32,
}

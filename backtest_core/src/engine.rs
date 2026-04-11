use std::time::Instant;

use crate::metrics::compute_metrics;
use crate::sim_exchange::{apply_slippage, calc_fee, calc_funding_payment, check_liquidation, check_sl_hit};
use crate::strategy::evaluate;
use crate::types::{Action, BacktestConfig, BacktestResult, EntrySignal, Position, TradeRecord};

pub struct BacktestEngine {
    config: BacktestConfig,
    capital: f64,
    position: Option<Position>,
    trades: Vec<TradeRecord>,
    equity_curve: Vec<(f64, f64)>,
    trade_counter: u32,
    entry_cooldown: usize,

    // Trailing entry state
    pending_signal: Option<EntrySignal>,
    signals_expired: u32,
    signals_cancelled: u32,

    // ATR calculation state (rolling window)
    atr_highs: Vec<f64>,
    atr_lows: Vec<f64>,
    atr_closes: Vec<f64>,
    current_atr: f64,
    /// Track candle boundaries for ATR from tick data.
    /// We group ticks into synthetic candles (4 ticks = 1 candle from OHLC expansion).
    atr_tick_count: usize,
    atr_candle_high: f64,
    atr_candle_low: f64,
    atr_candle_close: f64,
    atr_prev_close: f64,
}

impl BacktestEngine {
    pub fn new(config: BacktestConfig) -> Self {
        let capital = config.initial_capital;
        let atr_period = config.trailing_entry.atr_period;
        Self {
            config,
            capital,
            position: None,
            trades: Vec::new(),
            equity_curve: Vec::new(),
            trade_counter: 0,
            entry_cooldown: 0,
            pending_signal: None,
            signals_expired: 0,
            signals_cancelled: 0,
            atr_highs: Vec::with_capacity(atr_period + 1),
            atr_lows: Vec::with_capacity(atr_period + 1),
            atr_closes: Vec::with_capacity(atr_period + 1),
            current_atr: 0.0,
            atr_tick_count: 0,
            atr_candle_high: 0.0,
            atr_candle_low: f64::MAX,
            atr_candle_close: 0.0,
            atr_prev_close: 0.0,
        }
    }

    pub fn run(&mut self, timestamps: &[f64], prices: &[f64]) -> BacktestResult {
        let start = Instant::now();
        let tick_count = timestamps.len().min(prices.len()) as u64;
        let trailing_enabled = self.config.trailing_entry.enabled;

        for i in 0..tick_count as usize {
            let ts = timestamps[i];
            let price = prices[i];

            // ── Update ATR from tick data ────────────────────────────────
            if trailing_enabled && self.config.trailing_entry.atr_enabled {
                self.update_atr_tick(price);
            }

            // ── Update position state ────────────────────────────────────
            if let Some(ref mut pos) = self.position {
                pos.update_extremes(price);

                // MFE / MAE
                let pnl_pct = pos.pnl_pct(price);
                if pnl_pct > pos.max_favorable {
                    pos.max_favorable = pnl_pct;
                }
                if pnl_pct < pos.max_adverse {
                    pos.max_adverse = pnl_pct;
                }

                // ── Funding payments ─────────────────────────────────────
                if self.config.funding_rate_pct != 0.0
                    && self.config.funding_interval_sec > 0.0
                    && ts - pos.last_funding_time >= self.config.funding_interval_sec
                {
                    let payment = calc_funding_payment(pos, self.config.funding_rate_pct, price);
                    pos.cumulative_funding += payment;
                    self.capital -= payment;
                    pos.last_funding_time = ts;
                }

                // ── Liquidation check ────────────────────────────────────
                if self.config.enable_liquidation && check_liquidation(pos, price) {
                    self.close_position(price, ts, "liquidation");
                    continue;
                }

                // ── Stop-loss check ──────────────────────────────────────
                if check_sl_hit(pos, price) {
                    let exit_price = apply_slippage(
                        pos.current_sl,
                        self.config.slippage_pct,
                        !pos.is_long,
                    );
                    self.close_position(exit_price, ts, "stop_loss");
                    continue;
                }

                // ── Strategy evaluation (trailing exit) ──────────────────
                let action = evaluate(pos, price, &self.config.strategy);
                match action {
                    Action::MoveSL { price: sl, reason } => {
                        let pos = self.position.as_mut().unwrap();
                        pos.current_sl = sl;
                        pos.last_sl_update_price = sl;
                        match reason {
                            "tp1_hit" => pos.tp1_hit = true,
                            "tp2_hit" => pos.tp2_hit = true,
                            "tp3_hit" => pos.tp3_hit = true,
                            _ => {}
                        }
                    }
                    Action::PartialClose {
                        close_pct,
                        new_sl,
                        reason,
                    } => {
                        self.partial_close(price, ts, close_pct, new_sl, reason);
                    }
                    Action::Alert { .. } | Action::None => {}
                }
            } else if trailing_enabled {
                // ── Trailing entry logic ─────────────────────────────────
                self.process_trailing_entry(price, ts);
            } else {
                // ── Immediate entry (legacy) ─────────────────────────────
                if self.entry_cooldown > 0 {
                    self.entry_cooldown -= 1;
                } else {
                    self.open_position(price, ts, 0.0);
                    if self.config.entry_mode == "every_n" {
                        self.entry_cooldown = self.config.entry_interval;
                    }
                }
            }

            // ── Equity curve sampling ────────────────────────────────────
            if self.config.equity_sample_interval > 0
                && i % self.config.equity_sample_interval == 0
            {
                let equity = self.current_equity(price);
                self.equity_curve.push((ts, equity));
            }
        }

        // Force-close any open position at end
        if self.position.is_some() {
            let last_price = prices[tick_count as usize - 1];
            let last_ts = timestamps[tick_count as usize - 1];
            self.close_position(last_price, last_ts, "end_of_data");
        }

        let metrics = compute_metrics(
            &self.trades,
            &self.equity_curve,
            self.config.initial_capital,
            self.signals_expired,
            self.signals_cancelled,
        );

        BacktestResult {
            metrics,
            trades: self.trades.clone(),
            equity_curve: self.equity_curve.clone(),
            tick_count,
            execution_time_ms: start.elapsed().as_secs_f64() * 1000.0,
        }
    }

    // ─── Trailing Entry State Machine ────────────────────────────────────────

    fn process_trailing_entry(&mut self, price: f64, ts: f64) {
        let cfg = &self.config.trailing_entry;

        if let Some(ref mut signal) = self.pending_signal {
            // Update extreme price tracking
            signal.update_extreme(price);

            // ── Check timeout ────────────────────────────────────────
            if cfg.timeout_sec > 0.0 && (ts - signal.signal_time) >= cfg.timeout_sec {
                self.signals_expired += 1;
                self.pending_signal = None;
                // Set cooldown before next signal
                if self.config.entry_mode == "every_n" {
                    self.entry_cooldown = self.config.entry_interval;
                }
                return;
            }

            // ── Check max adverse move (cancel signal) ───────────────
            if cfg.max_adverse_pct > 0.0 {
                let adverse = signal.adverse_pct(price);
                if adverse > cfg.max_adverse_pct {
                    self.signals_cancelled += 1;
                    self.pending_signal = None;
                    if self.config.entry_mode == "every_n" {
                        self.entry_cooldown = self.config.entry_interval;
                    }
                    return;
                }
            }

            // ── Check rebound trigger ────────────────────────────────
            let rebound = signal.rebound_pct(price);

            // Determine trail distance: ATR-based or fixed %
            let trail_distance = if cfg.atr_enabled && signal.atr_at_signal > 0.0 {
                // ATR-based: convert ATR absolute value to % of extreme price
                let atr_distance = signal.atr_at_signal * cfg.atr_multiplier;
                atr_distance / signal.extreme_since_signal * 100.0
            } else {
                cfg.trail_pct
            };

            if rebound >= trail_distance {
                // Trailing entry triggered — enter at current price
                let signal_price = signal.signal_price;
                self.pending_signal = None;
                self.open_position(price, ts, signal_price);
                if self.config.entry_mode == "every_n" {
                    self.entry_cooldown = self.config.entry_interval;
                }
            }
        } else {
            // No pending signal — generate one based on entry mode
            if self.entry_cooldown > 0 {
                self.entry_cooldown -= 1;
            } else {
                // Create a new entry signal
                let is_long = self.config.side == "long";
                let atr = if cfg.atr_enabled { self.current_atr } else { 0.0 };
                self.pending_signal = Some(EntrySignal::new(is_long, price, ts, atr));
            }
        }
    }

    // ─── ATR Calculation ─────────────────────────────────────────────────────

    /// Build synthetic candles from ticks and compute ATR.
    /// Groups every 4 ticks as one candle (matching OHLC tick expansion).
    fn update_atr_tick(&mut self, price: f64) {
        self.atr_tick_count += 1;

        // Track candle high/low/close
        if price > self.atr_candle_high {
            self.atr_candle_high = price;
        }
        if price < self.atr_candle_low {
            self.atr_candle_low = price;
        }
        self.atr_candle_close = price;

        // Every 4 ticks = 1 synthetic candle (OHLC expansion)
        if self.atr_tick_count % 4 == 0 {
            let high = self.atr_candle_high;
            let low = self.atr_candle_low;
            let close = self.atr_candle_close;

            self.atr_highs.push(high);
            self.atr_lows.push(low);
            self.atr_closes.push(close);

            // Compute ATR when we have enough candles
            let period = self.config.trailing_entry.atr_period;
            if self.atr_closes.len() >= period + 1 {
                self.current_atr = self.calc_atr(period);

                // Keep only what we need (sliding window)
                if self.atr_closes.len() > period * 2 {
                    let drain = self.atr_closes.len() - period - 1;
                    self.atr_highs.drain(..drain);
                    self.atr_lows.drain(..drain);
                    self.atr_closes.drain(..drain);
                }
            }

            self.atr_prev_close = close;

            // Reset candle tracking
            self.atr_candle_high = 0.0;
            self.atr_candle_low = f64::MAX;
        }
    }

    /// Calculate ATR from stored candle data.
    fn calc_atr(&self, period: usize) -> f64 {
        let n = self.atr_closes.len();
        if n < period + 1 {
            return 0.0;
        }

        let mut sum_tr = 0.0;
        for i in (n - period)..n {
            let high = self.atr_highs[i];
            let low = self.atr_lows[i];
            let prev_close = self.atr_closes[i - 1];

            // True Range = max(H-L, |H-prevC|, |L-prevC|)
            let tr = (high - low)
                .max((high - prev_close).abs())
                .max((low - prev_close).abs());
            sum_tr += tr;
        }

        sum_tr / period as f64
    }

    // ─── Position Management ─────────────────────────────────────────────────

    fn open_position(&mut self, price: f64, ts: f64, signal_price: f64) {
        let is_long = self.config.side == "long";
        let fill_price = apply_slippage(price, self.config.slippage_pct, is_long);

        let size = self.config.position_size;
        let fee = calc_fee(fill_price, size, self.config.fee_pct);
        self.capital -= fee;

        self.trade_counter += 1;
        let mut pos = Position::new(
            is_long,
            fill_price,
            size,
            self.config.leverage,
            ts,
            self.trade_counter,
        );
        pos.total_fees = fee;

        // Store signal price for entry improvement tracking
        if signal_price > 0.0 {
            pos.signal_price = signal_price;
        }

        self.position = Some(pos);
    }

    fn close_position(&mut self, exit_price: f64, ts: f64, reason: &str) {
        if let Some(pos) = self.position.take() {
            let fill_price = apply_slippage(
                exit_price,
                self.config.slippage_pct,
                !pos.is_long,
            );

            let exit_fee = calc_fee(fill_price, pos.size, self.config.fee_pct);
            let raw_pnl = pos.pnl_usd(fill_price, pos.size) + pos.partial_pnl;
            let net_pnl = raw_pnl - pos.total_fees - exit_fee - pos.cumulative_funding;
            let pnl_pct = net_pnl / (pos.entry_price * pos.initial_size) * 100.0;

            self.capital += net_pnl;

            // Calculate entry improvement
            let (sig_price, improvement) = if pos.signal_price > 0.0 {
                let imp = if pos.is_long {
                    // Long: entering lower is better
                    (pos.signal_price - pos.entry_price) / pos.signal_price * 100.0
                } else {
                    // Short: entering higher is better
                    (pos.entry_price - pos.signal_price) / pos.signal_price * 100.0
                };
                (pos.signal_price, imp)
            } else {
                (0.0, 0.0)
            };

            self.trades.push(TradeRecord {
                trade_id: pos.trade_id,
                is_long: pos.is_long,
                entry_price: pos.entry_price,
                exit_price: fill_price,
                entry_time: pos.entry_time,
                exit_time: ts,
                exit_reason: reason.to_string(),
                initial_size: pos.initial_size,
                realized_pnl: net_pnl,
                realized_pnl_pct: pnl_pct,
                tp1_hit: pos.tp1_hit,
                tp2_hit: pos.tp2_hit,
                tp3_hit: pos.tp3_hit,
                max_favorable: pos.max_favorable,
                max_adverse: pos.max_adverse,
                funding_paid: pos.cumulative_funding,
                fees_paid: pos.total_fees + exit_fee,
                signal_price: sig_price,
                entry_improvement_pct: improvement,
            });
        }
    }

    fn partial_close(
        &mut self,
        price: f64,
        ts: f64,
        close_pct: f64,
        new_sl: f64,
        reason: &'static str,
    ) {
        let pos = self.position.as_mut().unwrap();
        let close_size = pos.size * close_pct;
        let fill_price = apply_slippage(price, self.config.slippage_pct, !pos.is_long);

        let fee = calc_fee(fill_price, close_size, self.config.fee_pct);
        let pnl = pos.pnl_usd(fill_price, close_size);

        pos.partial_pnl += pnl;
        pos.total_fees += fee;
        pos.size -= close_size;
        pos.current_sl = new_sl;
        pos.last_sl_update_price = new_sl;

        self.capital += pnl - fee;

        match reason {
            "tp1_hit" => pos.tp1_hit = true,
            "tp2_hit" => pos.tp2_hit = true,
            "tp3_hit" => pos.tp3_hit = true,
            _ => {}
        }

        // If position too small, close entirely
        if pos.size < 0.0001 {
            self.close_position(price, ts, reason);
        }
    }

    #[inline(always)]
    fn current_equity(&self, price: f64) -> f64 {
        match &self.position {
            Some(pos) => {
                let unrealized = pos.pnl_usd(price, pos.size);
                self.capital + unrealized
            }
            None => self.capital,
        }
    }
}

use crate::types::{Action, Position, StrategyConfig};

/// Multi-level trailing TP/SL — exact port of Python evaluate().
/// Pure math, no allocations, ~5ns per call.
#[inline(always)]
pub fn evaluate(pos: &Position, price: f64, cfg: &StrategyConfig) -> Action {
    let pnl_pct = pos.pnl_pct(price);

    // ── TP3: Start tight trailing ──────────────────────────────────
    if !pos.tp3_hit && pnl_pct >= cfg.tp3_pct {
        let new_sl = calc_trailing_sl(pos, cfg);
        return Action::MoveSL {
            price: new_sl,
            reason: "tp3_hit",
        };
    }

    // ── TP2: Move SL to TP1 price + partial close ──────────────────
    if !pos.tp2_hit && pnl_pct >= cfg.tp2_pct {
        let tp1_price = calc_tp_price(pos, cfg.tp1_pct);
        if cfg.tp2_close_pct > 0.0 {
            return Action::PartialClose {
                close_pct: cfg.tp2_close_pct,
                new_sl: tp1_price,
                reason: "tp2_hit",
            };
        }
        return Action::MoveSL {
            price: tp1_price,
            reason: "tp2_hit",
        };
    }

    // ── TP1: Move SL to breakeven + partial close ──────────────────
    if !pos.tp1_hit && pnl_pct >= cfg.tp1_pct {
        if cfg.tp1_close_pct > 0.0 {
            return Action::PartialClose {
                close_pct: cfg.tp1_close_pct,
                new_sl: pos.entry_price,
                reason: "tp1_hit",
            };
        }
        return Action::MoveSL {
            price: pos.entry_price,
            reason: "tp1_hit",
        };
    }

    // ── Active trailing (after TP3) ────────────────────────────────
    if pos.tp3_hit {
        let new_sl = calc_trailing_sl(pos, cfg);

        // Only move SL in profitable direction
        if pos.is_long && new_sl <= pos.current_sl {
            return Action::None;
        }
        if !pos.is_long && (pos.current_sl > 0.0 && new_sl >= pos.current_sl) {
            return Action::None;
        }

        // Smart threshold — skip tiny changes
        if pos.last_sl_update_price > 0.0 {
            let change_pct = ((new_sl - pos.last_sl_update_price) / pos.last_sl_update_price * 100.0).abs();
            if change_pct < cfg.min_sl_change_pct {
                return Action::None;
            }
        }

        return Action::MoveSL {
            price: new_sl,
            reason: "trailing_update",
        };
    }

    Action::None
}

#[inline(always)]
fn calc_tp_price(pos: &Position, pct: f64) -> f64 {
    if pos.is_long {
        pos.entry_price * (1.0 + pct / 100.0)
    } else {
        pos.entry_price * (1.0 - pct / 100.0)
    }
}

#[inline(always)]
fn calc_trailing_sl(pos: &Position, cfg: &StrategyConfig) -> f64 {
    if pos.is_long {
        pos.highest_since_entry * (1.0 - cfg.trail_pct / 100.0)
    } else {
        pos.lowest_since_entry * (1.0 + cfg.trail_pct / 100.0)
    }
}

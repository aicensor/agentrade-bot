use crate::types::Position;

/// Simulated exchange — handles fills, funding, liquidation.

/// Apply slippage to a fill price.
#[inline(always)]
pub fn apply_slippage(price: f64, slippage_pct: f64, is_buy: bool) -> f64 {
    if is_buy {
        price * (1.0 + slippage_pct / 100.0)
    } else {
        price * (1.0 - slippage_pct / 100.0)
    }
}

/// Calculate fee for a trade.
#[inline(always)]
pub fn calc_fee(price: f64, size: f64, fee_pct: f64) -> f64 {
    price * size * fee_pct / 100.0
}

/// Check if stop-loss was hit at this price.
#[inline(always)]
pub fn check_sl_hit(pos: &Position, price: f64) -> bool {
    if pos.current_sl <= 0.0 {
        return false;
    }
    if pos.is_long {
        price <= pos.current_sl
    } else {
        price >= pos.current_sl
    }
}

/// Check if position should be liquidated.
#[inline(always)]
pub fn check_liquidation(pos: &Position, price: f64) -> bool {
    let liq = pos.liquidation_price();
    if pos.is_long {
        price <= liq
    } else {
        price >= liq
    }
}

/// Process funding payment (called every funding_interval).
/// Returns funding amount (positive = paid, negative = received).
#[inline(always)]
pub fn calc_funding_payment(pos: &Position, funding_rate_pct: f64, mark_price: f64) -> f64 {
    // Notional value * funding rate
    // Positive funding rate: longs pay shorts
    // Negative funding rate: shorts pay longs
    let notional = mark_price * pos.size;
    let payment = notional * funding_rate_pct / 100.0;

    if pos.is_long {
        payment  // longs pay when rate is positive
    } else {
        -payment  // shorts receive when rate is positive
    }
}

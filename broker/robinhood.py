"""
robin_stocks wrapper — login, place equity and option orders, fetch positions.
Orders are never placed without an explicit call to place_equity_order or place_option_order.
"""
import robin_stocks.robinhood as rh
from config import RH_USERNAME, RH_PASSWORD

_logged_in = False


def login():
    global _logged_in
    if _logged_in:
        return
    rh.login(username=RH_USERNAME, password=RH_PASSWORD, store_session=True)
    _logged_in = True


def get_positions():
    login()
    return rh.get_open_stock_positions()


def get_option_positions():
    login()
    return rh.get_open_option_positions()


def get_portfolio_value():
    login()
    profile = rh.load_portfolio_profile()
    return float(profile.get("equity", 0))


def place_equity_order(symbol: str, quantity: int, side: str, order_type: str = "market", limit_price: float = None):
    login()
    if order_type == "market":
        return rh.order_buy_market(symbol, quantity) if side == "buy" else rh.order_sell_market(symbol, quantity)
    if limit_price is None:
        raise ValueError("limit_price required for limit orders")
    return (
        rh.order_buy_limit(symbol, quantity, limit_price)
        if side == "buy"
        else rh.order_sell_limit(symbol, quantity, limit_price)
    )


def place_option_order(symbol: str, expiry: str, strike: float, option_type: str, quantity: int, side: str, limit_price: float):
    """expiry: 'YYYY-MM-DD', option_type: 'call'|'put', side: 'buy'|'sell'"""
    login()
    if side == "buy":
        return rh.order_buy_option_limit("open", "debit", limit_price, symbol, quantity, expiry, strike, option_type)
    return rh.order_sell_option_limit("close", "credit", limit_price, symbol, quantity, expiry, strike, option_type)


def cancel_order(order_id: str):
    login()
    return rh.cancel_stock_order(order_id)

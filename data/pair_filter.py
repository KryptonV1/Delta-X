"""
data/pair_filter.py — Filter Binance pairs, block stablecoins & wrapped tokens
"""
from config.settings import STABLECOINS, WRAPPED_TOKENS, SKIP_SUFFIXES, BASE_CURRENCY
from utils.logger import get_logger

log = get_logger("pair_filter")


def is_valid_pair(symbol: str) -> bool:
    """
    Return True if the symbol is a tradable spot pair that is NOT:
    - a stablecoin base
    - a wrapped token
    - a leveraged/directional token (UP/DOWN/BULL/BEAR/3L/3S etc.)
    - quoted in anything other than USDT
    """
    if not symbol.endswith(BASE_CURRENCY):
        return False

    base = symbol[: -len(BASE_CURRENCY)]   # strip USDT suffix

    if base in STABLECOINS:
        return False
    if base in WRAPPED_TOKENS:
        return False
    if any(base.endswith(s) for s in SKIP_SUFFIXES):
        return False

    return True


def filter_pairs(all_symbols: list[str]) -> list[str]:
    """Filter a raw list of Binance symbols and return only valid ones."""
    valid = [s for s in all_symbols if is_valid_pair(s)]
    log.info(f"Pair filter: {len(all_symbols)} total → {len(valid)} valid")
    return sorted(valid)

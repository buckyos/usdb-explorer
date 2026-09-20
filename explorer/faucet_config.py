"""Validate optional testnet faucet policy using exact native-token units."""
from decimal import Decimal
import re


DEFAULTS = {"gas_reserve": "0.01", "max_gas_price_gwei": "100", "cooldown_seconds": 86400,
            "ip_daily_claims": 10, "ip_requests_per_minute": 10, "confirmations": 3}


def amount(value, decimals=18):
    """Reject exponent notation and amounts which cannot be represented exactly."""
    if (not isinstance(value, str) or not re.fullmatch(r"(0|[1-9][0-9]{0,30})(\.[0-9]+)?", value)
            or ("." in value and len(value.split(".")[1]) > decimals) or Decimal(value) <= 0):
        raise ValueError("faucet amounts must be positive decimal strings with supported precision")
    whole, _, fraction = value.partition(".")
    return int(whole + fraction.ljust(decimals, "0"))


def validate_faucet(config):
    """Existing configurations stay disabled until an operator supplies explicit funding policy."""
    if "faucet" not in config:
        return
    value = config["faucet"]
    if (not isinstance(value, dict) or set(value) - {"enabled", "claim_amount", "daily_budget", *DEFAULTS}
            or type(value.get("enabled")) is not bool):
        raise ValueError("invalid faucet configuration fields")
    if not value["enabled"]:
        return
    for key, default in DEFAULTS.items():
        value.setdefault(key, default)
    claim, budget = amount(value.get("claim_amount")), amount(value.get("daily_budget"))
    amount(value["gas_reserve"])
    gas = amount(value["max_gas_price_gwei"], 9)
    if budget < claim + 21000 * gas:
        raise ValueError("faucet daily_budget must cover one claim and its maximum transaction fee")
    for key, minimum, maximum in (("cooldown_seconds", 60, 31536000), ("ip_daily_claims", 1, 10000),
                                  ("ip_requests_per_minute", 1, 1000), ("confirmations", 1, 100)):
        if type(value[key]) is not int or not minimum <= value[key] <= maximum:
            raise ValueError(f"invalid faucet policy: {key}")

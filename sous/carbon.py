"""CO2 as a range, not one number.

Valio Aimo's carbon file gives each product one product-group average (kg CO2e per kg). A single figure hides how much
the choice matters, so a quote line also carries `co2_range_kg`: the lowest and highest estimate among the orderable
products shortlisted for the same item, for the same quantity. It is the spread between *options*, not a measurement
uncertainty, and it says so. The quote adds `co2_summary` (estimate, low and high across the order).
"""

from __future__ import annotations

RANGE_BASIS = ("spread of the product-group averages among the orderable products shortlisted for this item, for "
               "the same quantity; a spread between options, not a measurement uncertainty")
SUMMARY_BASIS = ("sum of product-group averages x quantity (an estimate); low and high take, for each item, the "
                 "lowest and highest option the agent shortlisted")


def per_unit(offer: dict) -> float | None:
    """kg CO2e per unit covered, for an orderable offer that has CO2 data."""
    est, cov = offer.get("co2_estimate_kg"), (offer.get("covers") or {}).get("amount")
    return est / cov if offer.get("orderable") and est is not None and cov else None


def add_range(chosen: list[dict], offers: list[dict]) -> None:
    """Put co2_range_kg on each chosen line from the item's orderable offers (needs two with data)."""
    rates = [r for o in offers if (r := per_unit(o)) is not None]
    for l in chosen:
        cov = (l.get("covers") or {}).get("amount")
        if len(rates) >= 2 and cov and l.get("co2_estimate_kg") is not None:
            l["co2_range_kg"] = {"low": round(min(rates) * cov, 1), "high": round(max(rates) * cov, 1),
                                 "options": len(rates), "basis": RANGE_BASIS}
        else:
            l["co2_range_kg"] = None


def summary(lines: list[dict]) -> dict:
    """Order-level estimate with low and high; items without CO2 data are counted, not guessed."""
    use = [l for l in lines if l.get("orderable", True)]
    have = [l for l in use if l.get("co2_estimate_kg") is not None]
    est = sum(l["co2_estimate_kg"] for l in have)
    low = sum((l.get("co2_range_kg") or {}).get("low", l["co2_estimate_kg"]) for l in have)
    high = sum((l.get("co2_range_kg") or {}).get("high", l["co2_estimate_kg"]) for l in have)
    return {"estimate_kg": round(est, 1), "low_kg": round(min(low, est), 1), "high_kg": round(max(high, est), 1),
            "lines_with_data": len(have), "lines": len(use), "basis": SUMMARY_BASIS}

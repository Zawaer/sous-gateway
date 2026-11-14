"""CO2 as a range: spread between the shortlisted options, per line and per order."""

from sous import carbon


def offer(pid, est, cov=100.0, orderable=True):
    return {"product_id": pid, "co2_estimate_kg": est, "covers": {"amount": cov, "unit": "KG"}, "orderable": orderable,
            "orderable_note": None}


def test_range_spans_orderable_options_for_the_same_quantity():
    offers = [offer("A", 110.0), offer("B", 181.0), offer("C", 230.0, orderable=False), offer("D", None)]
    chosen = [dict(offers[0])]
    carbon.add_range(chosen, offers)
    r = chosen[0]["co2_range_kg"]
    assert (r["low"], r["high"], r["options"]) == (110.0, 181.0, 2)  # the unorderable one is not an option
    assert "not a measurement uncertainty" in r["basis"]


def test_scales_to_the_chosen_line_quantity():
    offers = [offer("A", 110.0, 100.0), offer("B", 181.0, 100.0)]
    line = {**offer("A", 55.0, 50.0)}  # a split line covering half the need
    carbon.add_range([line], offers)
    assert (line["co2_range_kg"]["low"], line["co2_range_kg"]["high"]) == (55.0, 90.5)


def test_one_option_or_no_data_gives_no_range():
    only = [offer("A", 110.0)]
    line = dict(only[0])
    carbon.add_range([line], only)
    assert line["co2_range_kg"] is None
    nodata = {**offer("N", None)}
    carbon.add_range([nodata], [offer("A", 1.0), offer("B", 2.0)])
    assert nodata["co2_range_kg"] is None


def test_summary_adds_up_and_counts_missing_data():
    a = {**offer("A", 110.0), "co2_range_kg": {"low": 110.0, "high": 181.0}}
    b = {**offer("B", 40.0), "co2_range_kg": None}
    c = {**offer("C", None)}
    u = {**offer("U", 999.0, orderable=False)}
    s = carbon.summary([a, b, c, u])
    assert (s["estimate_kg"], s["low_kg"], s["high_kg"]) == (150.0, 150.0, 221.0)
    assert (s["lines_with_data"], s["lines"]) == (2, 3)
    assert "estimate" in s["basis"]


def test_a_null_range_says_why():
    only = [offer("A", 110.0), offer("B", 90.0, orderable=False), offer("C", None)]
    line = dict(only[0])
    carbon.add_range([line], only)
    assert line["co2_range_kg"] is None and "1 of the 3 shortlisted (2 orderable)" in line["co2_range_note"]
    nodata = {**offer("N", None)}
    carbon.add_range([nodata], [offer("A", 1.0), offer("B", 2.0)])
    assert "no product-group average" in nodata["co2_range_note"]
    packs = {**offer("P", None), "covers": None}  # need in sales units: no kg/L to scale to
    carbon.add_range([packs], [offer("A", 1.0), offer("B", 2.0)])
    assert "quantity in kg or L isn't known" in packs["co2_range_note"]
    ranged = dict(offer("A", 110.0))
    carbon.add_range([ranged], [offer("A", 110.0), offer("B", 181.0)])
    assert "co2_range_note" not in ranged

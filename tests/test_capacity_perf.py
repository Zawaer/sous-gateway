"""The two hot-path speed-ups found by scripts/loadtest.py (docs/CAPACITY.md) return exactly what the old code did."""

import json
import re
import sqlite3
import threading

from sous import catalog, config


def _old_term_score(t: str, name: str) -> float:
    e = re.escape(t)
    if re.search(rf"\b{e}\b(?!-)", name):
        return 3.0
    if re.search(rf"{e}\b(?!-)", name):
        return 2.5
    if re.search(rf"\b{e}", name):
        return 1.0
    return 0.5 if t in name else 0.0


def test_term_score_matches_uncached_regexes():
    terms = ["maito", "voi", "milk", "smör", "kerma", "juusto", "muna", "a.b", "c++", "laktoositon", "x", ""]
    names = ["valio kevytmaito 1l", "kasvivoi 400g", "maitosuklaa", "muna-riisipasteija", "voima juoma",
             "milk chocolate", "smör 500 g", "kermaviili", "kevyt-maito", "a.b c++ test", "juustot", "", "maito-"]
    for t in terms:
        for n in names:
            assert catalog._term_score(t, n) == _old_term_score(t, n), (t, n)


def test_in_category_matches_json_scan(tmp_path, monkeypatch):
    db = tmp_path / "catalog.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE product (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    rows = [(f"P{i:03d}", {"id": f"P{i:03d}", "category": [None, "10400", "20100", 7][i % 4], "n": i})
            for i in (5, 1, 9, 2, 14, 3, 0, 8, 11)]  # inserted out of id order: catalog (rowid) order must hold
    c.executemany("INSERT INTO product VALUES (?, ?)", [(i, json.dumps(d)) for i, d in rows])
    c.commit()
    monkeypatch.setattr(config, "CATALOG_DB", db)
    monkeypatch.setattr(catalog, "_local", threading.local())
    monkeypatch.setattr(catalog, "_categories", None)
    for cat in ["10400", "20100", "7", "none", None]:
        old = [json.loads(d) for (d,) in c.execute(
            "SELECT data FROM product WHERE json_extract(data, '$.category') = ?", (cat,))]
        assert catalog.in_category(cat) == old, cat
    assert [p["n"] for p in catalog.in_category("10400")] == [5, 1, 9]
    # a rebuilt catalog file is re-indexed
    c.execute("INSERT INTO product VALUES (?, ?)", ("P999", json.dumps({"id": "P999", "category": "10400"})))
    c.commit()
    c.close()
    catalog._local.conn = None
    assert [p["id"] for p in catalog.in_category("10400")][-1] == "P999"

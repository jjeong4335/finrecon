"""UCI Online Retail II module: reading, classification, validation, control totals.

Uses hand-written rows in the UCI layout (tests/fixtures/uci_format), turned into
a two-sheet .xlsx inside a .zip, the same shape as the real download.
"""
import csv
import datetime as dt
import zipfile
from pathlib import Path

import pytest

from finrecon.db import Database
from finrecon.ingestion import uci_retail as U

FIX = Path(__file__).parent / "fixtures" / "uci_format"
SHEETS = [("Year 2009-2010", FIX / "sheet_2009_2010.csv"), ("Year 2010-2011", FIX / "sheet_2010_2011.csv")]


def _excel_value(col: str, v: str):
    """Mimic the types Excel/openpyxl return for the real file."""
    if v == "":
        return None
    if col == "InvoiceDate":
        return dt.datetime.strptime(v, "%Y-%m-%d %H:%M")
    if col in ("Invoice", "StockCode") and v.isdigit():
        return int(v)
    if col == "Quantity":
        try:
            return int(v)
        except ValueError:
            return v
    if col in ("Price", "Customer ID"):
        return float(v)
    return v


def make_xlsx(path: Path, header_override: list[str] | None = None) -> Path:
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, src in SHEETS:
        ws = wb.create_sheet(name)
        with open(src, newline="") as f:
            rows = list(csv.reader(f))
        ws.append(header_override or rows[0])
        for r in rows[1:]:
            ws.append([_excel_value(c, v) for c, v in zip(rows[0], r)])
    wb.save(path)
    return path


def make_zip(tmp_path: Path) -> Path:
    xlsx = make_xlsx(tmp_path / "online_retail_II.xlsx")
    z = tmp_path / "online_retail_ii.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(xlsx, "online_retail_II.xlsx")
    return z


@pytest.fixture
def loaded(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'r.db'}")
    result = U.load(db, make_zip(tmp_path), chunk_size=4, log=lambda *a: None)
    yield db, result
    db.close()


def line(db, sheet_row: int, sheet="Year 2009-2010"):
    return db.query("SELECT * FROM retail_lines WHERE source_row_id = ?", (f"{sheet}#{sheet_row}",))[0]


def test_every_row_is_kept_and_chunking_works(loaded):
    db, r = loaded
    assert r["rows_read"] == r["rows_loaded"] == 20
    assert db.scalar("SELECT COUNT(*) FROM retail_raw_lines") == 20
    run = db.query("SELECT * FROM retail_load_runs")[0]
    assert run["status"] == "completed" and run["chunk_size"] == 4 and run["rows_per_second"] > 0


def test_classification(loaded):
    db, _ = loaded
    assert line(db, 2)["line_class"] == "VALID_SALE" and line(db, 2)["line_amount_cents"] == 8340
    assert line(db, 4)["line_class"] == "NON_PRODUCT"              # POST
    assert line(db, 8)["line_class"] == "CANCELLATION" and line(db, 8)["line_amount_cents"] == -1390
    adj = line(db, 10)
    assert adj["line_class"] == "STOCK_ADJUSTMENT" and adj["zero_price"] and adj["missing_customer"]
    assert line(db, 12)["line_class"] == "ADJUSTMENT"             # A506401 bad debt
    bad = line(db, 7, "Year 2010-2011")
    assert bad["line_class"] == "INVALID" and "quantity" in bad["invalid_reason"]


def test_raw_text_preserved_and_ids_normalized(loaded):
    db, _ = loaded
    raw = db.query("SELECT * FROM retail_raw_lines WHERE source_row_id = 'Year 2009-2010#2'")[0]
    assert raw["customer_id_raw"] == "13085" and raw["price_raw"] == "6.95"     # as openpyxl delivered it
    assert line(db, 2)["customer_id"] == "13085" and line(db, 2)["invoice"] == "489434"


def test_missing_customer_kept_in_revenue(loaded):
    db, _ = loaded
    l = line(db, 5)
    assert l["missing_customer"] and l["line_class"] == "VALID_SALE" and l["line_amount_cents"] == 3060
    assert db.scalar("SELECT amount_cents FROM retail_invoices WHERE invoice = '489435'") == 3060


def test_repeated_lines_are_flagged_not_deleted(loaded):
    db, _ = loaded
    rows = db.query("SELECT possible_duplicate, overlap_excluded FROM retail_lines WHERE invoice = '489436'")
    assert len(rows) == 2 and all(r["possible_duplicate"] and not r["overlap_excluded"] for r in rows)
    assert db.scalar("SELECT amount_cents FROM retail_invoices WHERE invoice = '489436'") == 6000


def test_identical_sheet_overlap_excluded_conflict_kept(loaded):
    db, _ = loaded
    assert line(db, 2, "Year 2010-2011")["overlap_excluded"]           # 489453 identical in both sheets
    assert not line(db, 13)["overlap_excluded"]                         # first sheet's copy is canonical
    assert db.scalar("SELECT amount_cents FROM retail_invoices WHERE invoice = '489453'") == 509
    conflict = db.query("SELECT overlap_conflict, overlap_excluded FROM retail_lines WHERE invoice = '489455'")
    assert len(conflict) == 2 and all(r["overlap_conflict"] and not r["overlap_excluded"] for r in conflict)


def test_sub_penny_rounding(loaded):
    db, _ = loaded
    l = line(db, 13)                                                    # 3 x 1.695 = 5.085
    assert l["sub_penny"] and l["line_amount_cents"] == 509 and l["unit_price"] == "1.695"


def test_cancellation_links(loaded):
    db, _ = loaded
    st = {r["source_row_id"]: r["status"] for r in db.query("SELECT * FROM retail_cancellation_links")}
    assert st == {"Year 2009-2010#8": "LINKED", "Year 2009-2010#9": "NO_PRIOR_SALE"}


def test_control_totals_and_quality_results(loaded):
    db, _ = loaded
    q = {r["check_name"]: r for r in db.query("SELECT * FROM retail_quality_results")}
    assert q["control_row_count"]["passed"] and q["control_amount_totals"]["passed"]
    assert q["sheet_overlap_excluded"]["rows_affected"] == 2
    assert q["sheet_overlap_conflicts"]["rows_affected"] == 2 and not q["sheet_overlap_conflicts"]["passed"]
    assert q["possible_duplicate_lines"]["rows_affected"] == 2
    assert q["missing_customer_id"]["rows_affected"] == 3
    assert q["class_invalid"]["rows_affected"] == 1 and q["class_invalid"]["severity"] == "error"
    assert q["missing_description"]["rows_affected"] == 1
    canonical = db.scalar("SELECT SUM(line_amount_cents) FROM retail_lines WHERE NOT overlap_excluded")
    assert canonical == db.scalar("SELECT SUM(amount_cents) FROM retail_invoices")
    assert db.scalar("SELECT COUNT(*) FROM retail_invoices") == 14


def test_reload_is_idempotent(loaded, tmp_path):
    db, _ = loaded
    before = db.query("SELECT invoice, amount_cents FROM retail_invoices ORDER BY invoice")
    U.load(db, make_zip(tmp_path), chunk_size=7, log=lambda *a: None)
    assert db.scalar("SELECT COUNT(*) FROM retail_lines") == 20
    assert db.query("SELECT invoice, amount_cents FROM retail_invoices ORDER BY invoice") == before


def test_alternative_header_names_and_csv_input(tmp_path):
    src = (FIX / "sheet_2009_2010.csv").read_text().splitlines()
    src[0] = "InvoiceNo,StockCode,Description,Quantity,InvoiceDate,UnitPrice,CustomerID,Country"
    p = tmp_path / "retail.csv"
    p.write_text("\n".join(src) + "\n")
    db = Database(f"sqlite:///{tmp_path / 'c.db'}")
    r = U.load(db, p, log=lambda *a: None)
    assert r["rows_read"] == 14
    assert db.scalar("SELECT line_amount_cents FROM retail_lines WHERE source_row_id = 'retail#2'") == 8340


def test_bad_header_is_rejected(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("a,b,c\n1,2,3\n")
    with pytest.raises(ValueError):
        list(next(U.iter_sheets(p))[1])


def test_dashboard_queries_handle_retail(loaded):
    from finrecon import dashboard_data as dd

    db, _ = loaded
    s = dd.retail_summary(db)
    assert s["load"]["rows_read"] == 20 and s["classes"] and s["checks"] and s["monthly"]


def test_retail_never_touches_reconciliation(loaded):
    db, _ = loaded
    assert db.scalar("SELECT COUNT(*) FROM reconciliation_results") == 0
    assert db.scalar("SELECT COUNT(*) FROM invoices") == 0

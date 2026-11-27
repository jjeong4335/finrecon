"""UCI Online Retail II: real public transaction data, loaded and quality-checked.

Source: Chen, D. (2012). Online Retail II [Dataset]. UCI Machine Learning
Repository. https://doi.org/10.24432/C5CG6D  (CC BY 4.0)
About 1.07M invoice lines from a UK online retailer, 2009-12 to 2011-12, in two
Excel sheets. Real data, with real problems: missing customer IDs, cancellation
invoices ("C..."), bad-debt adjustments ("A..."), non-product lines (postage,
fees), stock write-offs with negative quantities, zero prices, repeated lines.

This module is deliberately separate from the QuickBooks/Stripe reconciliation.
The retail data has no Stripe or QuickBooks IDs, so it is never matched to them,
and Country is the customer's country, not a store location.

Pipeline
  read (chunked)  -> retail_raw_lines   every row as text, keyed "<sheet>#<row>"
  parse/classify  -> retail_lines       one row per raw row, nothing dropped
  validate (SQL)  -> flags: repeated lines, sheet overlap, cancellation links
  aggregate (SQL) -> retail_invoices
  control totals  -> retail_quality_results, retail_load_runs (timings)

Usage
  python -m finrecon.ingestion.uci_retail download            # needs internet (e.g. on EC2)
  python -m finrecon.ingestion.uci_retail load --file PATH    # .zip, .xlsx or .csv
  python -m finrecon.ingestion.uci_retail all                 # download + load
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import re
import time
import zipfile
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator

from ..config import ROOT, get_settings
from ..db import Database, utcnow

DOWNLOAD_URL = "https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip"
DEFAULT_PATH = ROOT / "data" / "external" / "online_retail_II.zip"
CITATION = ("Chen, D. (2012). Online Retail II [Dataset]. UCI Machine Learning Repository. "
            "https://doi.org/10.24432/C5CG6D (CC BY 4.0)")
CURRENCY = "GBP"
CHUNK_SIZE = 50_000

# The UCI page and the file use different header names; accept both.
ALIASES = {
    "invoice": "invoice", "invoiceno": "invoice",
    "stockcode": "stock_code",
    "description": "description",
    "quantity": "quantity",
    "invoicedate": "invoice_date",
    "price": "price", "unitprice": "price",
    "customerid": "customer_id", "customer id": "customer_id", "customer_id": "customer_id",
    "country": "country",
}
FIELDS = ["invoice", "stock_code", "description", "quantity", "invoice_date", "price", "customer_id", "country"]
PRODUCT_CODE = re.compile(r"^\d{5}[A-Za-z]{0,2}$")


# ----------------------------------------------------------------------- reading
def _header_map(header: list) -> dict[int, str]:
    out = {}
    for i, h in enumerate(header):
        key = re.sub(r"[\s_]+", "", str(h or "")).lower()
        key = ALIASES.get(key) or ALIASES.get(str(h or "").strip().lower())
        if key:
            out[i] = key
    missing = set(FIELDS) - set(out.values())
    if missing:
        raise ValueError(f"unrecognized header {header!r}; missing {sorted(missing)}")
    return out


def _text(v) -> str | None:
    """Cell value -> stable text (keeps the original, before any parsing)."""
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, float):
        return repr(v)
    s = str(v).strip()
    return s if s != "" else None


def iter_sheets(path: Path) -> Iterator[tuple[str, Iterator[dict]]]:
    """Yield (sheet_name, rows) for a .zip (containing .xlsx/.csv), .xlsx or .csv file."""
    path = Path(path)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            members = [m for m in z.namelist() if m.lower().endswith((".xlsx", ".csv"))]
            if not members:
                raise ValueError(f"{path} contains no .xlsx or .csv file")
            for m in members:
                data = io.BytesIO(z.read(m))
                yield from (_xlsx_sheets(data) if m.lower().endswith(".xlsx") else
                            [(Path(m).stem, _csv_rows(io.TextIOWrapper(data, encoding="utf-8-sig")))])
    elif path.suffix.lower() == ".xlsx":
        yield from _xlsx_sheets(path)
    elif path.suffix.lower() == ".csv":
        with open(path, encoding="utf-8-sig", newline="") as f:
            yield path.stem, _csv_rows(f)
    else:
        raise ValueError(f"unsupported file type: {path}")


def _xlsx_sheets(source) -> Iterator[tuple[str, Iterator[dict]]]:
    import openpyxl

    wb = openpyxl.load_workbook(source, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            yield ws.title, _rows(ws.iter_rows(values_only=True))
    finally:
        wb.close()


def _csv_rows(f) -> Iterator[dict]:
    return _rows(csv.reader(f))


def _rows(it) -> Iterator[dict]:
    it = iter(it)
    try:
        header = next(it)
    except StopIteration:
        return
    cols = _header_map(list(header))
    for n, row in enumerate(it, start=2):          # row 1 is the header
        rec = {"_row": n}
        for i, key in cols.items():
            rec[key] = _text(row[i]) if i < len(row) else None
        if all(rec.get(f) is None for f in FIELDS):
            continue                                # trailing empty rows
        yield rec


# ----------------------------------------------------------------------- parsing
@dataclass
class Parsed:
    invoice_type: str
    is_product: bool
    quantity: int | None
    invoice_ts: str | None
    unit_price: str | None
    amount_cents: int | None
    sub_penny: bool
    customer_id: str | None
    line_class: str
    invalid_reason: str | None


def _invoice_type(invoice: str | None) -> str:
    if not invoice:
        return "UNKNOWN"
    head = invoice[0].upper()
    if head == "C":
        return "CANCELLATION"
    if head == "A":
        return "ADJUSTMENT"
    return "SALE" if invoice[0].isdigit() else "UNKNOWN"


def _customer(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        d = Decimal(raw)
        return str(int(d)) if d == d.to_integral_value() else raw
    except InvalidOperation:
        return raw


def parse(rec: dict) -> Parsed:
    reason = None
    qty = None
    try:
        q = Decimal(rec["quantity"]) if rec.get("quantity") is not None else None
        if q is None or q != q.to_integral_value():
            raise InvalidOperation
        qty = int(q)
    except (InvalidOperation, TypeError):
        reason = f"quantity not an integer: {rec.get('quantity')!r}"
    price = None
    try:
        price = Decimal(rec["price"]) if rec.get("price") is not None else None
        if price is None:
            raise InvalidOperation
    except (InvalidOperation, TypeError):
        price = None
        reason = reason or f"price not a number: {rec.get('price')!r}"
    ts = None
    raw_date = rec.get("invoice_date")
    if raw_date:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M"):
            try:
                ts = dt.datetime.strptime(raw_date, fmt).isoformat(sep=" ")
                break
            except ValueError:
                continue
    if ts is None:
        reason = reason or f"invoice date not parseable: {raw_date!r}"

    amount, sub_penny = None, False
    if qty is not None and price is not None:
        exact = Decimal(qty) * price * 100
        rounded = exact.quantize(Decimal(1), ROUND_HALF_UP)
        amount, sub_penny = int(rounded), rounded != exact

    inv_type = _invoice_type(rec.get("invoice"))
    code = rec.get("stock_code") or ""
    is_product = bool(PRODUCT_CODE.match(code))
    if reason:
        cls = "INVALID"
    elif inv_type == "CANCELLATION":
        cls = "CANCELLATION"
    elif inv_type == "ADJUSTMENT":
        cls = "ADJUSTMENT"
    elif inv_type == "UNKNOWN":
        cls, reason = "INVALID", f"unrecognized invoice number {rec.get('invoice')!r}"
    elif not is_product:
        cls = "NON_PRODUCT"
    elif qty < 0:
        cls = "STOCK_ADJUSTMENT"
    elif price < 0:
        cls, reason = "INVALID", "negative unit price on a sale line"
    else:
        cls = "VALID_SALE"
    return Parsed(inv_type, is_product, qty, ts, None if price is None else format(price, "f"), amount, sub_penny,
                  _customer(rec.get("customer_id")), cls, reason)


def row_hash(rec: dict) -> str:
    return hashlib.sha1("\x1f".join(str(rec.get(f) or "") for f in FIELDS).encode()).hexdigest()


# ----------------------------------------------------------------------- loading
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(dest: Path = DEFAULT_PATH) -> Path:
    import requests

    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(DOWNLOAD_URL, stream=True, timeout=120) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(".part")
        with open(tmp, "wb") as f:
            for block in r.iter_content(1 << 20):
                f.write(block)
    tmp.replace(dest)
    return dest


def load(db: Database, path: Path, chunk_size: int = CHUNK_SIZE, log=print) -> dict:
    """Load a UCI Online Retail II file. Re-running replaces the previous load."""
    path = Path(path)
    db.init_schema()
    load_id = f"retail-{hashlib.sha256(path.name.encode()).hexdigest()[:6]}-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S}"
    sha = _sha256(path)
    db.upsert("retail_load_runs", {"load_id": load_id, "source_file": path.name, "source_sha256": sha,
                                   "started_at": utcnow(), "finished_at": None, "status": "running",
                                   "chunk_size": chunk_size, "rows_read": None, "rows_loaded": None,
                                   "seconds_read_parse": None, "seconds_load": None, "seconds_validate": None,
                                   "rows_per_second": None, "detail": CITATION}, ["load_id"])
    # Idempotent: one current load. Children first (foreign keys).
    for t in ("retail_cancellation_links", "retail_invoices", "retail_lines", "retail_raw_lines"):
        db.execute(f"DELETE FROM {t}")
    db.commit()

    t_parse = t_load = 0.0
    rows_read = 0
    raw_buf: list[dict] = []
    line_buf: list[dict] = []

    def flush():
        nonlocal t_load
        t0 = time.perf_counter()
        db.upsert_many("retail_raw_lines", raw_buf, ["source_row_id"])
        db.upsert_many("retail_lines", line_buf, ["source_row_id"])
        db.commit()
        t_load += time.perf_counter() - t0
        raw_buf.clear()
        line_buf.clear()

    for order, (sheet, rows) in enumerate(iter_sheets(path)):
        t0 = time.perf_counter()
        for rec in rows:
            rows_read += 1
            sid = f"{sheet}#{rec['_row']}"
            p = parse(rec)
            raw_buf.append({"source_row_id": sid, "load_id": load_id, "sheet": sheet, "sheet_order": order,
                            "sheet_row": rec["_row"], "invoice_raw": rec.get("invoice"),
                            "stock_code_raw": rec.get("stock_code"), "description_raw": rec.get("description"),
                            "quantity_raw": rec.get("quantity"), "invoice_date_raw": rec.get("invoice_date"),
                            "price_raw": rec.get("price"), "customer_id_raw": rec.get("customer_id"),
                            "country_raw": rec.get("country"), "row_hash": row_hash(rec)})
            line_buf.append({"source_row_id": sid, "sheet": sheet, "sheet_order": order,
                             "invoice": rec.get("invoice"), "invoice_type": p.invoice_type,
                             "stock_code": rec.get("stock_code"), "is_product": p.is_product,
                             "description": rec.get("description"), "quantity": p.quantity,
                             "invoice_ts": p.invoice_ts, "unit_price": p.unit_price,
                             "line_amount_cents": p.amount_cents, "currency": CURRENCY,
                             "customer_id": p.customer_id, "country": rec.get("country"),
                             "line_class": p.line_class, "invalid_reason": p.invalid_reason,
                             "missing_customer": p.customer_id is None,
                             "missing_description": rec.get("description") is None,
                             "zero_price": p.unit_price is not None and Decimal(p.unit_price) == 0,
                             "sub_penny": p.sub_penny, "possible_duplicate": False, "overlap_excluded": False,
                             "overlap_conflict": False, "load_id": load_id})
            if len(line_buf) >= chunk_size:
                t_parse += time.perf_counter() - t0
                flush()
                log(f"  {sheet}: {rows_read:,} rows")
                t0 = time.perf_counter()
        t_parse += time.perf_counter() - t0
    if line_buf:
        flush()

    t0 = time.perf_counter()
    validate(db, load_id)
    t_val = time.perf_counter() - t0
    loaded = db.scalar("SELECT COUNT(*) FROM retail_lines")
    total = t_parse + t_load + t_val
    db.execute("UPDATE retail_load_runs SET finished_at = ?, status = ?, rows_read = ?, rows_loaded = ?, "
               "seconds_read_parse = ?, seconds_load = ?, seconds_validate = ?, rows_per_second = ? WHERE load_id = ?",
               (utcnow(), "completed" if loaded == rows_read else "incomplete", rows_read, loaded,
                round(t_parse, 2), round(t_load, 2), round(t_val, 2),
                round(rows_read / total, 1) if total else None, load_id))
    db.commit()
    return {"load_id": load_id, "rows_read": rows_read, "rows_loaded": loaded,
            "seconds": {"read_parse": round(t_parse, 2), "load": round(t_load, 2), "validate": round(t_val, 2)}}


# ----------------------------------------------------------------------- validation (SQL)
def validate(db: Database, load_id: str) -> None:
    # 1. The file layout repeats some invoices in more than one sheet. An identical
    #    copy is a layout duplicate (excluded from totals); a different copy is a
    #    conflict (kept, flagged).
    multi = db.query(
        "SELECT invoice_raw, MIN(sheet_order) AS first_sheet FROM retail_raw_lines WHERE invoice_raw IS NOT NULL "
        "GROUP BY invoice_raw HAVING COUNT(DISTINCT sheet_order) > 1")
    for m in multi:
        rows = db.query("SELECT sheet_order, row_hash FROM retail_raw_lines WHERE invoice_raw = ?", (m["invoice_raw"],))
        by_sheet: dict[int, list[str]] = {}
        for r in rows:
            by_sheet.setdefault(r["sheet_order"], []).append(r["row_hash"])
        first = sorted(by_sheet[m["first_sheet"]])
        if all(sorted(v) == first for v in by_sheet.values()):
            db.execute("UPDATE retail_lines SET overlap_excluded = ? WHERE invoice = ? AND sheet_order > ?",
                       (True, m["invoice_raw"], m["first_sheet"]))
        else:
            db.execute("UPDATE retail_lines SET overlap_conflict = ? WHERE invoice = ?", (True, m["invoice_raw"]))

    # 2. Identical lines repeated within one sheet: flagged, NOT deleted. The same
    #    item can be keyed twice on one invoice; deleting would understate revenue.
    db.execute(
        "UPDATE retail_lines SET possible_duplicate = ? WHERE source_row_id IN ("
        " SELECT r.source_row_id FROM retail_raw_lines r JOIN ("
        "  SELECT sheet, row_hash FROM retail_raw_lines GROUP BY sheet, row_hash HAVING COUNT(*) > 1"
        " ) d ON d.sheet = r.sheet AND d.row_hash = r.row_hash)", (True,))

    # 3. Cancellation lines: is there an earlier sale this cancellation can refer to?
    #    The data has no link from "C" invoices to originals, so this is the
    #    "exists in one place but not the other" question on real data.
    db.execute(
        "INSERT INTO retail_cancellation_links (source_row_id, status, load_id) "
        "SELECT c.source_row_id, "
        "  CASE WHEN c.customer_id IS NULL THEN 'MISSING_CUSTOMER' "
        "       WHEN EXISTS (SELECT 1 FROM retail_lines s WHERE s.line_class = 'VALID_SALE' "
        "                     AND NOT s.overlap_excluded AND s.customer_id = c.customer_id "
        "                     AND s.stock_code = c.stock_code AND s.invoice_ts <= c.invoice_ts "
        "                     AND s.quantity >= -c.quantity) THEN 'LINKED' "
        "       ELSE 'NO_PRIOR_SALE' END, ? "
        "FROM retail_lines c WHERE c.line_class = 'CANCELLATION' AND NOT c.overlap_excluded", (load_id,))

    # 4. Invoice-level aggregation of canonical lines.
    db.execute(
        "INSERT INTO retail_invoices (invoice, invoice_type, customer_id, customer_count, country, first_ts, "
        " line_count, product_lines, amount_cents, currency, load_id) "
        "SELECT invoice, MIN(invoice_type), MIN(customer_id), COUNT(DISTINCT customer_id), MIN(country), "
        " MIN(invoice_ts), COUNT(*), SUM(CASE WHEN is_product THEN 1 ELSE 0 END), "
        " COALESCE(SUM(line_amount_cents), 0), MIN(currency), ? "
        "FROM retail_lines WHERE invoice IS NOT NULL AND NOT overlap_excluded GROUP BY invoice", (load_id,))
    db.commit()
    _quality(db, load_id)


def _quality(db: Database, load_id: str) -> None:
    db.execute("DELETE FROM retail_quality_results WHERE load_id = ?", (load_id,))
    q = db.query
    total_rows = db.scalar("SELECT COUNT(*) FROM retail_lines")
    canon = "NOT overlap_excluded"
    canon_rows = db.scalar(f"SELECT COUNT(*) FROM retail_lines WHERE {canon}") or 0
    results = []

    def add(name, rows, amount, passed, severity, detail, denom=None):
        d = denom if denom is not None else canon_rows
        results.append({"load_id": load_id, "check_name": name, "rows_affected": rows, "amount_cents": amount,
                        "share": round(rows / d, 6) if d and rows is not None else None, "passed": passed,
                        "severity": "info" if passed else severity, "detail": detail})

    raw_rows = db.scalar("SELECT COUNT(*) FROM retail_raw_lines")
    add("control_row_count", total_rows, None, raw_rows == total_rows, "error",
        f"{raw_rows:,} raw rows read, {total_rows:,} parsed rows: nothing dropped" if raw_rows == total_rows
        else f"{raw_rows:,} raw rows but {total_rows:,} parsed rows", denom=raw_rows)

    by_class = {r["line_class"]: r for r in q(
        f"SELECT line_class, COUNT(*) AS n, COALESCE(SUM(line_amount_cents), 0) AS cents FROM retail_lines "
        f"WHERE {canon} GROUP BY line_class")}
    class_total = sum(r["cents"] for r in by_class.values())
    line_total = db.scalar(f"SELECT COALESCE(SUM(line_amount_cents), 0) FROM retail_lines WHERE {canon}")
    inv_total = db.scalar("SELECT COALESCE(SUM(amount_cents), 0) FROM retail_invoices")
    no_invoice = db.scalar(f"SELECT COALESCE(SUM(line_amount_cents), 0) FROM retail_lines WHERE {canon} AND invoice IS NULL")
    add("control_amount_totals", canon_rows, line_total, class_total == line_total == inv_total + no_invoice, "error",
        f"lines {line_total:,}p = sum of classes {class_total:,}p = invoices {inv_total:,}p"
        + (f" + {no_invoice:,}p on lines with no invoice number" if no_invoice else ""))

    for cls, sev, text in (
            ("VALID_SALE", None, "Product sale lines"),
            ("CANCELLATION", "info", "Lines on cancellation invoices (C...)"),
            ("ADJUSTMENT", "warning", "Bad-debt / accounting adjustments (A...)"),
            ("NON_PRODUCT", "info", "Postage, fees, discounts and manual lines (non-product stock codes)"),
            ("STOCK_ADJUSTMENT", "warning", "Negative quantity on a sale invoice: write-offs, damages, stock fixes"),
            ("INVALID", "error", "Unparseable or impossible values")):
        r = by_class.get(cls, {"n": 0, "cents": 0})
        add(f"class_{cls.lower()}", r["n"], r["cents"], sev is None or r["n"] == 0 or sev == "info", sev or "info", text)

    def flag(name, col, severity, text):
        r = q(f"SELECT COUNT(*) AS n, COALESCE(SUM(line_amount_cents), 0) AS cents FROM retail_lines "
              f"WHERE {col} AND {canon}")[0]
        add(name, r["n"], r["cents"], r["n"] == 0, severity, text)

    flag("missing_customer_id", "missing_customer", "warning",
         "No customer ID: kept in revenue totals, excluded from customer-level analysis")
    flag("zero_price", "zero_price", "warning", "Unit price of zero")
    flag("missing_description", "missing_description", "warning", "No product description")
    flag("sub_penny_amounts", "sub_penny", "warning", "Quantity x price has fractions of a penny; rounded half-up")
    flag("possible_duplicate_lines", "possible_duplicate", "warning",
         "Identical lines repeated within a sheet. Flagged, not deleted: the same item can be keyed twice")
    r = q("SELECT COUNT(*) AS n, COALESCE(SUM(line_amount_cents), 0) AS cents FROM retail_lines WHERE overlap_excluded")[0]
    add("sheet_overlap_excluded", r["n"], r["cents"], True, "info",
        "Identical copies of invoices already read from an earlier sheet; excluded so they are not counted twice",
        denom=total_rows)
    r = q("SELECT COUNT(*) AS n, COUNT(DISTINCT invoice) AS inv FROM retail_lines WHERE overlap_conflict")[0]
    add("sheet_overlap_conflicts", r["n"], None, r["n"] == 0, "error",
        f"{r['inv']} invoice(s) appear in several sheets with different lines; kept and flagged for review")
    n = db.scalar("SELECT COUNT(*) FROM retail_invoices WHERE customer_count > 1")
    add("invoice_customer_consistency", n, None, n == 0, "error", "Invoices whose lines name different customers",
        denom=db.scalar("SELECT COUNT(*) FROM retail_invoices"))
    links = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) AS n FROM retail_cancellation_links GROUP BY status")}
    c_total = sum(links.values())
    add("cancellations_without_prior_sale", links.get("NO_PRIOR_SALE", 0), None, links.get("NO_PRIOR_SALE", 0) == 0,
        "warning", f"{links.get('LINKED', 0):,} linked to an earlier sale of the same item to the same customer; "
        f"{links.get('NO_PRIOR_SALE', 0):,} have no earlier sale in the data (often sold before the data starts); "
        f"{links.get('MISSING_CUSTOMER', 0):,} cannot be checked (no customer ID)", denom=c_total)
    db.upsert_many("retail_quality_results", results, ["load_id", "check_name"])
    db.commit()


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="uci_retail")
    p.add_argument("cmd", choices=["download", "load", "all"])
    p.add_argument("--file", type=Path, default=DEFAULT_PATH)
    p.add_argument("--chunk-size", type=int, default=CHUNK_SIZE)
    a = p.parse_args(argv)
    if a.cmd in ("download", "all"):
        print(f"downloading {DOWNLOAD_URL}")
        download(a.file)
    if a.cmd in ("load", "all"):
        db = Database(get_settings().database_url)
        print(load(db, a.file, a.chunk_size))
        for r in db.query("SELECT check_name, rows_affected, passed, detail FROM retail_quality_results "
                          "ORDER BY check_name"):
            print(f"  {'ok ' if r['passed'] else '!! '} {r['check_name']:<34} {r['rows_affected']!s:>9}  {r['detail']}")


if __name__ == "__main__":
    main()

import math
import sqlite3

import pytest

import main_app as app_module
from i18n import t
from main_app import (
    MainApp,
    _apply_payment,
    _parse_finite_number,
    _validate_discount_input,
    _validate_document_date,
    _validate_line_inputs,
    _validate_payment_amount,
    _validate_percentage,
    _validate_quantity,
    _validate_unit_price,
)


class FakeMessagebox:
    def __init__(self):
        self.errors = []
        self.infos = []

    def showerror(self, title, message):
        self.errors.append(message)

    def showinfo(self, title, message):
        self.infos.append(message)


def _make_app(db_path):
    app = MainApp.__new__(MainApp)
    app.db_path = str(db_path)
    app.init_db()
    return app


def _insert_client(conn, name):
    return conn.execute("INSERT INTO clients (nom) VALUES (?)", (name,)).lastrowid


def _insert_document(
    conn, doc_type, numero, client_id, total, paid=0.0, statut="brouillon"
):
    return conn.execute(
        """
        INSERT INTO documents (
            type, numero, date, client_id, statut, total_ht, total_tva,
            total_ttc, paid_amount, payment_mode
        ) VALUES (?, ?, '2026-01-01 00:00:00', ?, ?, ?, 0, ?, ?, 'none')
        """,
        (doc_type, numero, client_id, statut, total, total, paid),
    ).lastrowid


def _payment_count(conn, document_id):
    return conn.execute(
        "SELECT COUNT(*) FROM payments WHERE document_id = ?", (document_id,)
    ).fetchone()[0]


@pytest.mark.parametrize("raw", [None, "", "abc", "nan", "NaN", "inf", "-inf", object()])
def test_parse_finite_number_rejects_invalid_values(raw):
    assert _parse_finite_number(raw) is None


@pytest.mark.parametrize("raw, expected", [("1.5", 1.5), ("-2", -2.0), (0, 0.0), (" 3 ", 3.0)])
def test_parse_finite_number_accepts_finite_values(raw, expected):
    assert _parse_finite_number(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "0", "-1", "nan", "inf", "-inf"])
def test_validate_quantity_requires_positive_finite_number(raw):
    assert _validate_quantity(raw) is None


def test_validate_quantity_accepts_positive_fraction():
    assert _validate_quantity("2.5") == 2.5


@pytest.mark.parametrize("raw", [None, "", "-0.01", "nan", "inf", "-inf"])
def test_validate_unit_price_rejects_invalid_values(raw):
    assert _validate_unit_price(raw) is None


@pytest.mark.parametrize("raw, expected", [("0", 0), ("12.5", 1250), ("1.005", 101)])
def test_validate_unit_price_accepts_zero_and_positive(raw, expected):
    assert _validate_unit_price(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "-1", "100.01", "nan", "inf", "-inf"])
def test_validate_percentage_rejects_out_of_range_values(raw):
    assert _validate_percentage(raw) is None


@pytest.mark.parametrize("raw, expected", [("0", 0.0), ("100", 100.0), ("20", 20.0)])
def test_validate_percentage_accepts_range(raw, expected):
    assert _validate_percentage(raw) == expected


def test_validate_line_inputs_returns_values_and_specific_errors():
    assert _validate_line_inputs("2", "10", "20") == ((2.0, 1000, 20.0), None)
    assert _validate_line_inputs("0", "10", "20") == (
        None,
        "new_doc.error_qty_invalid",
    )
    assert _validate_line_inputs("1", "-1", "20") == (
        None,
        "new_doc.error_pu_invalid",
    )
    assert _validate_line_inputs("1", "10", "101") == (
        None,
        "new_doc.error_tva_invalid",
    )


def test_validate_discount_input_handles_disabled_and_invalid_values():
    assert _validate_discount_input(False, "not a number") == (0.0, None)
    assert _validate_discount_input(True, "") == (0.0, None)
    assert _validate_discount_input(True, "15") == (15.0, None)
    assert _validate_discount_input(True, "-1") == (
        None,
        "new_doc.error_discount_invalid",
    )
    assert _validate_discount_input(True, "100.1") == (
        None,
        "new_doc.error_discount_invalid",
    )


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-01-31", "2026-01-31 00:00:00"),
        ("2026-01-31 14:05:06", "2026-01-31 14:05:06"),
    ],
)
def test_validate_document_date_accepts_supported_formats(raw, expected):
    assert _validate_document_date(raw) == expected


@pytest.mark.parametrize(
    "raw", [None, "", "31/01/2026", "2026-13-01", "2026-01-32", "not a date"]
)
def test_validate_document_date_rejects_invalid_values(raw):
    assert _validate_document_date(raw) is None


@pytest.mark.parametrize(
    "raw, remaining, expected",
    [
        ("10", 10000, 1000),
        ("100", 10000, 10000),
        ("0", 10000, None),
        ("-5", 10000, None),
        ("100.01", 10000, None),
        ("nan", 10000, None),
        ("inf", 10000, None),
        ("10", 0, None),
        ("10", None, 1000),
    ],
)
def test_validate_payment_amount(raw, remaining, expected):
    assert _validate_payment_amount(raw, remaining) == expected


def test_apply_payment_records_exact_amount_and_marks_full(tmp_path):
    app = _make_app(tmp_path / "commercial.db")
    conn = sqlite3.connect(app.db_path)
    try:
        client_id = _insert_client(conn, "Acme")
        invoice_id = _insert_document(
            conn, "factures", "FAC-1", client_id, 10000, 4000
        )
        conn.commit()

        cursor = conn.cursor()
        assert _apply_payment(cursor, invoice_id, "60", "cash") == (None, {})
        conn.commit()

        paid, mode = conn.execute(
            "SELECT paid_amount, payment_mode FROM documents WHERE id = ?",
            (invoice_id,),
        ).fetchone()
        amount = conn.execute(
            "SELECT amount FROM payments WHERE document_id = ?", (invoice_id,)
        ).fetchone()[0]
    finally:
        conn.close()

    assert paid == 10000
    assert mode == "full"
    assert amount == 6000


@pytest.mark.parametrize("raw", ["0", "-10", "nan", "inf", "-inf", "abc"])
def test_apply_payment_rejects_non_positive_or_non_finite_amount(tmp_path, raw):
    app = _make_app(tmp_path / "commercial.db")
    conn = sqlite3.connect(app.db_path)
    try:
        client_id = _insert_client(conn, "Acme")
        invoice_id = _insert_document(
            conn, "factures", "FAC-1", client_id, 10000, 2000
        )
        conn.commit()

        cursor = conn.cursor()
        error_key, _kwargs = _apply_payment(cursor, invoice_id, raw)
        conn.commit()

        paid = conn.execute(
            "SELECT paid_amount FROM documents WHERE id = ?", (invoice_id,)
        ).fetchone()[0]
        count = _payment_count(conn, invoice_id)
    finally:
        conn.close()

    assert error_key in {
        "payment_dialog.error_invalid_amount",
        "payment_dialog.error_amount_positive",
    }
    assert paid == 2000
    assert count == 0


def test_apply_payment_rejects_overpayment_without_writing(tmp_path):
    app = _make_app(tmp_path / "commercial.db")
    conn = sqlite3.connect(app.db_path)
    try:
        client_id = _insert_client(conn, "Acme")
        invoice_id = _insert_document(
            conn, "factures", "FAC-1", client_id, 10000, 4000
        )
        conn.commit()

        cursor = conn.cursor()
        error_key, kwargs = _apply_payment(cursor, invoice_id, "60.01")
        conn.commit()

        paid = conn.execute(
            "SELECT paid_amount FROM documents WHERE id = ?", (invoice_id,)
        ).fetchone()[0]
        count = _payment_count(conn, invoice_id)
    finally:
        conn.close()

    assert error_key == "payment_dialog.error_amount_exceeds_remaining"
    assert kwargs["remaining"] == pytest.approx(60.0)
    assert paid == 4000
    assert count == 0


def test_apply_payment_rejects_paid_invoice_and_non_invoice(tmp_path):
    app = _make_app(tmp_path / "commercial.db")
    conn = sqlite3.connect(app.db_path)
    try:
        client_id = _insert_client(conn, "Acme")
        paid_invoice = _insert_document(
            conn, "factures", "FAC-1", client_id, 10000, 10000
        )
        devis_id = _insert_document(conn, "devis", "DEV-1", client_id, 10000)
        conn.commit()

        cursor = conn.cursor()
        paid_error, _ = _apply_payment(cursor, paid_invoice, "10")
        devis_error, _ = _apply_payment(cursor, devis_id, "10")
        missing_error, _ = _apply_payment(cursor, 99999, "10")
        conn.rollback()
    finally:
        conn.close()

    assert paid_error == "payment_dialog.error_nothing_due"
    assert devis_error == "payment_dialog.error_not_invoice"
    assert missing_error == "payment_dialog.error_doc_not_found"


def test_payment_dialog_rejects_non_invoice_before_opening_ui(tmp_path, monkeypatch):
    app = _make_app(tmp_path / "commercial.db")
    messages = FakeMessagebox()
    monkeypatch.setattr(app_module, "messagebox", messages)

    conn = sqlite3.connect(app.db_path)
    try:
        client_id = _insert_client(conn, "Acme")
        devis_id = _insert_document(conn, "devis", "DEV-1", client_id, 10000)
        conn.commit()
    finally:
        conn.close()

    app.show_add_payment_dialog_for(devis_id)

    assert messages.errors == [t("payment_dialog.error_not_invoice")]

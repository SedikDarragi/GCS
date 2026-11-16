import sqlite3
from decimal import Decimal

import pytest

from money import (
    as_cents,
    discount_cents,
    format_money,
    line_totals,
    migrate_money_to_cents,
    money_to_input,
    parse_money,
)

INVALID_MONEY_VALUES = [None, "", "abc", "nan", "NaN", "inf", "-inf", object(), True]


@pytest.mark.parametrize("raw", INVALID_MONEY_VALUES)
def test_parse_money_rejects_invalid_values(raw):
    assert parse_money(raw) is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0", 0),
        ("10", 1000),
        ("12.34", 1234),
        ("-2.5", -250),
        (0.1, 10),
        (Decimal("1.005"), 101),
        (" 3.999 ", 400),
    ],
)
def test_parse_money_rounds_half_up_to_cents(raw, expected):
    assert parse_money(raw) == expected


def test_as_cents_normalizes_stored_values():
    assert as_cents(None) == 0
    assert as_cents(1234) == 1234
    assert as_cents("12.6") == 13
    assert as_cents(Decimal("12.4")) == 12
    assert as_cents(object()) == 0


def test_format_money_and_money_to_input():
    assert format_money(1234) == "12.34"
    assert format_money(-250) == "-2.50"
    assert format_money(None) == "0.00"
    assert money_to_input(10000) == "100.00"


def test_line_totals_rounds_each_amount_to_cents():
    assert line_totals(2, 1050, 20) == (2100, 420, 2520)
    assert line_totals(1.5, 999, 20) == (1499, 300, 1799)
    assert line_totals(1, 0, 19) == (0, 0, 0)


def test_line_totals_rejects_non_finite_input():
    with pytest.raises(ValueError):
        line_totals("nan", 100, 20)


def test_discount_cents_rounds_half_up():
    assert discount_cents(2100, 12.5) == 263
    assert discount_cents(100, 0) == 0
    with pytest.raises(ValueError):
        discount_cents(100, "inf")


def _create_legacy_database(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE articles ("
            "id INTEGER PRIMARY KEY, prix_achat REAL, prix_vente REAL, "
            "tva REAL, quantite REAL)"
        )
        conn.execute(
            "CREATE TABLE documents ("
            "id INTEGER PRIMARY KEY, total_ht REAL, total_tva REAL, "
            "total_ttc REAL, paid_amount REAL, discount_value REAL)"
        )
        conn.execute(
            "CREATE TABLE payments (id INTEGER PRIMARY KEY, amount REAL)"
        )
        conn.execute(
            "INSERT INTO articles VALUES (1, 1.23, 4.56, 20, 2.5)"
        )
        conn.execute(
            "INSERT INTO documents VALUES (1, 10.00, 2.00, 12.00, 5.00, 10.0)"
        )
        conn.execute("INSERT INTO payments VALUES (1, 5.00)")
        conn.commit()
    finally:
        conn.close()


def test_migration_converts_currency_columns_to_cents(tmp_path):
    path = tmp_path / "legacy.db"
    _create_legacy_database(path)

    conn = sqlite3.connect(path)
    try:
        assert migrate_money_to_cents(conn) is True
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        article = conn.execute(
            "SELECT prix_achat, prix_vente, tva, quantite FROM articles"
        ).fetchone()
        document = conn.execute(
            "SELECT total_ht, total_tva, total_ttc, paid_amount, discount_value "
            "FROM documents"
        ).fetchone()
        amount = conn.execute("SELECT amount FROM payments").fetchone()[0]
    finally:
        conn.close()

    assert version == 1
    assert article == (123, 456, 20, 2.5)
    assert document == (1000, 200, 1200, 500, 10.0)
    assert amount == 500


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    _create_legacy_database(path)

    conn = sqlite3.connect(path)
    try:
        assert migrate_money_to_cents(conn) is True
        assert migrate_money_to_cents(conn) is False
        value = conn.execute("SELECT prix_vente FROM articles").fetchone()[0]
    finally:
        conn.close()

    assert value == 456


def test_migration_leaves_integer_columns_untouched(tmp_path):
    path = tmp_path / "fresh.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE articles (id INTEGER PRIMARY KEY, prix_vente INTEGER)"
        )
        conn.execute("INSERT INTO articles VALUES (1, 1234)")
        conn.commit()

        assert migrate_money_to_cents(conn) is False
        assert conn.execute("SELECT prix_vente FROM articles").fetchone()[0] == 1234
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        conn.close()

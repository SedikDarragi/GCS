"""Money helpers shared by the application and the database maintenance code.

Monetary values are stored in SQLite as integer cents.  Parsing, rounding and
formatting all go through :class:`~decimal.Decimal` so no binary floating
point error can leak into an invoice total, a payment balance or an export.

The module deliberately has no UI import so it can be used by
``main_app.py`` as well as by ``database_maintenance.py``.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

SCHEMA_VERSION = 1

#: Tables and columns that hold an amount of money.  Percentages (``tva``,
#: ``discount_value``) and quantities are intentionally absent.
MONEY_COLUMNS = {
    "articles": ("prix_achat", "prix_vente"),
    "documents": ("total_ht", "total_tva", "total_ttc", "paid_amount"),
    "document_lignes": (
        "prix_unitaire",
        "total_ht",
        "total_tva",
        "total_ttc",
    ),
    "payments": ("amount",),
    # Legacy tables created by ``main.py``.
    "products": ("price",),
    "sales": ("total_price",),
}

#: SQLite declared types that used to hold currency units before the switch to
#: integer cents.  Only those columns are converted by the migration.
_CURRENCY_TYPES = ("REAL", "FLOAT", "DOUBLE", "NUMERIC", "DECIMAL")

_CENT_FACTOR = Decimal(100)
_HUNDRED = Decimal(100)


def _to_decimal(value):
    """Return ``value`` as a finite Decimal, or ``None`` when it is not one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite():
        return None
    return number


def parse_money(raw):
    """Parse a user supplied amount and return it as integer cents.

    Returns ``None`` for anything that is not a finite number.  Rounding is
    half-up, which matches how invoices are normally rounded.
    """
    value = _to_decimal(raw)
    if value is None:
        return None
    return int((value * _CENT_FACTOR).to_integral_value(rounding=ROUND_HALF_UP))


def as_cents(value):
    """Normalize a value already stored in cents to a plain integer."""
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    number = _to_decimal(value)
    if number is None:
        return 0
    return int(number.to_integral_value(rounding=ROUND_HALF_UP))


def format_money(cents):
    """Format integer cents as a plain ``1234.56`` string."""
    amount = Decimal(as_cents(cents)) / _CENT_FACTOR
    return f"{amount:.2f}"


def money_to_input(cents):
    """Format integer cents for an editable amount field."""
    return format_money(cents)


def line_totals(quantity, unit_price_cents, vat_percent):
    """Return ``(ht, tva, ttc)`` cents for a document line.

    ``quantity`` and ``vat_percent`` may be floats or Decimal values; the unit
    price must already be in cents.
    """
    qty = _to_decimal(quantity)
    vat = _to_decimal(vat_percent)
    if qty is None or vat is None:
        raise ValueError("quantity and VAT must be finite numbers")

    unit_price = Decimal(as_cents(unit_price_cents))
    total_ht = (qty * unit_price).to_integral_value(rounding=ROUND_HALF_UP)
    total_tva = (total_ht * vat / _HUNDRED).to_integral_value(
        rounding=ROUND_HALF_UP
    )
    return int(total_ht), int(total_tva), int(total_ht + total_tva)


def discount_cents(total_ht_cents, percent):
    """Return the discount amount in cents for ``percent`` of ``total_ht``."""
    value = _to_decimal(percent)
    if value is None:
        raise ValueError("discount must be a finite number")
    amount = Decimal(as_cents(total_ht_cents)) * value / _HUNDRED
    return int(amount.to_integral_value(rounding=ROUND_HALF_UP))


def _declared_types(conn, table):
    return {
        row[1]: (row[2] or "").upper()
        for row in conn.execute(f'PRAGMA table_info("{table}")')
    }


def _is_currency_type(declared_type):
    return any(token in declared_type for token in _CURRENCY_TYPES)


def migrate_money_to_cents(conn):
    """Convert a legacy currency-unit database to integer cents.

    ``PRAGMA user_version`` records that the migration has run.  The function
    is also safe on a fresh database: columns already declared as ``INTEGER``
    are left untouched.  It returns ``True`` when at least one column was
    converted.
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    if version >= SCHEMA_VERSION:
        return False

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    migrated = False

    for table, columns in MONEY_COLUMNS.items():
        if table not in tables:
            continue
        declared = _declared_types(conn, table)
        for column in columns:
            if column not in declared or not _is_currency_type(declared[column]):
                continue
            conn.execute(
                f'UPDATE "{table}" '
                f'SET "{column}" = CAST(ROUND('
                f'COALESCE(CAST("{column}" AS REAL), 0) * 100'
                f") AS INTEGER)"
            )
            migrated = True

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return migrated

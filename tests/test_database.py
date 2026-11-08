import sqlite3
from datetime import datetime

from main_app import MainApp

EXPECTED_TABLES = {
    "articles",
    "clients",
    "documents",
    "document_lignes",
    "payments",
    "sequences",
}


def _make_app(db_path):
    app = MainApp.__new__(MainApp)
    app.db_path = str(db_path)
    return app


def _table_names(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def test_init_db_creates_schema(tmp_path):
    app = _make_app(tmp_path / "commercial.db")
    app.init_db()
    assert EXPECTED_TABLES <= _table_names(app.db_path)


def test_init_db_is_idempotent(tmp_path):
    app = _make_app(tmp_path / "commercial.db")
    app.init_db()
    app.init_db()
    assert EXPECTED_TABLES <= _table_names(app.db_path)


def test_init_db_preserves_existing_data(tmp_path):
    db = tmp_path / "commercial.db"
    app = _make_app(db)
    app.init_db()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO articles (designation, prix_achat, prix_vente, tva, stock) "
            "VALUES ('Test', 1, 2, 19, 3)"
        )
        conn.commit()
    finally:
        conn.close()
    app.init_db()
    conn = sqlite3.connect(str(db))
    try:
        count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_get_next_numero_increments(tmp_path):
    app = _make_app(tmp_path / "commercial.db")
    app.init_db()
    year = datetime.now().year
    assert app.get_next_numero("devis") == f"DEV-{year}-0001"
    assert app.get_next_numero("devis") == f"DEV-{year}-0002"


def test_get_next_numero_prefixes(tmp_path):
    app = _make_app(tmp_path / "commercial.db")
    app.init_db()
    year = datetime.now().year
    assert app.get_next_numero("bl") == f"BL-{year}-0001"
    assert app.get_next_numero("factures") == f"FAC-{year}-0001"
    assert app.get_next_numero("reception") == f"BR-{year}-0001"


def test_get_next_numero_types_are_independent(tmp_path):
    app = _make_app(tmp_path / "commercial.db")
    app.init_db()
    year = datetime.now().year
    app.get_next_numero("devis")
    assert app.get_next_numero("bl") == f"BL-{year}-0001"


def test_get_next_numero_resets_on_new_year(tmp_path):
    app = _make_app(tmp_path / "commercial.db")
    app.init_db()
    year = datetime.now().year
    app.get_next_numero("devis")
    app.get_next_numero("devis")
    conn = sqlite3.connect(app.db_path)
    try:
        conn.execute(
            "UPDATE sequences SET annee = ?, compteur = 5 WHERE type = 'devis'",
            (year - 1,),
        )
        conn.commit()
    finally:
        conn.close()
    assert app.get_next_numero("devis") == f"DEV-{year}-0001"


def _insert_document(db_path, doc_type, numero):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO documents (type, numero, date) VALUES (?, ?, ?)",
            (doc_type, numero, f"{datetime.now().year}-01-01 00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def test_peek_next_numero_does_not_reserve(tmp_path):
    app = _make_app(tmp_path / "commercial.db")
    app.init_db()
    year = datetime.now().year

    assert app.peek_next_numero("devis") == f"DEV-{year}-0001"
    assert app.peek_next_numero("devis") == f"DEV-{year}-0001"
    assert app.get_next_numero("devis") == f"DEV-{year}-0001"
    assert app.peek_next_numero("devis") == f"DEV-{year}-0002"


def test_get_next_numero_reconciles_with_existing_documents(tmp_path):
    app = _make_app(tmp_path / "commercial.db")
    app.init_db()
    year = datetime.now().year
    _insert_document(app.db_path, "devis", f"DEV-{year}-0003")
    _insert_document(app.db_path, "devis", f"DEV-{year}-0007")

    assert app.peek_next_numero("devis") == f"DEV-{year}-0008"
    assert app.get_next_numero("devis") == f"DEV-{year}-0008"
    assert app.peek_next_numero("devis") == f"DEV-{year}-0009"


def test_get_next_numero_ignores_previous_year_documents(tmp_path):
    app = _make_app(tmp_path / "commercial.db")
    app.init_db()
    year = datetime.now().year
    _insert_document(app.db_path, "devis", f"DEV-{year - 1}-0042")

    assert app.peek_next_numero("devis") == f"DEV-{year}-0001"


def test_get_next_numero_with_cursor_obeys_caller_transaction(tmp_path):
    app = _make_app(tmp_path / "commercial.db")
    app.init_db()
    year = datetime.now().year

    conn = sqlite3.connect(app.db_path)
    try:
        cursor = conn.cursor()
        assert app.get_next_numero("devis", cursor=cursor) == f"DEV-{year}-0001"
        conn.rollback()
    finally:
        conn.close()

    assert app.peek_next_numero("devis") == f"DEV-{year}-0001"


def test_numero_exists_ignores_the_edited_document(tmp_path):
    from main_app import _numero_exists

    app = _make_app(tmp_path / "commercial.db")
    app.init_db()
    year = datetime.now().year
    numero = f"DEV-{year}-0001"
    _insert_document(app.db_path, "devis", numero)

    conn = sqlite3.connect(app.db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM documents WHERE numero = ?", (numero,))
        doc_id = cursor.fetchone()[0]

        assert _numero_exists(cursor, numero) is True
        assert _numero_exists(cursor, numero.lower()) is True
        assert _numero_exists(cursor, numero, exclude_id=doc_id) is False
        assert _numero_exists(cursor, f"DEV-{year}-0099") is False
    finally:
        conn.close()

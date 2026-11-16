import sqlite3

import pytest

import main_app as app_module
from i18n import t
from main_app import MainApp


class FakeTree:
    def __init__(self, selected_id=None):
        self.selected_id = selected_id

    def selection(self):
        return ("item-1",) if self.selected_id is not None else ()

    def item(self, item_id, option=None):
        if option == "values":
            return (str(self.selected_id),)
        return ()


class RecordingTree:
    def __init__(self):
        self.rows = []

    def get_children(self):
        return ()

    def delete(self, item):
        return None

    def insert(self, parent, index, values=(), tags=()):
        self.rows.append(values)


class FakeMessagebox:
    def __init__(self):
        self.errors = []
        self.infos = []

    def showerror(self, title, message):
        self.errors.append(message)

    def showinfo(self, title, message):
        self.infos.append(message)

    def askyesno(self, title, message):
        return True


def _make_app(db_path):
    app = MainApp.__new__(MainApp)
    app.db_path = str(db_path)
    app.init_db()
    return app


def _disable_ui_refresh(app):
    app.load_documents = lambda doc_type: None
    app.load_paiements = lambda: None


def _insert_client(conn, name="Acme"):
    cur = conn.execute("INSERT INTO clients (nom) VALUES (?)", (name,))
    return cur.lastrowid


def _insert_document(
    conn,
    doc_type,
    numero,
    client_id,
    total,
    paid=0.0,
    **fields,
):
    values = {
        "type": doc_type,
        "numero": numero,
        "date": "2026-01-01 00:00:00",
        "client_id": client_id,
        "statut": "brouillon",
        "total_ht": total,
        "total_tva": 0,
        "total_ttc": total,
        "paid_amount": paid,
        "payment_mode": "none",
        "echeance": None,
        "discount_enabled": 0,
        "discount_value": 0,
    }
    values.update(fields)
    cur = conn.execute(
        """
        INSERT INTO documents (
            type, numero, date, client_id, statut, total_ht, total_tva,
            total_ttc, paid_amount, payment_mode, echeance,
            discount_enabled, discount_value
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            values["type"],
            values["numero"],
            values["date"],
            values["client_id"],
            values["statut"],
            values["total_ht"],
            values["total_tva"],
            values["total_ttc"],
            values["paid_amount"],
            values["payment_mode"],
            values["echeance"],
            values["discount_enabled"],
            values["discount_value"],
        ),
    )
    return cur.lastrowid


def _insert_line(conn, document_id, total):
    conn.execute(
        """
        INSERT INTO document_lignes (
            document_id, designation, quantite, prix_unitaire, tva,
            total_ht, total_tva, total_ttc
        ) VALUES (?, 'Line', 1, ?, 0, ?, 0, ?)
        """,
        (document_id, total, total, total),
    )


def test_init_db_adds_conversion_tracking_columns(tmp_path):
    app = _make_app(tmp_path / "commercial.db")
    conn = sqlite3.connect(app.db_path)
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()
        }
    finally:
        conn.close()

    assert {"converted_to_id", "converted_at"} <= columns


def test_init_db_migrates_existing_documents_table(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                numero TEXT NOT NULL,
                date TEXT NOT NULL,
                client_id INTEGER,
                statut TEXT NOT NULL DEFAULT 'brouillon'
            )
            """
        )
        conn.execute(
            "INSERT INTO documents (type, numero, date, statut) "
            "VALUES ('devis', 'DEV-OLD', '2025-01-01', 'valide')"
        )
        conn.commit()
    finally:
        conn.close()

    app = _make_app(db_path)

    conn = sqlite3.connect(app.db_path)
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()
        }
        count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    finally:
        conn.close()

    assert {"converted_to_id", "converted_at"} <= columns
    assert count == 1



def test_outstanding_counts_only_invoices(tmp_path):
    app = _make_app(tmp_path / "commercial.db")
    conn = sqlite3.connect(app.db_path)
    try:
        client_id = _insert_client(conn)
        _insert_document(conn, "devis", "DEV-1", client_id, 100000, paid=0)
        _insert_document(conn, "bl", "BL-1", client_id, 50000, paid=10000)
        _insert_document(conn, "factures", "FAC-1", client_id, 30000, paid=10000)
        # Overpayments must not reduce other invoices.
        _insert_document(conn, "factures", "FAC-2", client_id, 5000, paid=20000)
        conn.commit()
    finally:
        conn.close()

    app.clients_unpaid_tree = RecordingTree()
    app.client_docs_tree = RecordingTree()
    app.ui = {"tree_even": "", "tree_odd": ""}
    app._apply_zebra = lambda tree: None

    app.load_paiements()

    assert app.clients_unpaid_tree.rows == [(client_id, "Acme", "200.00")]


def test_conversion_copies_fields_moves_payments_and_blocks_duplicates(
    tmp_path, monkeypatch
):
    app = _make_app(tmp_path / "commercial.db")
    _disable_ui_refresh(app)
    monkeypatch.setattr(app_module, "messagebox", FakeMessagebox())

    conn = sqlite3.connect(app.db_path)
    try:
        client_id = _insert_client(conn)
        source_id = _insert_document(
            conn,
            "devis",
            "DEV-1",
            client_id,
            100,
            paid=20,
            payment_mode="partial",
            echeance="2026-12-31",
            discount_enabled=1,
            discount_value=5,
        )
        _insert_line(conn, source_id, 100)
        conn.execute(
            "INSERT INTO payments (document_id, date, amount, method, note) "
            "VALUES (?, '2026-01-01', 20, 'cash', 'Deposit')",
            (source_id,),
        )
        conn.commit()
    finally:
        conn.close()

    app.documents_trees = {"devis": FakeTree(source_id)}
    app.get_next_numero = lambda doc_type, cursor=None: "BL-2026-0001"

    app.convert_selected_document("devis", "bl")

    conn = sqlite3.connect(app.db_path)
    try:
        bl = conn.execute(
            """
            SELECT id, type, statut, total_ttc, paid_amount, payment_mode,
                   echeance, discount_enabled, discount_value
            FROM documents WHERE type = 'bl'
            """
        ).fetchone()
        source = conn.execute(
            "SELECT converted_to_id, converted_at, paid_amount, payment_mode "
            "FROM documents WHERE id = ?",
            (source_id,),
        ).fetchone()
        lines = conn.execute(
            "SELECT COUNT(*) FROM document_lignes WHERE document_id = ?", (bl[0],)
        ).fetchone()[0]
        payment_doc_id = conn.execute(
            "SELECT document_id FROM payments WHERE amount = 20"
        ).fetchone()[0]
    finally:
        conn.close()

    assert bl[1] == "bl"
    assert bl[2] == "brouillon"
    assert bl[3] == 100
    assert bl[4] == 20
    assert bl[5] == "partial"
    assert bl[6] == "2026-12-31"
    assert bl[7] == 1
    assert bl[8] == 5
    assert lines == 1
    assert source[0] == bl[0]
    assert source[1] is not None
    assert source[2] == 0
    assert source[3] == "none"
    assert payment_doc_id == bl[0]

    app.documents_trees = {"devis": FakeTree(source_id)}
    app.convert_selected_document("devis", "bl")

    conn = sqlite3.connect(app.db_path)
    try:
        bl_count = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE type = 'bl'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert bl_count == 1
    assert app_module.messagebox.errors[-1] == t("documents.error_already_converted")


def test_bl_to_invoice_keeps_payment_and_deleting_invoice_releases_source(
    tmp_path, monkeypatch
):
    app = _make_app(tmp_path / "commercial.db")
    _disable_ui_refresh(app)
    monkeypatch.setattr(app_module, "messagebox", FakeMessagebox())

    conn = sqlite3.connect(app.db_path)
    try:
        client_id = _insert_client(conn)
        bl_id = _insert_document(conn, "bl", "BL-1", client_id, 119, paid=20)
        conn.execute(
            "INSERT INTO payments (document_id, date, amount, method, note) "
            "VALUES (?, '2026-01-01', 20, 'cash', 'Deposit')",
            (bl_id,),
        )
        conn.commit()
    finally:
        conn.close()

    app.documents_trees = {"bl": FakeTree(bl_id)}
    app.get_next_numero = lambda doc_type, cursor=None: "FAC-2026-0001"

    app.convert_selected_document("bl", "factures")

    conn = sqlite3.connect(app.db_path)
    try:
        invoice = conn.execute(
            "SELECT id, paid_amount, source_bl_id FROM documents "
            "WHERE type = 'factures'"
        ).fetchone()
        payment_doc_id = conn.execute(
            "SELECT document_id FROM payments WHERE amount = 20"
        ).fetchone()[0]
    finally:
        conn.close()

    assert invoice[1] == 20
    assert invoice[2] == bl_id
    assert payment_doc_id == invoice[0]

    app.documents_trees = {"factures": FakeTree(invoice[0])}
    app.delete_selected_document("factures")

    conn = sqlite3.connect(app.db_path)
    try:
        source = conn.execute(
            "SELECT converted_to_id, converted_at FROM documents WHERE id = ?",
            (bl_id,),
        ).fetchone()
    finally:
        conn.close()

    assert source == (None, None)

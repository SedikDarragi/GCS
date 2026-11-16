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


class FakeMessagebox:
    def __init__(self):
        self.errors = []
        self.infos = []
        self.confirmations = []

    def showerror(self, title, message):
        self.errors.append(message)

    def showinfo(self, title, message):
        self.infos.append(message)

    def askyesno(self, title, message, **kwargs):
        self.confirmations.append(message)
        return True


def _make_app(db_path):
    app = MainApp.__new__(MainApp)
    app.db_path = str(db_path)
    app.init_db()
    return app


def _disable_ui_refresh(app):
    app.load_articles = lambda *args, **kwargs: None
    app.load_clients = lambda *args, **kwargs: None
    app.load_documents = lambda *args, **kwargs: None
    app.load_paiements = lambda *args, **kwargs: None


def _insert_client(conn, name):
    return conn.execute("INSERT INTO clients (nom) VALUES (?)", (name,)).lastrowid


def _insert_article(conn, designation, stock=0):
    return conn.execute(
        "INSERT INTO articles (designation, prix_achat, prix_vente, tva, stock) "
        "VALUES (?, 0, 0, 0, ?)",
        (designation, stock),
    ).lastrowid


def _insert_document(
    conn,
    doc_type,
    numero,
    client_id,
    total,
    statut="brouillon",
):
    return conn.execute(
        """
        INSERT INTO documents (
            type, numero, date, client_id, statut, total_ht, total_tva,
            total_ttc, paid_amount, payment_mode
        ) VALUES (?, ?, '2026-01-01 00:00:00', ?, ?, ?, 0, ?, 0, 'none')
        """,
        (doc_type, numero, client_id, statut, total, total),
    ).lastrowid


def _insert_line(conn, document_id, article_id, designation, qty, price):
    total = qty * price
    return conn.execute(
        """
        INSERT INTO document_lignes (
            document_id, article_id, designation, quantite, prix_unitaire,
            tva, total_ht, total_tva, total_ttc
        ) VALUES (?, ?, ?, ?, ?, 0, ?, 0, ?)
        """,
        (document_id, article_id, designation, qty, price, total, total),
    ).lastrowid


def _insert_payment(conn, document_id, amount):
    conn.execute(
        "INSERT INTO payments (document_id, date, amount, method, note) "
        "VALUES (?, '2026-01-01', ?, 'cash', 'Test')",
        (document_id, amount),
    )


def _count(conn, table, where="", params=()):
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return conn.execute(sql, params).fetchone()[0]


def test_delete_article_keeps_historical_document_lines(tmp_path, monkeypatch):
    app = _make_app(tmp_path / "commercial.db")
    _disable_ui_refresh(app)
    messages = FakeMessagebox()
    monkeypatch.setattr(app_module, "messagebox", messages)

    conn = sqlite3.connect(app.db_path)
    try:
        client_id = _insert_client(conn, "Acme")
        article_id = _insert_article(conn, "Widget", stock=10)
        doc_id = _insert_document(conn, "factures", "FAC-1", client_id, 100)
        _insert_line(conn, doc_id, article_id, "Widget", 2, 50)
        conn.commit()
    finally:
        conn.close()

    app.articles_tree = FakeTree(article_id)
    app.delete_selected_article()

    conn = sqlite3.connect(app.db_path)
    try:
        assert _count(conn, "articles", "id = ?", (article_id,)) == 0
        line = conn.execute(
            "SELECT article_id, designation, quantite, total_ttc "
            "FROM document_lignes WHERE document_id = ?",
            (doc_id,),
        ).fetchone()
        document_total = conn.execute(
            "SELECT total_ttc FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()[0]
    finally:
        conn.close()

    assert line == (None, "Widget", 2, 100)
    assert document_total == 100
    assert messages.errors == []
    assert t("articles.confirm_delete_referenced", count=1) in messages.confirmations


def test_delete_validated_bl_restores_stock_and_clears_references(
    tmp_path, monkeypatch
):
    app = _make_app(tmp_path / "commercial.db")
    _disable_ui_refresh(app)
    messages = FakeMessagebox()
    monkeypatch.setattr(app_module, "messagebox", messages)

    conn = sqlite3.connect(app.db_path)
    try:
        client_id = _insert_client(conn, "Acme")
        # Stock is 3 because the validated BL already removed 2 units.
        article_id = _insert_article(conn, "Widget", stock=3)
        devis_id = _insert_document(conn, "devis", "DEV-1", client_id, 100)
        bl_id = _insert_document(conn, "bl", "BL-1", client_id, 100, statut="valide")
        invoice_id = _insert_document(conn, "factures", "FAC-1", client_id, 100)
        _insert_line(conn, bl_id, article_id, "Widget", 2, 50)
        _insert_payment(conn, bl_id, 20)
        conn.execute(
            "UPDATE documents SET converted_to_id = ? WHERE id = ?", (bl_id, devis_id)
        )
        conn.execute(
            "UPDATE documents SET source_bl_id = ? WHERE id = ?", (bl_id, invoice_id)
        )
        conn.commit()
    finally:
        conn.close()

    app.documents_trees = {"bl": FakeTree(bl_id)}
    app.delete_selected_document("bl")

    conn = sqlite3.connect(app.db_path)
    try:
        stock = conn.execute(
            "SELECT stock FROM articles WHERE id = ?", (article_id,)
        ).fetchone()[0]
        devis_link = conn.execute(
            "SELECT converted_to_id FROM documents WHERE id = ?", (devis_id,)
        ).fetchone()[0]
        invoice_source = conn.execute(
            "SELECT source_bl_id FROM documents WHERE id = ?", (invoice_id,)
        ).fetchone()[0]
        bl_count = _count(conn, "documents", "id = ?", (bl_id,))
        line_count = _count(conn, "document_lignes", "document_id = ?", (bl_id,))
        payment_count = _count(conn, "payments", "document_id = ?", (bl_id,))
    finally:
        conn.close()

    assert stock == 5
    assert devis_link is None
    assert invoice_source is None
    assert bl_count == 0
    assert line_count == 0
    assert payment_count == 0
    assert messages.errors == []


def test_delete_validated_reception_reverses_stock(tmp_path, monkeypatch):
    app = _make_app(tmp_path / "commercial.db")
    _disable_ui_refresh(app)
    monkeypatch.setattr(app_module, "messagebox", FakeMessagebox())

    conn = sqlite3.connect(app.db_path)
    try:
        client_id = _insert_client(conn, "Acme")
        # Stock is 7 because the validated reception already added 2 units.
        article_id = _insert_article(conn, "Widget", stock=7)
        reception_id = _insert_document(
            conn, "reception", "BR-1", client_id, 100, statut="valide"
        )
        _insert_line(conn, reception_id, article_id, "Widget", 2, 50)
        conn.commit()
    finally:
        conn.close()

    app.documents_trees = {"reception": FakeTree(reception_id)}
    app.delete_selected_document("reception")

    conn = sqlite3.connect(app.db_path)
    try:
        stock = conn.execute(
            "SELECT stock FROM articles WHERE id = ?", (article_id,)
        ).fetchone()[0]
    finally:
        conn.close()

    assert stock == 5


def test_delete_client_cascades_documents_lines_and_payments(tmp_path, monkeypatch):
    app = _make_app(tmp_path / "commercial.db")
    _disable_ui_refresh(app)
    messages = FakeMessagebox()
    monkeypatch.setattr(app_module, "messagebox", messages)

    conn = sqlite3.connect(app.db_path)
    try:
        client_id = _insert_client(conn, "Acme")
        other_client_id = _insert_client(conn, "Other")
        article_id = _insert_article(conn, "Widget", stock=3)

        bl_id = _insert_document(conn, "bl", "BL-1", client_id, 100, statut="valide")
        invoice_id = _insert_document(conn, "factures", "FAC-1", client_id, 100)
        other_doc_id = _insert_document(
            conn, "factures", "FAC-2", other_client_id, 50
        )
        _insert_line(conn, bl_id, article_id, "Widget", 2, 50)
        _insert_line(conn, invoice_id, None, "Service", 1, 100)
        _insert_line(conn, other_doc_id, None, "Service", 1, 50)
        _insert_payment(conn, invoice_id, 25)
        _insert_payment(conn, other_doc_id, 10)
        conn.commit()
    finally:
        conn.close()

    app.clients_tree = FakeTree(client_id)
    app.delete_selected_client()

    conn = sqlite3.connect(app.db_path)
    try:
        assert _count(conn, "clients", "id = ?", (client_id,)) == 0
        assert _count(conn, "documents", "client_id = ?", (client_id,)) == 0
        assert (
            _count(
                conn,
                "document_lignes",
                f"document_id IN ({bl_id}, {invoice_id})",
            )
            == 0
        )
        assert (
            _count(
                conn,
                "payments",
                f"document_id IN ({bl_id}, {invoice_id})",
            )
            == 0
        )
        assert _count(conn, "documents", "id = ?", (other_doc_id,)) == 1
        assert _count(conn, "document_lignes", "document_id = ?", (other_doc_id,)) == 1
        assert _count(conn, "payments", "document_id = ?", (other_doc_id,)) == 1
        stock = conn.execute(
            "SELECT stock FROM articles WHERE id = ?", (article_id,)
        ).fetchone()[0]
    finally:
        conn.close()

    # Deleting the validated BL restores the 2 units it had removed.
    assert stock == 5
    assert messages.errors == []
    assert t("clients.confirm_delete_with_docs", count=2) in messages.confirmations

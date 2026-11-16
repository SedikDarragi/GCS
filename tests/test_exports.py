"""Tests for HTML/PDF exports: escaping and language switching."""

import sqlite3

import pytest

import i18n
import main_app
from main_app import MainApp

CLIENT_NAME = "A & B <script>alert(1)</script>"
CLIENT_EMAIL = "a&b@example.com"
DOC_NUMERO = "DEV-&<1>"
DESIGNATION = "Ligne & <b>test</b>"


def _make_app(tmp_path):
    app = MainApp.__new__(MainApp)
    app.db_path = str(tmp_path / "commercial.db")
    app.user = {"id": 1, "username": "admin<&>"}
    app.init_db()
    return app


def _seed_document(app):
    conn = sqlite3.connect(app.db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO clients (nom, email, telephone, adresse, matricule_fiscale) "
            "VALUES (?, ?, ?, ?, ?)",
            (CLIENT_NAME, CLIENT_EMAIL, "0102030405", "1 <rue> & co", "TVA<&>"),
        )
        client_id = cur.lastrowid
        cur.execute(
            "INSERT INTO documents (type, numero, date, client_id, statut, "
            "total_ht, total_tva, total_ttc) "
            "VALUES ('devis', ?, '2026-01-01', ?, 'brouillon', 1000, 200, 1200)",
            (DOC_NUMERO, client_id),
        )
        document_id = cur.lastrowid
        cur.execute(
            "INSERT INTO document_lignes (document_id, designation, quantite, "
            "prix_unitaire, tva, total_ht, total_tva, total_ttc) "
            "VALUES (?, ?, 1, 1000, 20, 1000, 200, 1200)",
            (document_id, DESIGNATION),
        )
        conn.commit()
        return document_id
    finally:
        conn.close()


def _use_language(monkeypatch, lang):
    monkeypatch.setattr(main_app, "get_language", lambda: lang)
    monkeypatch.setattr(
        main_app, "t", lambda key, **kwargs: i18n.t(key, lang=lang, **kwargs)
    )


@pytest.mark.parametrize(
    "lang, labels",
    [
        ("en", ("Description", "Qty", "Unit Price", "Print", "Quotes")),
        ("fr", ("Désignation", "Qté", "PU", "Imprimer", "Devis")),
    ],
)
def test_html_export_escapes_content_and_follows_language(
    tmp_path, monkeypatch, lang, labels
):
    _use_language(monkeypatch, lang)
    app = _make_app(tmp_path)
    document_id = _seed_document(app)
    target = tmp_path / "export.html"

    app.export_document_html(document_id, str(target))
    html = target.read_text(encoding="utf-8")

    assert f"<html lang='{lang}'>" in html
    assert "A &amp; B &lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert "admin&lt;&amp;&gt;" in html
    assert "1 &lt;rue&gt; &amp; co" in html
    assert "a&amp;b@example.com" in html
    assert "DEV-&amp;&lt;1&gt;" in html
    assert "Ligne &amp; &lt;b&gt;test&lt;/b&gt;" in html
    for label in labels:
        assert label in html


def test_pdf_export_handles_markup_characters(tmp_path, monkeypatch):
    if main_app.SimpleDocTemplate is None:
        pytest.skip("reportlab is not installed")

    _use_language(monkeypatch, "en")
    app = _make_app(tmp_path)
    document_id = _seed_document(app)
    target = tmp_path / "export.pdf"

    app.export_document_pdf(document_id, str(target))

    assert target.exists()
    assert target.stat().st_size > 0


def test_pdf_total_labels_exist_in_both_languages():
    for key in ("export.pdf_total_ht", "export.pdf_total_tva", "export.pdf_total_ttc"):
        assert key in i18n.TRANSLATIONS["en"]
        assert key in i18n.TRANSLATIONS["fr"]

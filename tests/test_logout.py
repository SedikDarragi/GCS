"""Tests for returning to the login screen without creating a second Tk root."""

import types

import main_app
from main_app import MainApp


class FakeParent:
    def __init__(self, exists=True):
        self._exists = exists
        self.events = []
        self.password_entry = types.SimpleNamespace(
            delete=lambda *args: self.events.append("clear_password")
        )
        self.lang_var = types.SimpleNamespace(
            set=lambda value: self.events.append(("lang_var", value))
        )
        self.lang_switch = types.SimpleNamespace(
            set=lambda value: self.events.append(("lang_switch", value))
        )

    def winfo_exists(self):
        return self._exists

    def deiconify(self):
        self.events.append("deiconify")

    def lift(self):
        self.events.append("lift")

    def apply_language(self):
        self.events.append("apply_language")


def _make_app(parent):
    app = MainApp.__new__(MainApp)
    app._parent = parent
    app.events = []
    app.destroy = lambda: app.events.append("destroy")
    return app


def test_return_to_login_reuses_existing_parent(monkeypatch):
    monkeypatch.setattr(main_app, "get_language", lambda: "en")
    parent = FakeParent()
    app = _make_app(parent)

    app._return_to_login()

    assert app.events == ["destroy"]
    assert parent.events == [
        "deiconify",
        "lift",
        "clear_password",
        "apply_language",
        ("lang_var", "EN"),
        ("lang_switch", "EN"),
    ]


def test_return_to_login_syncs_french_language(monkeypatch):
    monkeypatch.setattr(main_app, "get_language", lambda: "fr")
    parent = FakeParent()
    app = _make_app(parent)

    app._return_to_login()

    assert ("lang_var", "FR") in parent.events
    assert ("lang_switch", "FR") in parent.events


def test_return_to_login_without_parent_only_closes_main_window():
    app = _make_app(None)

    app._return_to_login()

    assert app.events == ["destroy"]


def test_return_to_login_skips_destroyed_parent():
    parent = FakeParent(exists=False)
    app = _make_app(parent)

    app._return_to_login()

    assert app.events == ["destroy"]
    assert parent.events == []


def test_logout_delegates_to_return_to_login(monkeypatch):
    calls = []
    monkeypatch.setattr(
        MainApp, "_return_to_login", lambda self: calls.append(self)
    )
    app = MainApp.__new__(MainApp)

    app.logout()

    assert calls == [app]

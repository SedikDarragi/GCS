import math
import os
import re
import sqlite3
import tempfile
import tkinter as tk
import webbrowser
from datetime import datetime
from decimal import Decimal
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
from PIL import Image, ImageTk
from accounts import (
    AccountError,
    admin_create_user,
    admin_delete_user,
    admin_update_user,
    ensure_account_schema,
    get_user_by_id,
    require_admin,
)
from database_maintenance import (
    DatabaseMaintenanceError,
    backup_database,
    reset_database,
    restore_database,
    validate_database_file,
)
from main import DB_PATH, prompt_password_change, verify_password
from i18n import get_language, set_language, t
from money import (
    as_cents,
    discount_cents,
    format_money,
    line_totals,
    migrate_money_to_cents,
    money_to_input,
    parse_money,
)

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
except Exception:
    A4 = None
    canvas = None
    colors = None
    getSampleStyleSheet = None
    ParagraphStyle = None
    mm = None
    SimpleDocTemplate = None
    Table = None
    TableStyle = None
    Paragraph = None
    Spacer = None


def _parse_finite_number(raw):
    """Return ``raw`` as a finite float, or None when it is not a number.

    ``float`` accepts ``nan`` and ``inf`` which would poison totals and
    payment balances, so those are rejected here as well.
    """
    if isinstance(raw, bool):
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _validate_quantity(raw):
    value = _parse_finite_number(raw)
    if value is None or value <= 0:
        return None
    return value


def _validate_unit_price(raw):
    value = parse_money(raw)
    if value is None or value < 0:
        return None
    return value


def _validate_percentage(raw):
    value = _parse_finite_number(raw)
    if value is None or value < 0 or value > 100:
        return None
    return value


def _validate_line_inputs(qty_raw, pu_raw, tva_raw):
    """Validate a document line.

    Returns ``((quantity, unit_price, vat), None)`` on success or
    ``(None, error_key)`` on failure, where ``error_key`` is an i18n key.
    """
    qty = _validate_quantity(qty_raw)
    if qty is None:
        return None, "new_doc.error_qty_invalid"
    pu = _validate_unit_price(pu_raw)
    if pu is None:
        return None, "new_doc.error_pu_invalid"
    tva = _validate_percentage(tva_raw)
    if tva is None:
        return None, "new_doc.error_tva_invalid"
    return (qty, pu, tva), None


def _validate_discount_input(enabled, raw):
    """Validate the optional discount percentage.

    Returns ``(percentage, None)`` on success or ``(None, error_key)``.
    """
    if not enabled:
        return 0.0, None
    value = _validate_percentage(raw or 0)
    if value is None:
        return None, "new_doc.error_discount_invalid"
    return value, None


def _validate_document_date(raw):
    """Accept ``YYYY-MM-DD`` or ``YYYY-MM-DD HH:MM:SS`` and normalize it."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d":
            return parsed.strftime("%Y-%m-%d 00:00:00")
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    return None


def _validate_payment_amount(raw, remaining):
    """Return a positive amount in cents within the remaining balance.

    ``remaining`` is expressed in cents as well.  ``None`` means the caller
    does not know the balance; the amount is then only checked for positivity.
    """
    amount = parse_money(raw)
    if amount is None or amount <= 0:
        return None
    if remaining is not None:
        remaining_value = as_cents(remaining)
        if remaining_value <= 0:
            return None
        if amount > remaining_value:
            return None
    return amount


def _apply_payment(cursor, document_id, raw_amount, method=None):
    """Validate and record a payment on an invoice using ``cursor``.

    Returns ``(None, {})`` on success or ``(error_key, kwargs)`` on failure.
    The caller owns the transaction and must commit or roll back.
    """
    cursor.execute(
        "SELECT type, total_ttc, paid_amount FROM documents WHERE id = ?",
        (document_id,),
    )
    row = cursor.fetchone()
    if not row:
        return "payment_dialog.error_doc_not_found", {}
    if row[0] != "factures":
        return "payment_dialog.error_not_invoice", {}

    amount = parse_money(raw_amount)
    if amount is None:
        return "payment_dialog.error_invalid_amount", {}
    if amount <= 0:
        return "payment_dialog.error_amount_positive", {}

    total_ttc = max(0, as_cents(row[1]))
    paid = max(0, as_cents(row[2]))
    remaining = max(0, total_ttc - paid)
    if remaining <= 0:
        return "payment_dialog.error_nothing_due", {}
    if amount > remaining:
        return (
            "payment_dialog.error_amount_exceeds_remaining",
            {"remaining": Decimal(remaining) / Decimal(100)},
        )

    new_paid = paid + amount
    date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO payments (document_id, date, amount, method, note) VALUES (?, ?, ?, ?, ?)",
        (document_id, date_now, amount, method, "Paiement"),
    )
    mode = "full" if new_paid >= total_ttc else "partial"
    cursor.execute(
        "UPDATE documents SET paid_amount = ?, payment_mode = ? WHERE id = ?",
        (new_paid, mode, document_id),
    )
    return None, {}


_NUMERO_PREFIXES = {
    "devis": "DEV",
    "bl": "BL",
    "factures": "FAC",
    "reception": "BR",
}
_NUMERO_PATTERN = re.compile(r"^([A-Z]+)-(\d{4})-(\d+)$")


def _numero_prefix(doc_type):
    return _NUMERO_PREFIXES.get(doc_type, str(doc_type).upper())


def _numero_counter(numero, prefix, annee):
    """Return the counter of ``numero`` for ``prefix``/``annee``, else 0."""
    if not isinstance(numero, str):
        return 0
    match = _NUMERO_PATTERN.fullmatch(numero.strip().upper())
    if not match or match.group(1) != prefix:
        return 0
    if int(match.group(2)) != annee:
        return 0
    return int(match.group(3))


def _max_document_counter(cursor, doc_type, prefix, annee):
    """Highest existing counter for this type/year, so sequences can catch up."""
    cursor.execute(
        "SELECT numero FROM documents WHERE type = ? AND numero LIKE ?",
        (doc_type, f"{prefix}-{annee}-%"),
    )
    return max(
        (_numero_counter(row[0], prefix, annee) for row in cursor.fetchall()),
        default=0,
    )


def _numero_exists(cursor, numero, exclude_id=None):
    """Whether ``numero`` is already used (optionally ignoring ``exclude_id``)."""
    if exclude_id is None:
        cursor.execute(
            "SELECT 1 FROM documents WHERE UPPER(TRIM(numero)) = UPPER(?)",
            (numero,),
        )
    else:
        cursor.execute(
            "SELECT 1 FROM documents WHERE UPPER(TRIM(numero)) = UPPER(?) AND id <> ?",
            (numero, exclude_id),
        )
    return cursor.fetchone() is not None


class MainApp(ctk.CTkToplevel):
    def __init__(self, user, parent=None):
        super().__init__(parent)

        try:
            _orig_tree_init = ttk.Treeview.__init__

            def _tree_init_and_overlay(self, *a, **kw):
                _orig_tree_init(self, *a, **kw)
                try:
                    self.after(50, lambda: self._attach_grid_overlay())
                except Exception:
                    pass

            if not getattr(ttk.Treeview, "_grid_overlay_patched", False):
                ttk.Treeview.__init__ = _tree_init_and_overlay
                ttk.Treeview._grid_overlay_patched = True

            def _attach_grid_overlay_method(self):
                if hasattr(self, "_grid_overlay"):
                    return
                try:
                    canvas = tk.Canvas(
                        self.master, highlightthickness=0, bd=0, bg="", relief="flat"
                    )
                except Exception:
                    return

                def redraw(event=None):
                    try:
                        canvas.delete("gridline")
                        w = self.winfo_width()
                        h = self.winfo_height()
                        if w <= 1 or h <= 1:
                            return

                        x = 0
                        for col in self["columns"]:
                            colw = int(self.column(col, option="width") or 0)
                            x += colw

                            canvas.create_line(
                                x, 0, x, h, fill="#d6d6d6", tags=("gridline",), width=1
                            )

                        for iid in self.get_children(""):
                            bbox = self.bbox(iid)
                            if bbox:
                                y = bbox[1] + bbox[3]
                                canvas.create_line(
                                    0,
                                    y,
                                    w,
                                    y,
                                    fill="#eeeeee",
                                    tags=("gridline",),
                                    width=1,
                                )
                    except Exception:
                        pass

                try:
                    canvas.place(in_=self, relx=0, rely=0, relwidth=1, relheight=1)
                    canvas.lift(self)
                except Exception:
                    canvas.destroy()
                    return

                def _forward(event):
                    try:
                        x = event.x_root - self.winfo_rootx()
                        y = event.y_root - self.winfo_rooty()
                        self.event_generate(event.type, x=int(x), y=int(y))
                    except Exception:
                        pass

                for ev in (
                    "<Button-1>",
                    "<Double-1>",
                    "<ButtonRelease-1>",
                    "<Motion>",
                    "<Button-2>",
                    "<Button-3>",
                ):
                    canvas.bind(ev, _forward)

                self.bind("<Configure>", redraw)
                self.bind("<Expose>", redraw)
                self.bind("<ButtonRelease-1>", lambda e: self.after(1, redraw))
                self.bind("<MouseWheel>", lambda e: self.after(10, redraw))
                try:
                    vs = (
                        self.master.nametowidget(self.cget("yscrollcommand"))
                        if self.cget("yscrollcommand")
                        else None
                    )
                except Exception:
                    vs = None
                self._grid_overlay = canvas

                self.after(60, redraw)

            if not hasattr(ttk.Treeview, "_attach_grid_overlay"):
                ttk.Treeview._attach_grid_overlay = _attach_grid_overlay_method
        except Exception:
            pass

        if isinstance(user, dict):
            self.user = {
                "id": user.get("id"),
                "username": user.get("username", ""),
                "email": user.get("email", ""),
                "full_name": user.get("full_name", ""),
                "is_admin": bool(user.get("is_admin")),
                "must_change_password": bool(
                    user.get("must_change_password")
                ),
            }
        else:
            # Backward compatibility for callers passing the old tuple row.
            self.user = {
                "id": user[0],
                "username": user[1],
                "is_admin": bool(user[5]) if len(user) > 5 else False,
                "email": user[3] if len(user) > 3 else "",
                "full_name": user[4] if len(user) > 4 else "",
                "must_change_password": False,
            }

        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.title(
            t("app.window_title_connected", username=self.user['username'])
        )
        self.geometry("1400x800")
        self.minsize(1200, 700)

        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        try:
            if parent is not None:
                self._parent = parent
        except Exception:
            pass

        self.ui = {
            "font_family": "Arial",
            "font_h1": ("Arial", 24, "bold"),
            "font_h2": ("Arial", 14, "bold"),
            "font_body": ("Arial", 11),
            "sidebar_width": 220,
            "sidebar_btn_height": 40,
            "sidebar_btn_fg": "transparent",
            "sidebar_btn_hover": ("gray70", "gray30"),
            "sidebar_btn_active_fg": ("gray85", "gray25"),
            "sidebar_btn_text": ("gray10", "gray90"),
            "danger_fg": "#FF4B4B",
            "danger_hover": "#FF0000",
            "content_padx": 22,
            "content_pady": 22,
            "tree_rowheight": 28,
            "tree_even": "#FFFFFF",
            "tree_odd": "#F6F8FB",
        }

        self.current_frame = None

        self.db_path = DB_PATH
        self.init_db()

        self._setup_ttk_style()

        self.create_widgets()

        self.show_frame("articles")

    def create_widgets(self):
        self.sidebar = ctk.CTkFrame(
            self, width=self.ui["sidebar_width"], corner_radius=0
        )
        self.sidebar.pack(side="left", fill="y", expand=False)

        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            logo_path = os.path.join(base_dir, "assets", "logo.png")
            if os.path.exists(logo_path):
                pil_img = Image.open(logo_path).convert("RGBA")
                pil_resized = pil_img.resize((100, 100), Image.LANCZOS)

                try:
                    self.logo_image = ctk.CTkImage(
                        light_image=pil_resized, dark_image=pil_resized, size=(100, 100)
                    )
                    self.logo_label = ctk.CTkLabel(
                        self.sidebar, image=self.logo_image, text=""
                    )
                except Exception as e:
                    print(
                        f"CTkImage creation failed: {e}; falling back to tk.PhotoImage"
                    )
                    try:
                        self._logo_tk = ImageTk.PhotoImage(pil_resized, master=self)
                        import tkinter as tk

                        self.logo_label = tk.Label(
                            self.sidebar, image=self._logo_tk, bd=0
                        )
                    except Exception as e2:
                        print(f"Fallback PhotoImage failed: {e2}")

                        self.logo_label = ctk.CTkLabel(
                            self.sidebar, text="LOGO", font=("Arial", 16, "bold")
                        )
            else:
                print(f"Sidebar logo not found at {logo_path}")
                raise FileNotFoundError("Logo file missing")
        except Exception as e:
            print(f"Error loading sidebar logo: {e}")
            self.logo_label = ctk.CTkLabel(
                self.sidebar, text="LOGO", font=("Arial", 16, "bold")
            )

        self.logo_label.pack(pady=20, padx=10)

        # --- Language switcher ---
        try:
            self.lang_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
            self.lang_frame.pack(fill="x", padx=10, pady=(6, 4))
            ctk.CTkLabel(self.lang_frame, text=t("login.language_label") + ":", font=("Arial", 11)).pack(side="left", padx=(4, 6))
            self.lang_var = ctk.StringVar(value="EN" if get_language() == "en" else "FR")
            try:
                self.lang_switch = ctk.CTkSegmentedButton(
                    self.lang_frame, values=["EN", "FR"], command=self.on_language_change, width=120
                )
                self.lang_switch.set(self.lang_var.get())
                self.lang_switch.pack(side="right", padx=4)
            except Exception:
                self.lang_switch = ctk.CTkOptionMenu(
                    self.lang_frame, values=["EN", "FR"], variable=self.lang_var, command=self.on_language_change, width=80
                )
                self.lang_switch.pack(side="right", padx=4)
        except Exception as e:
            print(f"Language switcher init failed: {e}")

        self.menu_label = ctk.CTkLabel(
            self.sidebar, text=t("sidebar.menu_heading"), font=self.ui["font_h2"]
        )
        self.menu_label.pack(pady=(20, 10), padx=10, anchor="w")

        nav_buttons = [
            (t("nav.articles"), "articles"),
            (t("nav.clients"), "clients"),
            (t("nav.devis"), "devis"),
            (t("nav.bl"), "bl"),
            (t("nav.factures"), "factures"),
            (t("nav.reception"), "reception"),
            (t("nav.paiements"), "paiements"),
        ]

        if self.user.get("is_admin"):
            nav_buttons.append((t("nav.admin"), "admin"))

        nav_buttons.append((t("nav.about"), "about"))

        self.nav_buttons = {}
        for text, command in nav_buttons:
            if command == "admin":
                cmd_func = lambda txt=text: self.open_admin_settings()
            elif command == "about":
                cmd_func = lambda txt=text: self.open_about_dialog()
            else:
                cmd_func = lambda cmd=command: self.show_frame(cmd)

            btn = ctk.CTkButton(
                self.sidebar,
                text=text,
                command=cmd_func,
                fg_color=self.ui["sidebar_btn_fg"],
                text_color=self.ui["sidebar_btn_text"],
                hover_color=self.ui["sidebar_btn_hover"],
                anchor="w",
                height=self.ui["sidebar_btn_height"],
                corner_radius=10,
            )
            btn.pack(fill="x", pady=2, padx=10)
            self.nav_buttons[command] = btn

        ctk.CTkLabel(self.sidebar, text="").pack(fill="x", pady=20)

        user_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        user_frame.pack(side="bottom", fill="x", padx=10, pady=10)

        self.logged_in_label = ctk.CTkLabel(
            user_frame, text=t("sidebar.logged_in_as"), font=("Arial", 10)
        )
        self.logged_in_label.pack(anchor="w")

        ctk.CTkLabel(
            user_frame, text=f"{self.user['username']}", font=("Arial", 12, "bold")
        ).pack(anchor="w")

        self.change_password_btn = ctk.CTkButton(
            user_frame,
            text=t("sidebar.change_password"),
            command=self.open_change_password_dialog,
            fg_color=self.ui["sidebar_btn_fg"],
            text_color=self.ui["sidebar_btn_text"],
            hover_color=self.ui["sidebar_btn_hover"],
            width=120,
        )
        self.change_password_btn.pack(pady=(10, 4), fill="x")

        self.logout_btn = ctk.CTkButton(
            user_frame,
            text=t("sidebar.logout"),
            command=self.logout,
            fg_color=self.ui["danger_fg"],
            hover_color=self.ui["danger_hover"],
            width=120,
        )
        self.logout_btn.pack(pady=(10, 0), fill="x")

        ctk.CTkLabel(self.sidebar, text="").pack(fill="x", pady=(6, 0))

        try:
            about_lbl = ctk.CTkLabel(
                self.sidebar,
                text=t("nav.about"),
                text_color="blue",
                cursor="hand2",
                font=("Arial", 10, "underline"),
            )
            about_lbl.pack(fill="x", padx=10, pady=(6, 6), anchor="w")
            about_lbl.bind("<Button-1>", lambda e: self.open_about_dialog())
        except Exception:
            pass

        self.main_frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.main_frame.pack(
            side="right",
            fill="both",
            expand=True,
            padx=self.ui["content_padx"],
            pady=self.ui["content_pady"],
        )

        self.frames = {}

        self.create_articles_frame()
        self.create_clients_frame()
        self.create_documents_frame("devis", t("nav.devis"))
        self.create_documents_frame("bl", t("nav.bl"))
        self.create_documents_frame("factures", t("nav.factures"))
        self.create_documents_frame("reception", t("nav.reception"))
        self.create_paiements_frame()

    def show_frame(self, frame_name):
        if self.current_frame:
            self.current_frame.pack_forget()

        if frame_name in self.frames:
            self.current_frame = self.frames[frame_name]
            self.current_frame.pack(fill="both", expand=True)
            self._set_active_nav(frame_name)

    def _set_active_nav(self, frame_name):
        btns = getattr(self, "nav_buttons", {})
        for key, btn in btns.items():
            if key == frame_name:
                btn.configure(fg_color=self.ui["sidebar_btn_active_fg"])
            else:
                btn.configure(fg_color=self.ui["sidebar_btn_fg"])

    def on_language_change(self, value):
        lang_code = "en" if str(value).upper().startswith("EN") else "fr"
        set_language(lang_code)
        try:
            self.lang_var.set("EN" if lang_code == "en" else "FR")
            try:
                self.lang_switch.set(self.lang_var.get())
            except Exception:
                pass
        except Exception:
            pass
        self.apply_language()

    def apply_language(self):
        # Window title
        try:
            self.title(t("app.window_title_connected", username=self.user.get("username","")))
        except Exception:
            pass
        # Sidebar
        try:
            if hasattr(self, "menu_label"):
                self.menu_label.configure(text=t("sidebar.menu_heading"))
        except Exception:
            pass
        try:
            nav_map = {
                "articles": "nav.articles",
                "clients": "nav.clients",
                "devis": "nav.devis",
                "bl": "nav.bl",
                "factures": "nav.factures",
                "reception": "nav.reception",
                "paiements": "nav.paiements",
                "admin": "nav.admin",
                "about": "nav.about",
            }
            for key, btn in getattr(self, "nav_buttons", {}).items():
                k = nav_map.get(key)
                if k:
                    btn.configure(text=t(k))
        except Exception:
            pass
        try:
            if hasattr(self, "logout_btn"):
                self.logout_btn.configure(text=t("sidebar.logout"))
        except Exception:
            pass
        try:
            if hasattr(self, "logged_in_label"):
                self.logged_in_label.configure(text=t("sidebar.logged_in_as"))
        except Exception:
            pass
        try:
            if hasattr(self, "lang_frame"):
                for ch in self.lang_frame.winfo_children():
                    if isinstance(ch, ctk.CTkLabel):
                        ch.configure(text=t("login.language_label") + ":")
                        break
        except Exception:
            pass
        # Articles frame
        try:
            if hasattr(self, "articles_header_label"):
                self.articles_header_label.configure(text=t("articles.header_title"))
            if hasattr(self, "articles_add_btn"):
                self.articles_add_btn.configure(text=t("articles.button_add"))
            if hasattr(self, "delete_article_btn"):
                self.delete_article_btn.configure(text=t("common.delete"))
            if hasattr(self, "article_search_entry"):
                self.article_search_entry.configure(placeholder_text=t("articles.search_placeholder"))
            if hasattr(self, "articles_search_btn"):
                self.articles_search_btn.configure(text=t("common.search"))
            if hasattr(self, "articles_tree"):
                try:
                    cols = list(self.articles_tree["columns"])
                    keys = ["ID", "articles.heading_reference", "articles.heading_designation", "articles.heading_pa", "articles.heading_pv", "articles.heading_tva", "articles.heading_margin", "articles.heading_stock"]
                    for col_id, k in zip(cols, keys):
                        txt = "ID" if k == "ID" else t(k)
                        self.articles_tree.heading(col_id, text=txt)
                except Exception:
                    pass
        except Exception:
            pass
        # Clients frame
        try:
            if hasattr(self, "clients_header_label"):
                self.clients_header_label.configure(text=t("clients.header_title"))
            if hasattr(self, "clients_add_btn"):
                self.clients_add_btn.configure(text=t("clients.button_add"))
            if hasattr(self, "delete_client_btn"):
                self.delete_client_btn.configure(text=t("common.delete"))
            if hasattr(self, "client_search_entry"):
                self.client_search_entry.configure(placeholder_text=t("clients.search_placeholder"))
            if hasattr(self, "clients_search_btn"):
                self.clients_search_btn.configure(text=t("common.search"))
            if hasattr(self, "clients_tree"):
                try:
                    cols = list(self.clients_tree["columns"])
                    keys = ["ID", "clients.col_name", "register.email_placeholder", "clients.col_phone", "clients.col_address", "clients.col_tax_id"]
                    for col_id, k in zip(cols, keys):
                        txt = "ID" if k == "ID" else t(k)
                        self.clients_tree.heading(col_id, text=txt)
                except Exception:
                    pass
        except Exception:
            pass
        # Documents frames
        try:
            header_map = {
                "devis": "documents.header_devis",
                "bl": "documents.header_bl",
                "factures": "documents.header_factures",
                "reception": "documents.header_reception",
            }
            new_btn_map = {
                "devis": "documents.button_new_devis",
                "bl": "documents.button_new_bl",
                "factures": "documents.button_new_factures",
                "reception": "documents.button_new_reception",
            }
            for key in ["devis", "bl", "factures", "reception"]:
                if hasattr(self, "doc_headers") and key in getattr(self, "doc_headers", {}):
                    hk = header_map.get(key)
                    if hk:
                        self.doc_headers[key].configure(text=t(hk))
                if hasattr(self, "doc_new_buttons") and key in getattr(self, "doc_new_buttons", {}):
                    nbk = new_btn_map.get(key)
                    if nbk:
                        self.doc_new_buttons[key].configure(text=t(nbk))
                if hasattr(self, "doc_delete_buttons") and key in getattr(self, "doc_delete_buttons", {}):
                    self.doc_delete_buttons[key].configure(text=t("common.delete"))
                if hasattr(self, "doc_convert_buttons") and key in getattr(self, "doc_convert_buttons", {}):
                    # devis -> bl, bl -> factures
                    if key == "devis":
                        self.doc_convert_buttons[key].configure(text=t("documents.button_convert_devis_to_bl"))
                    elif key == "bl":
                        self.doc_convert_buttons[key].configure(text=t("documents.button_convert_bl_to_invoice"))
                if hasattr(self, "doc_validate_buttons") and key in getattr(self, "doc_validate_buttons", {}):
                    if key == "bl":
                        self.doc_validate_buttons[key].configure(text=t("documents.button_validate_bl"))
                    elif key == "reception":
                        self.doc_validate_buttons[key].configure(text=t("documents.button_validate_reception"))
                if hasattr(self, "document_search_entries") and key in getattr(self, "document_search_entries", {}):
                    self.document_search_entries[key].configure(placeholder_text=t("documents.search_placeholder"))
                if hasattr(self, "doc_search_buttons") and key in getattr(self, "doc_search_buttons", {}):
                    self.doc_search_buttons[key].configure(text=t("common.search"))
                if hasattr(self, "documents_trees") and key in getattr(self, "documents_trees", {}):
                    tree = self.documents_trees[key]
                    try:
                        cols = list(tree["columns"])
                        keys = ["ID", "client_dialog.col_number", "documents.col_client", "article_dialog.col_date", "client_dialog.col_status", "documents.col_ht", "documents.col_tva", "documents.col_ttc"]
                        for col_id, k in zip(cols, keys):
                            txt = "ID" if k == "ID" else t(k)
                            tree.heading(col_id, text=txt)
                    except Exception:
                        pass
        except Exception:
            pass
        # Paiements frame
        try:
            if hasattr(self, "paiements_header_label"):
                self.paiements_header_label.configure(text=t("paiements.header_title"))
            if hasattr(self, "paiements_clients_heading"):
                self.paiements_clients_heading.configure(text=t("paiements.clients_unpaid_heading"))
            if hasattr(self, "paiements_docs_heading"):
                self.paiements_docs_heading.configure(text=t("paiements.docs_unpaid_heading"))
            if hasattr(self, "paiements_client_search"):
                self.paiements_client_search.configure(placeholder_text=t("paiements.search_client_placeholder"))
            if hasattr(self, "paiements_search_btn"):
                self.paiements_search_btn.configure(text=t("common.search"))
            if hasattr(self, "paiements_doc_search"):
                self.paiements_doc_search.configure(placeholder_text=t("paiements.search_doc_placeholder"))
            if hasattr(self, "paiements_doc_search_btn"):
                self.paiements_doc_search_btn.configure(text=t("common.search"))
            if hasattr(self, "paiements_refresh_btn"):
                self.paiements_refresh_btn.configure(text=t("paiements.button_refresh"))
            if hasattr(self, "paiements_record_btn"):
                self.paiements_record_btn.configure(text=t("paiements.button_record_payment"))
            if hasattr(self, "paiements_mark_paid_btn"):
                self.paiements_mark_paid_btn.configure(text=t("paiements.button_mark_paid"))
            if hasattr(self, "clients_unpaid_tree"):
                try:
                    cols = list(self.clients_unpaid_tree["columns"])
                    # Expected: ClientID, Customer, Impayé/Outstanding
                    keys = ["ClientID", "documents.col_client", "paiements.col_unpaid"]
                    for col_id, k in zip(cols, keys):
                        txt = "ClientID" if k == "ClientID" else t(k)
                        self.clients_unpaid_tree.heading(col_id, text=txt)
                except Exception:
                    pass
                except Exception:
                    pass
            if hasattr(self, "client_docs_tree"):
                try:
                    cols = list(self.client_docs_tree["columns"])
                    keys = ["ID", "client_dialog.col_number", "client_dialog.col_type", "documents.col_ttc", "common.paid", "paiements.col_remaining"]
                    for col_id, k in zip(cols, keys):
                        txt = "ID" if k == "ID" else t(k)
                        self.client_docs_tree.heading(col_id, text=txt)
                except Exception:
                    pass
        except Exception:
            pass
        # Reload paiements data to refresh Paid/Outstanding texts
        try:
            if hasattr(self, "load_paiements"):
                self.load_paiements()
        except Exception:
            pass


    def _setup_ttk_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(
            "Treeview",
            rowheight=self.ui["tree_rowheight"],
            font=self.ui["font_body"],
            background=self.ui.get("tree_odd", "#F6F8FB"),
            foreground=self.ui.get("tree_text", "#000000"),
            fieldbackground=self.ui.get("tree_even", "#FFFFFF"),
            borderwidth=1,
            relief="solid",
        )

        style.configure(
            "Treeview.Heading",
            font=(self.ui["font_family"], 11, "bold"),
            padding=(8, 8),
            background="#F0F3F7",
            foreground="#1f2d3d",
            relief="flat",
            borderwidth=1,
        )

        try:
            style.map(
                "Treeview.Heading",
                background=[("active", "#E6EDF3")],
                relief=[("pressed", "sunken")],
            )
        except Exception:
            pass

    def _apply_zebra(self, tree):
        if not tree:
            return
        tree.tag_configure("even", background=self.ui["tree_even"])
        tree.tag_configure("odd", background=self.ui["tree_odd"])
        # Configure payment status tags if tree is clients_unpaid_tree
        if hasattr(self, "clients_unpaid_tree") and tree == self.clients_unpaid_tree:
            tree.tag_configure("paid", foreground="#22c55e")  # Green
            tree.tag_configure("unpaid", foreground="#ef4444")  # Red
        for idx, item in enumerate(tree.get_children()):
            # Get existing tags and add zebra tag
            current_tags = tree.item(item, "tags")
            if not current_tags:
                current_tags = ()
            elif isinstance(current_tags, str):
                current_tags = (current_tags,)
            zebra_tag = "even" if idx % 2 == 0 else "odd"
            new_tags = (zebra_tag,) + current_tags
            tree.item(item, tags=new_tags)

    def _require_admin(self):
        """Confirm against the database that the session user is still admin."""
        if not self.user.get("id"):
            messagebox.showerror(
                t("common.error"), t("admin.error_not_authorized")
            )
            return False

        conn = sqlite3.connect(self.db_path)
        try:
            require_admin(conn, self.user["id"])
        except AccountError:
            messagebox.showerror(
                t("common.error"), t("admin.error_not_authorized")
            )
            return False
        except Exception as e:
            messagebox.showerror(t("common.error"), str(e))
            return False
        finally:
            conn.close()
        return True

    def _confirm_admin_password(self):
        """Re-authenticate the current admin before a destructive action."""
        from tkinter import simpledialog

        parent = getattr(self, "_admin_win", None)
        try:
            if parent is None or not parent.winfo_exists():
                parent = self
        except Exception:
            parent = self

        password = simpledialog.askstring(
            t("admin.password_confirm_title"),
            t("admin.password_confirm_prompt"),
            show="*",
            parent=parent,
        )
        if not password:
            return False

        conn = None
        row = None
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                "SELECT password FROM users WHERE id = ?",
                (self.user.get("id"),),
            ).fetchone()
        except Exception:
            row = None
        finally:
            if conn is not None:
                conn.close()

        stored = row[0] if row else None
        if not stored or not verify_password(password, stored):
            messagebox.showerror(
                t("common.error"), t("admin.error_wrong_password")
            )
            return False
        return True

    def _return_to_login(self):
        """Close the app and show the login window after a database swap."""
        parent = getattr(self, "_parent", None)
        try:
            self.destroy()
        finally:
            if parent is not None:
                try:
                    parent.deiconify()
                    parent.lift()
                    try:
                        parent.password_entry.delete(0, "end")
                    except Exception:
                        pass
                except Exception:
                    pass

    def open_change_password_dialog(self):
        if not self.user.get("id"):
            return
        prompt_password_change(self, self.user["id"], self.db_path)

    def _admin_user_dialog(self, parent, user_id=None, on_success=None):
        if not self._require_admin():
            return

        existing = None
        if user_id is not None:
            conn = sqlite3.connect(self.db_path)
            try:
                existing = get_user_by_id(conn, user_id)
            finally:
                conn.close()
            if not existing:
                messagebox.showerror(
                    t("common.error"), t("validation.user_not_found")
                )
                return

        title = (
            t("admin.user_dialog_edit_title")
            if existing
            else t("admin.user_dialog_create_title")
        )
        dialog = ctk.CTkToplevel(parent)
        dialog.title(title)
        dialog.geometry("440x580")
        dialog.resizable(False, False)
        try:
            dialog.transient(parent)
            dialog.grab_set()
        except Exception:
            pass

        ctk.CTkLabel(
            dialog, text=title, font=("Arial", 16, "bold")
        ).pack(pady=(14, 4), padx=20)

        fields = {}

        def add_field(key, label, show=None, value=""):
            ctk.CTkLabel(dialog, text=label).pack(
                anchor="w", padx=30, pady=(4, 0)
            )
            entry = ctk.CTkEntry(dialog, width=360, show=show)
            entry.pack(padx=30, pady=(2, 4))
            if value:
                entry.insert(0, value)
            fields[key] = entry
            return entry

        username_entry = add_field(
            "username",
            t("admin.user_username"),
            value=existing["username"] if existing else "",
        )
        if existing:
            username_entry.configure(state="disabled")
        add_field(
            "email",
            t("admin.user_email"),
            value=existing["email"] if existing else "",
        )
        add_field(
            "fullname",
            t("admin.user_fullname"),
            value=existing["full_name"] if existing else "",
        )

        if existing:
            ctk.CTkLabel(
                dialog,
                text=t("admin.user_password_hint"),
                text_color="gray",
            ).pack(anchor="w", padx=30)
        add_field(
            "password", t("admin.user_password"), show="*"
        )
        add_field(
            "confirm", t("admin.user_confirm_password"), show="*"
        )

        is_admin_var = tk.BooleanVar(
            value=bool(existing["is_admin"]) if existing else False
        )
        is_active_var = tk.BooleanVar(
            value=bool(existing["is_active"]) if existing else True
        )
        ctk.CTkCheckBox(
            dialog, text=t("admin.user_admin"), variable=is_admin_var
        ).pack(anchor="w", padx=30, pady=(6, 0))
        ctk.CTkCheckBox(
            dialog, text=t("admin.user_active"), variable=is_active_var
        ).pack(anchor="w", padx=30, pady=(4, 0))

        def submit():
            password = fields["password"].get()
            confirm = fields["confirm"].get()

            if bool(password) != bool(confirm):
                messagebox.showerror(
                    t("common.error"), t("validation.password_mismatch")
                )
                return

            conn = sqlite3.connect(self.db_path)
            try:
                if existing:
                    updated = admin_update_user(
                        conn,
                        self.user["id"],
                        existing["id"],
                        fields["email"].get(),
                        fields["fullname"].get(),
                        is_admin_var.get(),
                        is_active_var.get(),
                        new_password=password or None,
                        confirm_password=confirm,
                    )
                    if existing["id"] == self.user.get("id"):
                        self.user.update(updated)
                else:
                    admin_create_user(
                        conn,
                        self.user["id"],
                        fields["username"].get(),
                        password,
                        fields["email"].get(),
                        fields["fullname"].get(),
                        is_admin=is_admin_var.get(),
                        must_change_password=True,
                    )
            except AccountError as exc:
                conn.rollback()
                messagebox.showerror(
                    t("common.error"), t(exc.key, **exc.kwargs)
                )
                return
            except Exception as exc:
                conn.rollback()
                messagebox.showerror(t("common.error"), str(exc))
                return
            finally:
                conn.close()

            messagebox.showinfo(
                t("msgbox.success"),
                t("admin.user_updated")
                if existing
                else t("admin.user_created"),
            )
            dialog.destroy()
            if on_success:
                on_success()

        buttons = ctk.CTkFrame(dialog, fg_color="transparent")
        buttons.pack(pady=(12, 12))
        ctk.CTkButton(
            buttons, text=t("common.save"), command=submit, width=140
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            buttons,
            text=t("common.cancel"),
            command=dialog.destroy,
            fg_color="gray",
            hover_color="darkgray",
            width=140,
        ).pack(side="left", padx=6)

        try:
            (fields["email"] if existing else username_entry).focus_set()
        except Exception:
            pass

    def _admin_delete_user(self, parent, user_id, on_success=None):
        if not self._require_admin():
            return

        conn = sqlite3.connect(self.db_path)
        try:
            user = get_user_by_id(conn, user_id)
        finally:
            conn.close()
        if not user:
            messagebox.showerror(
                t("common.error"), t("validation.user_not_found")
            )
            return
        if not messagebox.askyesno(
            t("admin.button_delete_user"),
            t("admin.confirm_delete_user", username=user["username"]),
        ):
            return

        conn = sqlite3.connect(self.db_path)
        try:
            admin_delete_user(conn, self.user["id"], user_id)
        except AccountError as exc:
            conn.rollback()
            messagebox.showerror(
                t("common.error"), t(exc.key, **exc.kwargs)
            )
            return
        except Exception as exc:
            conn.rollback()
            messagebox.showerror(t("common.error"), str(exc))
            return
        finally:
            conn.close()

        messagebox.showinfo(
            t("msgbox.success"), t("admin.user_deleted")
        )
        if on_success:
            on_success()

    def open_admin_settings(self):
        """Open administration settings in a separate French-titled window."""
        if not self._require_admin():
            return
        try:
            admin_win = ctk.CTkToplevel(self)

            try:
                self._admin_win = admin_win
            except Exception:
                pass
            admin_win.title(t("admin.window_title"))
            admin_win.geometry("900x600")
            try:
                admin_win.transient(self)
                admin_win.grab_set()
            except Exception as e:
                print(f"Warning: could not set modal grab for admin window: {e}")

            def _on_admin_close():
                try:
                    self._admin_win = None
                except Exception:
                    pass
                try:
                    admin_win.destroy()
                except Exception:
                    pass

            try:
                admin_win.protocol("WM_DELETE_WINDOW", _on_admin_close)
            except Exception:
                pass

            header = ctk.CTkFrame(admin_win, fg_color="transparent")
            header.pack(fill="x", padx=16, pady=(12, 6))
            ctk.CTkLabel(
                header,
                text=t("admin.window_title"),
                font=self.ui.get("font_h1", ("Arial", 24, "bold")),
            ).pack(side="left")

            content = ctk.CTkFrame(admin_win, fg_color="transparent")
            content.pack(fill="both", expand=True, padx=16, pady=8)

            sidebar = ctk.CTkFrame(content, width=240, fg_color="transparent")
            sidebar.pack(side="left", fill="y", padx=(0, 12), pady=6)

            detail = ctk.CTkFrame(content, fg_color="transparent")
            detail.pack(side="right", fill="both", expand=True, pady=6)

            sections = [
                (t("admin.section_users"), "users"),
                (t("admin.section_system"), "system"),
            ]

            btns = {}

            def clear_detail():
                for w in detail.winfo_children():
                    w.destroy()

            def show_users_section():
                clear_detail()
                ctk.CTkLabel(
                    detail, text=t("admin.section_users"), font=self.ui.get("font_h2")
                ).pack(anchor="w", pady=(6, 8))

                tree_frame = ctk.CTkFrame(detail, fg_color="transparent")
                tree_frame.pack(fill="both", expand=True)
                cols = (
                    "ID",
                    t("login.username_placeholder"),
                    t("register.email_placeholder"),
                    t("register.fullname_placeholder"),
                    t("admin.col_admin"),
                    t("admin.col_active"),
                )
                users_tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
                for c in cols:
                    users_tree.heading(c, text=c)
                    users_tree.column(c, width=110, anchor="center")
                users_tree.column(t("register.email_placeholder"), width=190, anchor="w")
                scrollbar = ttk.Scrollbar(
                    tree_frame, orient="vertical", command=users_tree.yview
                )
                scrollbar.pack(side="right", fill="y")
                users_tree.configure(yscrollcommand=scrollbar.set)
                users_tree.pack(fill="both", expand=True)

                def populate_users():
                    for item in users_tree.get_children():
                        users_tree.delete(item)
                    conn = None
                    try:
                        conn = sqlite3.connect(self.db_path)
                        cur = conn.cursor()
                        cur.execute(
                            "SELECT id, username, email, full_name, is_admin, is_active "
                            "FROM users ORDER BY username"
                        )
                        for r in cur.fetchall():
                            users_tree.insert(
                                "",
                                "end",
                                values=(
                                    r[0],
                                    r[1],
                                    r[2] or "",
                                    r[3] or "",
                                    t("common.yes") if r[4] else t("common.no"),
                                    t("common.yes") if r[5] else t("common.no"),
                                ),
                            )
                    except Exception as e:
                        print(f"Failed to load users for admin panel: {e}")
                    finally:
                        if conn is not None:
                            conn.close()

                def selected_user_id():
                    selection = users_tree.selection()
                    if not selection:
                        messagebox.showinfo(
                            t("common.error"), t("admin.error_select_user")
                        )
                        return None
                    try:
                        return int(users_tree.item(selection[0], "values")[0])
                    except (TypeError, ValueError, IndexError):
                        return None

                def create_selected():
                    self._admin_user_dialog(
                        admin_win, None, on_success=populate_users
                    )

                def edit_selected():
                    uid = selected_user_id()
                    if uid is not None:
                        self._admin_user_dialog(
                            admin_win, uid, on_success=populate_users
                        )

                def delete_selected():
                    uid = selected_user_id()
                    if uid is not None:
                        self._admin_delete_user(
                            admin_win, uid, on_success=populate_users
                        )

                populate_users()
                users_tree.bind(
                    "<Double-1>", lambda _event: edit_selected()
                )

                footer = ctk.CTkFrame(detail, fg_color="transparent")
                footer.pack(fill="x", pady=(8, 0))
                ctk.CTkButton(
                    footer,
                    text=t("admin.button_create_user"),
                    command=create_selected,
                ).pack(side="left")
                ctk.CTkButton(
                    footer,
                    text=t("admin.button_edit_selection"),
                    command=edit_selected,
                ).pack(side="left", padx=8)
                ctk.CTkButton(
                    footer,
                    text=t("admin.button_delete_user"),
                    command=delete_selected,
                    fg_color=self.ui.get("danger_fg", "#FF4B4B"),
                    hover_color=self.ui.get("danger_hover", "#FF0000"),
                ).pack(side="left")

            def show_system_section():
                clear_detail()
                ctk.CTkLabel(
                    detail, text=t("admin.section_system"), font=self.ui.get("font_h2")
                ).pack(anchor="w", pady=(6, 8))
                sys_frame = ctk.CTkFrame(detail, fg_color="transparent")
                sys_frame.pack(fill="both", expand=True)

                ctk.CTkLabel(
                    sys_frame,
                    text=t("admin.backup_restore_heading"),
                    font=self.ui.get("font_body"),
                ).pack(anchor="w", pady=(4, 2))
                ctk.CTkButton(
                    sys_frame,
                    text=t("admin.button_backup"),
                    command=self._admin_backup_db,
                ).pack(anchor="w")
                ctk.CTkButton(
                    sys_frame,
                    text=t("admin.button_restore"),
                    command=self._admin_restore_db,
                ).pack(anchor="w", pady=(6, 0))

                ctk.CTkLabel(sys_frame, text="", font=self.ui.get("font_body")).pack(
                    pady=(8, 0)
                )

                def _wipe_confirm():
                    if not self._confirm_admin_password():
                        return
                    if not messagebox.askokcancel(
                        t("admin.button_wipe"),
                        t("admin.confirm_wipe_no_backup"),
                    ):
                        return
                    self._admin_wipe_db(skip_backup=True)

                ctk.CTkButton(
                    sys_frame,
                    text=t("admin.button_wipe"),
                    command=_wipe_confirm,
                    fg_color=self.ui.get("danger_fg", "#FF4B4B"),
                    hover_color=self.ui.get("danger_hover", "#FF0000"),
                ).pack(anchor="w", pady=(10, 0))

                ctk.CTkLabel(
                    sys_frame, text=t("admin.options_heading"), font=self.ui.get("font_body")
                ).pack(anchor="w", pady=(12, 2))

                def _toggle_reserve_var():
                    pass

                reserve_frame = ctk.CTkFrame(sys_frame, fg_color="transparent")
                reserve_frame.pack(anchor="w", pady=(4, 2))
                ctk.CTkLabel(
                    reserve_frame,
                    text=t("admin.toggle_auto_reserve"),
                    font=self.ui.get("font_body"),
                ).pack(side="left")
                ctk.CTkSwitch(reserve_frame, command=_toggle_reserve_var).pack(
                    side="left", padx=8
                )

            for text, key in sections:
                b = ctk.CTkButton(
                    sidebar, text=text, anchor="w", width=220, corner_radius=8
                )
                b.pack(fill="x", pady=6)
                btns[key] = b

            btns["users"].configure(command=show_users_section)
            btns["system"].configure(command=show_system_section)

            show_users_section()

            bottom = ctk.CTkFrame(admin_win, fg_color="transparent")
            bottom.pack(fill="x", padx=16, pady=10)
            ctk.CTkButton(
                bottom,
                text=t("common.close"),
                command=admin_win.destroy,
                fg_color="gray",
                hover_color="darkgray",
            ).pack(side="right")
        except Exception as e:
            print(f"Failed to open admin settings: {e}")

    def open_about_dialog(self):
        try:
            dialog = ctk.CTkToplevel(self)
            dialog.title(t("about.dialog_title"))
            dialog.geometry("420x220")
            try:
                dialog.transient(self)
                dialog.grab_set()
            except Exception as e:
                print(f"Warning: could not set modal grab for about dialog: {e}")

            ctk.CTkLabel(dialog, text="GCS", font=("Arial", 18, "bold")).pack(
                pady=(16, 4)
            )
            ctk.CTkLabel(dialog, text=t("about.version_mainapp"), font=("Arial", 12)).pack(
                pady=(0, 8)
            )

            email_lbl = ctk.CTkLabel(
                dialog,
                text="sedikdarragi@gmail.com",
                text_color="blue",
                cursor="hand2",
                font=("Arial", 11, "underline"),
            )
            email_lbl.pack(pady=(4, 2))
            email_lbl.bind(
                "<Button-1>", lambda e: webbrowser.open("mailto:sedikdarragi@gmail.com")
            )

            gh_lbl = ctk.CTkLabel(
                dialog,
                text="https://github.com/SedikDarragi",
                text_color="blue",
                cursor="hand2",
                font=("Arial", 11, "underline"),
            )
            gh_lbl.pack(pady=(2, 8))
            gh_lbl.bind(
                "<Button-1>",
                lambda e: webbrowser.open("https://github.com/SedikDarragi"),
            )

            ctk.CTkButton(
                dialog,
                text=t("common.close"),
                command=dialog.destroy,
                fg_color="gray",
                hover_color="darkgray",
            ).pack(pady=8)
        except Exception:
            messagebox.showinfo(
                t("login.about_link"), "GCS\nVersion: 0.1.0 beta\nContact: sedikdarragi@gmail.com"
            )

    def _admin_wipe_db(self, skip_backup: bool = False):
        """Delete the current DB and reinitialize schema.

        If skip_backup is False, offer the user a choice to backup before wiping.
        If skip_backup is True, proceed to wipe without creating any backup.
        """
        if not self._require_admin():
            return
        backup_path = None
        if not skip_backup:
            choice = messagebox.askyesnocancel(
                t("admin.wipe_title"),
                t("admin.wipe_backup_question"),
            )

            if choice is None:
                return

            if choice is True:
                path = filedialog.asksaveasfilename(
                    defaultextension=".db",
                    filetypes=[("SQLite DB", "*.db"), ("All files", "*.*")],
                    initialfile=f"{os.path.basename(self.db_path)}.backup",
                )
                if not path:
                    return
                try:
                    backup_path = backup_database(self.db_path, path)
                except DatabaseMaintenanceError as exc:
                    messagebox.showerror(
                        t("admin.backup_title"), t(exc.key, **exc.kwargs)
                    )
                    return
                except Exception as exc:
                    messagebox.showerror(
                        t("admin.backup_title"),
                        t("admin.backup_failed", error=str(exc)),
                    )
                    return

        try:
            preserved = reset_database(self.db_path)
        except DatabaseMaintenanceError as exc:
            messagebox.showerror(
                t("admin.wipe_title"), t(exc.key, **exc.kwargs)
            )
            return
        except Exception as e:
            messagebox.showerror(
                t("admin.wipe_title"), t("admin.wipe_error_reinit", error=str(e))
            )
            return

        try:
            self.init_db()
        except Exception as e:
            messagebox.showerror(
                t("admin.wipe_title"), t("admin.wipe_error_reinit", error=str(e))
            )
            return

        try:
            try:
                self.load_articles()
            except Exception:
                pass

            try:
                if hasattr(self, "load_clients"):
                    self.load_clients()
            except Exception:
                pass

            for doc_kind in ("devis", "bl", "factures", "reception"):
                try:
                    self.load_documents(doc_kind)
                except Exception:
                    pass

            try:
                self.load_paiements()
            except Exception:
                pass
        except Exception:
            pass

        try:
            if getattr(self, "_admin_win", None) is not None:
                try:
                    self._admin_win.destroy()
                except Exception:
                    pass

                try:
                    self._admin_win = None
                except Exception:
                    pass
                try:
                    self.open_admin_settings()
                except Exception:
                    pass
        except Exception:
            pass

        msg = t("admin.wipe_success")
        if preserved:
            msg += t("admin.wipe_users_kept")
        if backup_path:
            msg += t("admin.wipe_backup_created", path=backup_path)
        else:
            msg += t("admin.wipe_no_backup")

        messagebox.showinfo(t("admin.wipe_title"), msg)

    def _admin_backup_db(self):
        if not self._require_admin():
            return
        try:
            path = filedialog.asksaveasfilename(
                defaultextension=".db",
                filetypes=[("SQLite DB", "*.db"), ("All files", "*.*")],
                initialfile=os.path.basename(self.db_path),
            )
            if not path:
                return
            backup_database(self.db_path, path)
            messagebox.showinfo(
                t("admin.backup_title"), t("admin.backup_done", path=path)
            )
        except DatabaseMaintenanceError as exc:
            messagebox.showerror(
                t("admin.backup_title"), t(exc.key, **exc.kwargs)
            )
        except Exception as exc:
            messagebox.showerror(
                t("admin.backup_title"),
                t("admin.backup_failed", error=str(exc)),
            )

    def _admin_restore_db(self):
        if not self._require_admin():
            return
        try:
            src = filedialog.askopenfilename(
                filetypes=[("SQLite DB", "*.db"), ("All files", "*.*")]
            )
            if not src:
                return

            # Validate the candidate before the user is asked to confirm a
            # destructive operation. Invalid files never touch the live DB.
            try:
                validate_database_file(src)
            except DatabaseMaintenanceError as exc:
                messagebox.showerror(
                    t("admin.restore_title"), t(exc.key, **exc.kwargs)
                )
                return

            if not messagebox.askokcancel(
                t("admin.restore_title"),
                t("admin.restore_warning"),
            ):
                return

            if not self._confirm_admin_password():
                return

            try:
                backup_path = restore_database(self.db_path, src)
            except DatabaseMaintenanceError as exc:
                messagebox.showerror(
                    t("admin.restore_title"), t(exc.key, **exc.kwargs)
                )
                return
            except Exception as exc:
                messagebox.showerror(
                    t("admin.restore_title"),
                    t("admin.restore_failed", error=str(exc)),
                )
                return

            msg = t("admin.restore_done")
            if backup_path:
                msg += t("admin.restore_backup_created", path=backup_path)
            messagebox.showinfo(t("admin.restore_title"), msg)
            self._return_to_login()
        except Exception as exc:
            messagebox.showerror(
                t("admin.restore_title"),
                t("admin.restore_failed", error=str(exc)),
            )

    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            ensure_account_schema(conn)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT,
                    designation TEXT NOT NULL,
                    prix_achat INTEGER NOT NULL DEFAULT 0,
                    prix_vente INTEGER NOT NULL DEFAULT 0,
                    tva REAL NOT NULL DEFAULT 0,
                    stock INTEGER NOT NULL DEFAULT 0
                )
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS clients (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nom TEXT NOT NULL,
                    email TEXT,
                    telephone TEXT,
                    adresse TEXT,
                    matricule_fiscale TEXT
                )
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    numero TEXT NOT NULL,
                    date TEXT NOT NULL,
                    client_id INTEGER,
                    statut TEXT NOT NULL DEFAULT 'brouillon',
                    total_ht INTEGER NOT NULL DEFAULT 0,
                    total_tva INTEGER NOT NULL DEFAULT 0,
                    total_ttc INTEGER NOT NULL DEFAULT 0,
                    paid_amount INTEGER NOT NULL DEFAULT 0,
                    payment_mode TEXT NOT NULL DEFAULT 'none',
                    echeance TEXT,
                    source_bl_id INTEGER,
                    converted_to_id INTEGER,
                    converted_at TEXT,
                    discount_enabled INTEGER NOT NULL DEFAULT 0,
                    discount_value REAL NOT NULL DEFAULT 0,
                    FOREIGN KEY(client_id) REFERENCES clients(id)
                )
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS document_lignes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL,
                    article_id INTEGER,
                    designation TEXT NOT NULL,
                    quantite REAL NOT NULL DEFAULT 1,
                    prix_unitaire INTEGER NOT NULL DEFAULT 0,
                    tva REAL NOT NULL DEFAULT 0,
                    total_ht INTEGER NOT NULL DEFAULT 0,
                    total_tva INTEGER NOT NULL DEFAULT 0,
                    total_ttc INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(document_id) REFERENCES documents(id)
                )
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    method TEXT,
                    note TEXT,
                    FOREIGN KEY(document_id) REFERENCES documents(id)
                )
                """
            )

            for ddl in [
                "ALTER TABLE document_lignes ADD COLUMN article_id INTEGER",
                "ALTER TABLE articles ADD COLUMN prix_achat INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE articles ADD COLUMN prix_vente INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE articles ADD COLUMN tva REAL NOT NULL DEFAULT 0",
                "ALTER TABLE documents ADD COLUMN total_ht INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE documents ADD COLUMN total_tva INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE documents ADD COLUMN total_ttc INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE documents ADD COLUMN paid_amount INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE documents ADD COLUMN payment_mode TEXT NOT NULL DEFAULT 'none'",
                "ALTER TABLE documents ADD COLUMN echeance TEXT",
                "ALTER TABLE document_lignes ADD COLUMN tva REAL NOT NULL DEFAULT 0",
                "ALTER TABLE document_lignes ADD COLUMN total_ht INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE document_lignes ADD COLUMN total_tva INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE document_lignes ADD COLUMN total_ttc INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE clients ADD COLUMN matricule_fiscale TEXT",
                "ALTER TABLE documents ADD COLUMN discount_enabled INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE documents ADD COLUMN discount_value REAL NOT NULL DEFAULT 0",
                "ALTER TABLE documents ADD COLUMN converted_to_id INTEGER",
                "ALTER TABLE documents ADD COLUMN converted_at TEXT",
            ]:
                try:
                    cursor.execute(ddl)
                except sqlite3.OperationalError:
                    pass

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS sequences (
                    type TEXT PRIMARY KEY,
                    annee INTEGER NOT NULL,
                    compteur INTEGER NOT NULL
                )
                """
            )

            # Convert legacy currency-unit amounts once.  Fresh databases
            # already use INTEGER columns and are left untouched.
            migrate_money_to_cents(conn)

            conn.commit()
        finally:
            conn.close()

    def peek_next_numero(self, doc_type):
        """Preview the next number without reserving it.

        The preview is derived from both the stored sequence and the highest
        number already used by an existing document, so imported or manually
        numbered documents cannot cause a duplicate.
        """
        annee = datetime.now().year
        prefix = _numero_prefix(doc_type)

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT annee, compteur FROM sequences WHERE type = ?", (doc_type,)
            )
            row = cursor.fetchone()
            compteur = 0
            if row and row[0] == annee:
                compteur = max(int(row[1]), 0)
            compteur = max(
                compteur,
                _max_document_counter(cursor, doc_type, prefix, annee),
            )
        finally:
            conn.close()

        return f"{prefix}-{annee}-{compteur + 1:04d}"

    def get_next_numero(self, doc_type, cursor=None):
        """Reserve and return the next document number.

        When ``cursor`` is provided the sequence is updated inside the
        caller's transaction; otherwise a short ``BEGIN IMMEDIATE``
        transaction makes the reservation atomic across app instances.
        """
        annee = datetime.now().year
        prefix = _numero_prefix(doc_type)
        own_conn = cursor is None
        conn = None
        if own_conn:
            conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")

        try:
            cursor.execute(
                "SELECT annee, compteur FROM sequences WHERE type = ?", (doc_type,)
            )
            row = cursor.fetchone()
            compteur = 0
            if row and row[0] == annee:
                compteur = max(int(row[1]), 0)
            compteur = max(
                compteur,
                _max_document_counter(cursor, doc_type, prefix, annee),
            )
            compteur += 1

            cursor.execute(
                """
                INSERT INTO sequences (type, annee, compteur) VALUES (?, ?, ?)
                ON CONFLICT(type) DO UPDATE SET
                    annee = excluded.annee,
                    compteur = excluded.compteur
                """,
                (doc_type, annee, compteur),
            )

            if own_conn:
                conn.commit()
        except Exception:
            if own_conn:
                conn.rollback()
            raise
        finally:
            if own_conn and conn is not None:
                conn.close()

        return f"{prefix}-{annee}-{compteur:04d}"

    def create_articles_frame(self):
        frame = ctk.CTkFrame(self.main_frame, corner_radius=0, fg_color="transparent")

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 20))

        self.articles_header_label = ctk.CTkLabel(header, text=t("articles.header_title"), font=self.ui["font_h1"])
        self.articles_header_label.pack(side="left")

        self.delete_article_btn = ctk.CTkButton(
            header,
            text=t("common.delete"),
            command=self.delete_selected_article,
            fg_color=self.ui.get("danger_fg", "#FF4B4B"),
            hover_color=self.ui.get("danger_hover", "#FF0000"),
            width=120,
            state="disabled",
        )
        self.delete_article_btn.pack(side="right", padx=(6, 0))

        self.articles_add_btn = ctk.CTkButton(
            header, text=t("articles.button_add"), command=self.show_add_article_dialog
        )
        self.articles_add_btn.pack(side="right")

        search_frame = ctk.CTkFrame(frame, fg_color="transparent")
        search_frame.pack(fill="x", pady=(0, 20))

        # Container for search entry and dropdown
        self.article_search_container = ctk.CTkFrame(
            search_frame, fg_color="transparent"
        )
        self.article_search_container.pack(side="left", padx=(0, 10))

        self.article_search_entry = ctk.CTkEntry(
            self.article_search_container,
            width=350,
            placeholder_text=t("articles.search_placeholder"),
        )
        self.article_search_entry.pack(fill="x")

        # Autocomplete dropdown for articles
        self.article_dropdown = ctk.CTkFrame(
            self.article_search_container, fg_color="gray20", corner_radius=6
        )
        self.article_dropdown.pack(fill="x", pady=(2, 0), expand=False)
        self.article_dropdown.pack_forget()  # Hide initially

        self.article_dropdown_items = []

        # Search button
        self.articles_search_btn = ctk.CTkButton(
            search_frame, text=t("common.search"), command=self.search_articles
        )
        self.articles_search_btn.pack(side="left")

        # Bind events for autocomplete
        self.article_search_entry.bind(
            "<KeyRelease>", self.on_article_search_keyrelease
        )
        self.article_search_entry.bind("<Return>", self.search_articles)
        self.article_search_entry.bind(
            "<Escape>", lambda e: self.hide_article_dropdown()
        )
        self.article_search_entry.bind(
            "<FocusOut>", lambda e: self.after(150, self.hide_article_dropdown)
        )
        self.article_dropdown.bind(
            "<Button-1>", lambda e: "break"
        )  # Prevent focus loss

        columns = (
            "ID",
            t("articles.col_reference"),
            t("articles.col_designation"),
            t("articles.col_pa"),
            t("articles.col_pv"),
            t("articles.col_tva"),
            t("articles.col_margin"),
            t("articles.col_stock"),
        )
        self.articles_tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse"
        )

        self.articles_tree.heading("ID", text="ID", anchor="center")
        self.articles_tree.heading(t("articles.col_reference"), text=t("articles.heading_reference"), anchor="w")
        self.articles_tree.heading(t("articles.col_designation"), text=t("articles.heading_designation"), anchor="w")
        self.articles_tree.heading(t("articles.col_pa"), text=t("articles.heading_pa"), anchor="e")
        self.articles_tree.heading(t("articles.col_pv"), text=t("articles.heading_pv"), anchor="e")
        self.articles_tree.heading(t("articles.col_tva"), text=t("articles.heading_tva"), anchor="center")
        self.articles_tree.heading(t("articles.col_margin"), text=t("articles.heading_margin"), anchor="e")
        self.articles_tree.heading(t("articles.col_stock"), text=t("articles.heading_stock"), anchor="center")

        self.articles_tree.column("ID", width=60, anchor="center")
        self.articles_tree.column(t("articles.col_reference"), width=140, anchor="w")
        self.articles_tree.column(t("articles.col_designation"), width=340, anchor="w")
        self.articles_tree.column(t("articles.col_pa"), width=120, anchor="e")
        self.articles_tree.column(t("articles.col_pv"), width=120, anchor="e")
        self.articles_tree.column(t("articles.col_tva"), width=80, anchor="center")
        self.articles_tree.column(t("articles.col_margin"), width=120, anchor="e")
        self.articles_tree.column(t("articles.col_stock"), width=100, anchor="center")

        scrollbar = ttk.Scrollbar(
            frame, orient="vertical", command=self.articles_tree.yview
        )
        scrollbar.pack(side="right", fill="y")
        self.articles_tree.configure(yscrollcommand=scrollbar.set)
        self.articles_tree.pack(fill="both", expand=True)

        self.articles_tree.bind("<Double-1>", self.on_article_double_click)
        self.articles_tree.bind(
            "<<TreeviewSelect>>", lambda e: self._update_article_delete_button()
        )
        
        # Add sorting functionality
        self.article_sort_state = {}  # Track sort state per column
        columns = ("ID", t("articles.col_reference"), t("articles.col_designation"), t("articles.col_pa"), t("articles.col_pv"), t("articles.col_tva"), t("articles.col_margin"), t("articles.col_stock"))
        for col in columns:
            self.articles_tree.heading(col, command=lambda c=col: self.sort_articles(c))
        
        self.load_articles()
        self.frames["articles"] = frame

    def sort_articles(self, column):
        """Sort articles by column (ascending/descending toggle)."""
        if not hasattr(self, "articles_tree"):
            return
        
        # Get current sort state for this column
        current_state = self.article_sort_state.get(column, False)  # False = ascending
        new_state = not current_state  # Toggle
        self.article_sort_state[column] = new_state
        
        # Update all headers to show arrows
        arrow = "▲" if new_state else "▼"
        all_cols = ["ID", t("articles.col_reference"), t("articles.col_designation"), t("articles.col_pa"), t("articles.col_pv"), t("articles.col_tva"), t("articles.col_margin"), t("articles.col_stock")]
        for col in all_cols:
            if col == column:
                self.articles_tree.heading(col, text=f"{col} {arrow}")
            else:
                # Remove arrow from other columns
                clean_col = col.replace(" ▲", "").replace(" ▼", "")
                self.articles_tree.heading(col, text=clean_col)
        
        # Get all items from tree
        items = [(self.articles_tree.item(item, "values"), item) for item in self.articles_tree.get_children()]
        
        # Column index mapping
        col_index = {
            "ID": 0,
            t("articles.col_reference"): 1,
            t("articles.col_designation"): 2,
            t("articles.col_pa"): 3,
            t("articles.col_pv"): 4,
            t("articles.col_tva"): 5,
            t("articles.col_margin"): 6,
            t("articles.col_stock"): 7,
        }
        
        idx = col_index.get(column, 0)
        
        # Sort items
        def sort_key(item):
            values = item[0]
            val = values[idx]
            
            # Handle numeric columns
            if column in ["ID", t("articles.col_pa"), t("articles.col_pv"), t("articles.col_tva"), t("articles.col_margin"), t("articles.col_stock")]:
                try:
                    return float(val)
                except:
                    return 0.0
            # Handle text columns
            else:
                return str(val).lower()
        
        items.sort(key=sort_key, reverse=new_state)
        
        # Reinsert items in sorted order
        for item in self.articles_tree.get_children():
            self.articles_tree.delete(item)
        
        for values, item_id in items:
            self.articles_tree.insert("", "end", values=values)
        
        self._apply_zebra(self.articles_tree)

    def delete_selected_article(self):
        if not hasattr(self, "articles_tree"):
            messagebox.showerror(t("common.error"), t("articles.error_no_list"))
            return
        sel = self.articles_tree.selection()
        if not sel:
            messagebox.showerror(t("common.error"), t("articles.error_select_to_delete"))
            return
        try:
            vals = self.articles_tree.item(sel[0], "values")
        except Exception:
            messagebox.showerror(t("common.error"), t("articles.error_read_selection"))
            return
        if not vals:
            messagebox.showerror(t("common.error"), t("articles.error_select_to_delete"))
            return
        try:
            article_id = int(vals[0])
        except Exception:
            messagebox.showerror(t("common.error"), t("articles.error_invalid_id"))
            return

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT COUNT(*) FROM document_lignes WHERE article_id = ?",
                (article_id,),
            )
            row = cur.fetchone()
            ref_count = row[0] if row else 0

            if ref_count > 0:
                if not messagebox.askyesno(
                    t("common.confirm"),
                    t("articles.confirm_delete_referenced", count=ref_count),
                    icon="warning",
                ):
                    return

                # Keep historical document lines intact. The line still
                # carries its designation, quantity, price and VAT; only the
                # link to the catalog product is removed.
                cur.execute(
                    "UPDATE document_lignes SET article_id = NULL WHERE article_id = ?",
                    (article_id,),
                )
            elif not messagebox.askyesno(
                t("common.confirm"), t("articles.confirm_delete")
            ):
                return

            cur.execute("DELETE FROM articles WHERE id = ?", (article_id,))
            conn.commit()
        except Exception as e:
            conn.rollback()
            messagebox.showerror(
                t("common.error"), t("articles.error_delete", error=str(e))
            )
            return
        finally:
            conn.close()

        try:
            self.load_articles()
        except Exception:
            pass
        try:
            for doc_type in ("devis", "bl", "factures", "reception"):
                self.load_documents(doc_type)
        except Exception:
            pass

        messagebox.showinfo(t("common.deleted"), t("articles.deleted_success"))

    def _update_article_delete_button(self):
        try:
            btn = getattr(self, "delete_article_btn", None)
            if not btn or not hasattr(self, "articles_tree"):
                return
            sel = self.articles_tree.selection()
            btn.configure(state=("normal" if sel else "disabled"))
        except Exception:
            pass

    def load_articles(self, search_query=None):
        for item in self.articles_tree.get_children():
            self.articles_tree.delete(item)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            if search_query:
                q = str(search_query).strip()
                # Handle the new format "ID - Reference - Designation"
                if " - " in q:
                    parts = q.split(" - ")
                    # Extract ID from the first part if it's a number
                    id_part = parts[0].strip() if parts else ""
                    ref_part = parts[1].strip() if len(parts) > 1 else ""
                    des_part = parts[2].strip() if len(parts) > 2 else ""

                    # Build conditions based on available parts
                    conditions = []
                    params = []

                    if id_part and id_part.isdigit():
                        conditions.append("CAST(id AS TEXT) LIKE ?")
                        params.append(f"%{id_part}%")
                    if ref_part:
                        conditions.append("reference LIKE ?")
                        params.append(f"%{ref_part}%")
                    if des_part:
                        conditions.append("designation LIKE ?")
                        params.append(f"%{des_part}%")

                    # If no specific parts found, search with full query
                    if not conditions:
                        conditions = [
                            "CAST(id AS TEXT) LIKE ? OR reference LIKE ? OR designation LIKE ?"
                        ]
                        params = [f"%{q}%", f"%{q}%", f"%{q}%"]

                    where_clause = " OR ".join(conditions)
                    sql = f"""
                        SELECT DISTINCT id, reference, designation, prix_achat, prix_vente, tva, stock
                        FROM articles
                        WHERE {where_clause}
                        ORDER BY designation
                        """
                    cursor.execute(sql, tuple(params))
                else:
                    cursor.execute(
                        """
                        SELECT id, reference, designation, prix_achat, prix_vente, tva, stock
                        FROM articles
                        WHERE CAST(id AS TEXT) LIKE ? OR
                              reference LIKE ? OR
                              designation LIKE ?
                        ORDER BY designation
                        """,
                        (f"%{q}%", f"%{q}%", f"%{q}%"),
                    )
            else:
                cursor.execute(
                    "SELECT id, reference, designation, prix_achat, prix_vente, tva, stock FROM articles ORDER BY designation"
                )

            for row in cursor.fetchall():
                pa = as_cents(row[3])
                pv = as_cents(row[4])
                tva = float(row[5] or 0)
                marge = pv - pa
                self.articles_tree.insert(
                    "",
                    "end",
                    values=(
                        row[0],
                        row[1] or "",
                        row[2] or "",
                        format_money(pa),
                        format_money(pv),
                        f"{tva:.2f}",
                        format_money(marge),
                        row[6] if row[6] is not None else 0,
                    ),
                )
        finally:
            conn.close()

        self._apply_zebra(self.articles_tree)

    def on_article_search_keyrelease(self, event):
        """Handle key release for article search autocomplete."""
        query = self.article_search_entry.get().strip()

        if not query:
            self.hide_article_dropdown()
            return

        # Get matching articles from database (search by ID, reference, or designation)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT DISTINCT
                    CASE
                        WHEN CAST(a.id AS TEXT) LIKE ? THEN a.id || ' - ' || COALESCE(a.reference,'') || ' - ' || COALESCE(a.designation,'')
                        WHEN a.reference LIKE ? THEN a.id || ' - ' || COALESCE(a.reference,'') || ' - ' || COALESCE(a.designation,'')
                        ELSE a.id || ' - ' || COALESCE(a.reference,'') || ' - ' || COALESCE(a.designation,'')
                    END as display_value,
                    a.id,
                    a.reference,
                    a.designation
                FROM articles a
                WHERE CAST(a.id AS TEXT) LIKE ? OR
                      a.reference LIKE ? OR
                      a.designation LIKE ?
                ORDER BY
                    CASE
                        WHEN CAST(a.id AS TEXT) LIKE ? THEN 1
                        WHEN a.reference LIKE ? THEN 2
                        ELSE 3
                    END,
                    a.designation
                LIMIT 5
                """,
                (
                    f"%{query}%",
                    f"%{query}%",
                    f"%{query}%",
                    f"%{query}%",
                    f"%{query}%",
                    f"%{query}%",
                    f"%{query}%",
                ),
            )
            results = cursor.fetchall()
        finally:
            conn.close()

        if results:
            # Show dropdown with suggestions (use display_value for display)
            suggestions = []
            for row in results:
                display_value = row[0]
                suggestions.append(display_value)
            self.show_article_dropdown(suggestions)
        else:
            self.hide_article_dropdown()

    def show_article_dropdown(self, items):
        """Show autocomplete dropdown with suggestions."""
        # Clear existing items
        for widget in self.article_dropdown_items:
            widget.destroy()
        self.article_dropdown_items = []

        # Add new items
        for item in items[:5]:  # Max 5 suggestions
            btn = ctk.CTkButton(
                self.article_dropdown,
                text=item,
                command=lambda x=item: self.select_article_suggestion(x),
                fg_color="transparent",
                text_color="white",
                hover_color="gray40",
                anchor="w",
                height=28,
                corner_radius=4,
            )
            btn.pack(fill="x", padx=2, pady=1)
            self.article_dropdown_items.append(btn)

        # Show dropdown
        self.article_dropdown.pack(fill="x", pady=(2, 0), expand=False)
        self.article_search_entry.focus_set()

    def hide_article_dropdown(self):
        """Hide autocomplete dropdown."""
        self.article_dropdown.pack_forget()
        for widget in self.article_dropdown_items:
            widget.destroy()
        self.article_dropdown_items = []

    def select_article_suggestion(self, suggestion):
        """Select an autocomplete suggestion."""
        self.article_search_entry.delete(0, "end")
        self.article_search_entry.insert(0, suggestion)
        self.hide_article_dropdown()
        # Force the search to execute immediately
        self.after(10, lambda: self.search_articles())

    def search_articles(self, event=None):
        query = self.article_search_entry.get().strip()
        self.load_articles(query if query else None)
        self.hide_article_dropdown()

    def on_client_search_keyrelease(self, event):
        """Handle key release for client search autocomplete."""
        query = self.client_search_entry.get().strip()

        if not query:
            self.hide_client_dropdown()
            return

        # Get matching clients from database (search by name, matricule fiscale, email, or phone)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT DISTINCT
                    CASE
                        WHEN CAST(c.id AS TEXT) LIKE ? THEN c.id || ' - ' || COALESCE(c.nom,'') || ' - ' || COALESCE(c.matricule_fiscale,'')
                        WHEN c.matricule_fiscale LIKE ? THEN c.id || ' - ' || COALESCE(c.nom,'') || ' - ' || COALESCE(c.matricule_fiscale,'')
                        ELSE c.id || ' - ' || COALESCE(c.nom,'') || ' - ' || COALESCE(c.matricule_fiscale,'')
                    END as display_value,
                    c.id,
                    c.nom,
                    c.matricule_fiscale
                FROM clients c
                WHERE CAST(c.id AS TEXT) LIKE ? OR
                      c.nom LIKE ? OR
                      c.matricule_fiscale LIKE ? OR
                      c.email LIKE ? OR
                      c.telephone LIKE ?
                ORDER BY
                    CASE
                        WHEN CAST(c.id AS TEXT) LIKE ? THEN 1
                        WHEN c.matricule_fiscale LIKE ? THEN 2
                        ELSE 3
                    END,
                    c.nom
                LIMIT 5
                """,
                (
                    f"%{query}%",
                    f"%{query}%",
                    f"%{query}%",
                    f"%{query}%",
                    f"%{query}%",
                    f"%{query}%",
                    f"%{query}%",
                    f"%{query}%",
                    f"%{query}%",
                ),
            )
            results = cursor.fetchall()
        finally:
            conn.close()

        if results:
            # Show dropdown with suggestions (use display_value for display)
            suggestions = []
            for row in results:
                display_value = row[0]
                suggestions.append(display_value)
            self.show_client_dropdown(suggestions)
        else:
            self.hide_client_dropdown()

    def show_client_dropdown(self, items):
        """Show autocomplete dropdown with suggestions."""
        # Clear existing items
        for widget in self.client_dropdown_items:
            widget.destroy()
        self.client_dropdown_items = []

        # Add new items
        for item in items[:5]:  # Max 5 suggestions
            btn = ctk.CTkButton(
                self.client_dropdown,
                text=item,
                command=lambda x=item: self.select_client_suggestion(x),
                fg_color="transparent",
                text_color="white",
                hover_color="gray40",
                anchor="w",
                height=28,
                corner_radius=4,
            )
            btn.pack(fill="x", padx=2, pady=1)
            self.client_dropdown_items.append(btn)

        # Show dropdown
        self.client_dropdown.pack(fill="x", pady=(2, 0), expand=False)
        self.client_search_entry.focus_set()

    def hide_client_dropdown(self):
        """Hide autocomplete dropdown."""
        self.client_dropdown.pack_forget()
        for widget in self.client_dropdown_items:
            widget.destroy()
        self.client_dropdown_items = []

    def select_client_suggestion(self, suggestion):
        """Select an autocomplete suggestion."""
        self.client_search_entry.delete(0, "end")
        self.client_search_entry.insert(0, suggestion)
        self.hide_client_dropdown()
        # Force the search to execute immediately
        self.after(10, lambda: self.search_clients())

    def search_clients(self, event=None):
        query = self.client_search_entry.get().strip()
        self.load_clients(query if query else None)
        self.hide_client_dropdown()

    def on_paiements_client_search_keyrelease(self, event):
        """Handle key release for unpaid clients search autocomplete."""
        if not hasattr(self, "paiements_client_search"):
            return

        query = self.paiements_client_search.get().strip()

        if not query:
            self.hide_paiements_client_dropdown()
            return

        # Get matching unpaid clients from database
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT DISTINCT
                    CASE
                        WHEN CAST(c.id AS TEXT) LIKE ? THEN c.id || ' - ' || COALESCE(c.nom,'')
                        ELSE c.id || ' - ' || COALESCE(c.nom,'')
                    END as display_value,
                    c.id,
                    c.nom
                FROM clients c
                LEFT JOIN documents d ON c.id = d.client_id AND d.type = 'factures'
                WHERE (CAST(c.id AS TEXT) LIKE ? OR c.nom LIKE ?)
                GROUP BY c.id
                ORDER BY c.nom
                LIMIT 5
                """,
                (f"%{query}%", f"%{query}%", f"%{query}%"),
            )
            results = cursor.fetchall()
        finally:
            conn.close()

        if results:
            suggestions = []
            for row in results:
                display_value = row[0]
                suggestions.append(display_value)
            self.show_paiements_client_dropdown(suggestions)
        else:
            self.hide_paiements_client_dropdown()

    def show_paiements_client_dropdown(self, items):
        """Show autocomplete dropdown for unpaid clients."""
        if not hasattr(self, "paiements_client_dropdown"):
            return

        # Clear existing items
        for widget in self.paiements_client_dropdown_items:
            widget.destroy()
        self.paiements_client_dropdown_items = []

        # Add new items
        for item in items[:5]:
            btn = ctk.CTkButton(
                self.paiements_client_dropdown,
                text=item,
                command=lambda x=item: self.select_paiements_client_suggestion(x),
                fg_color="transparent",
                text_color="white",
                hover_color="gray40",
                anchor="w",
                height=28,
                corner_radius=4,
            )
            btn.pack(fill="x", padx=2, pady=1)
            self.paiements_client_dropdown_items.append(btn)

        # Show dropdown
        self.paiements_client_dropdown.pack(fill="x", pady=(2, 0), expand=False)
        self.paiements_client_search.focus_set()

    def hide_paiements_client_dropdown(self):
        """Hide unpaid clients autocomplete dropdown."""
        if hasattr(self, "paiements_client_dropdown"):
            self.paiements_client_dropdown.pack_forget()
            for widget in self.paiements_client_dropdown_items:
                widget.destroy()
            self.paiements_client_dropdown_items = []

    def select_paiements_client_suggestion(self, suggestion):
        """Select an autocomplete suggestion for unpaid clients."""
        if hasattr(self, "paiements_client_search"):
            self.paiements_client_search.delete(0, "end")
            self.paiements_client_search.insert(0, suggestion)
        self.hide_paiements_client_dropdown()
        # Force the search to execute immediately
        self.after(10, self.search_paiements_clients)

    def on_paiements_doc_search_keyrelease(self, event):
        """Handle key release for unpaid documents search autocomplete."""
        if not hasattr(self, "paiements_doc_search"):
            return

        query = self.paiements_doc_search.get().strip()

        if not query:
            self.hide_paiements_doc_dropdown()
            return

        # Check if a client is selected
        sel = self.clients_unpaid_tree.selection()
        if not sel:
            self.hide_paiements_doc_dropdown()
            return

        vals = self.clients_unpaid_tree.item(sel[0], "values")
        if not vals:
            self.hide_paiements_doc_dropdown()
            return

        try:
            client_id = int(vals[0])
        except Exception:
            self.hide_paiements_doc_dropdown()
            return

        # Get matching unpaid documents for the selected client
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT DISTINCT
                    d.id || ' - ' || d.numero || ' - ' || d.type as display_value,
                    d.id,
                    d.numero,
                    d.type
                FROM documents d
                WHERE d.client_id = ?
                  AND d.type = 'factures'
                  AND (COALESCE(d.total_ttc,0) - COALESCE(d.paid_amount,0)) > 0
                  AND (CAST(d.id AS TEXT) LIKE ? OR d.numero LIKE ? OR d.type LIKE ?)
                ORDER BY d.date DESC, d.id DESC
                LIMIT 5
                """,
                (client_id, f"%{query}%", f"%{query}%", f"%{query}%"),
            )
            results = cursor.fetchall()
        finally:
            conn.close()

        if results:
            suggestions = []
            for row in results:
                display_value = row[0]
                suggestions.append(display_value)
            self.show_paiements_doc_dropdown(suggestions)
        else:
            self.hide_paiements_doc_dropdown()

    def show_paiements_doc_dropdown(self, items):
        """Show autocomplete dropdown for unpaid documents."""
        if not hasattr(self, "paiements_doc_dropdown"):
            return

        # Clear existing items
        for widget in self.paiements_doc_dropdown_items:
            widget.destroy()
        self.paiements_doc_dropdown_items = []

        # Add new items
        for item in items[:5]:
            btn = ctk.CTkButton(
                self.paiements_doc_dropdown,
                text=item,
                command=lambda x=item: self.select_paiements_doc_suggestion(x),
                fg_color="transparent",
                text_color="white",
                hover_color="gray40",
                anchor="w",
                height=28,
                corner_radius=4,
            )
            btn.pack(fill="x", padx=2, pady=1)
            self.paiements_doc_dropdown_items.append(btn)

        # Show dropdown
        self.paiements_doc_dropdown.pack(fill="x", pady=(2, 0), expand=False)
        self.paiements_doc_search.focus_set()

    def hide_paiements_doc_dropdown(self):
        """Hide unpaid documents autocomplete dropdown."""
        if hasattr(self, "paiements_doc_dropdown"):
            self.paiements_doc_dropdown.pack_forget()
            for widget in self.paiements_doc_dropdown_items:
                widget.destroy()
            self.paiements_doc_dropdown_items = []

    def select_paiements_doc_suggestion(self, suggestion):
        """Select an autocomplete suggestion for unpaid documents."""
        if hasattr(self, "paiements_doc_search"):
            self.paiements_doc_search.delete(0, "end")
            self.paiements_doc_search.insert(0, suggestion)
        self.hide_paiements_doc_dropdown()
        # Force the search to execute immediately
        self.after(10, lambda: self.search_paiements_docs())

    def show_add_article_dialog(self, article_id=None):
        dialog = ctk.CTkToplevel(self)
        dialog.title(
            t("article_dialog.title_add") if article_id is None else t("article_dialog.title_edit")
        )
        dialog.geometry("980x700")
        try:
            dialog.transient(self)
            dialog.grab_set()
        except Exception as e:
            print(f"Warning: could not set modal grab for article dialog: {e}")

        ref_value = ""
        des_value = ""
        pa_value = "0"
        pv_value = "0"
        tva_value = "0"
        stock_value = "0"

        if article_id is not None:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT reference, designation, prix_achat, prix_vente, tva, stock FROM articles WHERE id = ?",
                    (article_id,),
                )
                row = cursor.fetchone()
                if row:
                    ref_value = row[0] or ""
                    des_value = row[1] or ""
                    pa_value = money_to_input(as_cents(row[2]))
                    pv_value = money_to_input(as_cents(row[3]))
                    tva_value = str(row[4] if row[4] is not None else 0)
                    stock_value = str(row[5] if row[5] is not None else 0)
            finally:
                conn.close()

        header = ctk.CTkFrame(dialog, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 10))

        ctk.CTkLabel(
            header,
            text=t("article_dialog.header"),
            font=self.ui["font_h1"],
        ).pack(side="left")

        actions = ctk.CTkFrame(header, fg_color="transparent")
        actions.pack(side="right")

        ctk.CTkButton(
            actions,
            text=t("article_dialog.button_history"),
            state="disabled",
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "gray90"),
        ).pack(side="right")

        top = ctk.CTkFrame(dialog, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(0, 10))

        left = ctk.CTkFrame(top, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        right = ctk.CTkFrame(top, fg_color="transparent")
        right.pack(side="right")

        ctk.CTkLabel(left, text=t("articles.col_reference"), font=self.ui["font_h2"]).grid(
            row=0, column=0, sticky="w"
        )
        ctk.CTkLabel(left, text=t("articles.col_stock"), font=self.ui["font_h2"]).grid(
            row=0, column=1, sticky="w"
        )

        ref_entry = ctk.CTkEntry(left, width=220)
        ref_entry.insert(0, ref_value)
        ref_entry.grid(row=1, column=0, sticky="w", pady=(0, 8), padx=(0, 16))

        stock_entry = ctk.CTkEntry(left, width=220)
        stock_entry.insert(0, stock_value)
        stock_entry.grid(row=1, column=1, sticky="w", pady=(0, 8))

        ctk.CTkLabel(left, text=t("articles.col_designation"), font=self.ui["font_h2"]).grid(
            row=2, column=0, sticky="w", pady=(8, 0)
        )

        des_entry = ctk.CTkEntry(left, width=460)
        des_entry.insert(0, des_value)
        des_entry.grid(row=3, column=0, columnspan=2, sticky="w")

        statut_label = ctk.CTkLabel(
            right, text=t("article_dialog.status_active"), font=self.ui["font_h2"]
        )
        statut_label.pack(anchor="e")

        content = ctk.CTkFrame(dialog, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=20, pady=(10, 10))

        columns = (t("article_dialog.col_date"), t("article_dialog.col_movement"), t("article_dialog.col_qty"), t("article_dialog.col_user"))
        lines_tree = ttk.Treeview(
            content, columns=columns, show="headings", selectmode="browse"
        )
        for col in columns:
            lines_tree.heading(col, text=col)
            lines_tree.column(col, anchor="center")

        scrollbar = ttk.Scrollbar(content, orient="vertical", command=lines_tree.yview)
        scrollbar.pack(side="right", fill="y")
        lines_tree.configure(yscrollcommand=scrollbar.set)
        lines_tree.pack(fill="both", expand=True)

        def _update_line_buttons(evt=None):
            sel = lines_tree.selection()
            state = "normal" if sel else "disabled"
            try:
                edit_line_btn.configure(state=state)
                del_line_btn.configure(state=state)
            except Exception:
                pass

        lines_tree.bind("<<TreeviewSelect>>", _update_line_buttons)
        lines_tree.bind("<Double-1>", lambda e: edit_line())

        self._apply_zebra(lines_tree)

        totals_bar = ctk.CTkFrame(dialog, fg_color="transparent")
        totals_bar.pack(fill="x", padx=20, pady=(0, 10))

        totals_right = ctk.CTkFrame(totals_bar, fg_color="transparent")
        totals_right.pack(side="right")

        pv_box = ctk.CTkFrame(totals_right, fg_color="transparent")
        pv_box.pack(side="right", padx=(0, 12))
        ctk.CTkLabel(
            pv_box,
            text=t("articles.heading_pv"),
            font=self.ui["font_body"],
            text_color=("gray35", "gray70"),
        ).pack(anchor="e")
        pv_entry = ctk.CTkEntry(
            pv_box, width=150, font=self.ui["font_h2"], justify="right"
        )
        pv_entry.insert(0, pv_value)
        pv_entry.pack(anchor="e")

        tva_box = ctk.CTkFrame(totals_right, fg_color="transparent")
        tva_box.pack(side="right", padx=(0, 12))
        ctk.CTkLabel(
            tva_box,
            text=t("article_dialog.label_tva"),
            font=self.ui["font_body"],
            text_color=("gray35", "gray70"),
        ).pack(anchor="e")
        tva_entry = ctk.CTkEntry(
            tva_box, width=100, font=self.ui["font_h2"], justify="right"
        )
        tva_entry.insert(0, tva_value)
        tva_entry.pack(anchor="e")

        pa_box = ctk.CTkFrame(totals_right, fg_color="transparent")
        pa_box.pack(side="right")
        ctk.CTkLabel(
            pa_box,
            text=t("articles.heading_pa"),
            font=self.ui["font_body"],
            text_color=("gray35", "gray70"),
        ).pack(anchor="e")
        pa_entry = ctk.CTkEntry(
            pa_box, width=150, font=self.ui["font_h2"], justify="right"
        )
        pa_entry.insert(0, pa_value)
        pa_entry.pack(anchor="e")

        toolbar = ctk.CTkFrame(dialog, fg_color="transparent")
        toolbar.pack(fill="x", padx=20, pady=(0, 10))

        add_btn = ctk.CTkButton(
            toolbar,
            text=t("article_dialog.button_stock_movement"),
            state="disabled",
            fg_color="transparent",
            border_width=1,
            text_color=("gray", "gray"),
        )
        add_btn.pack(side="left")

        footer = ctk.CTkFrame(dialog, fg_color="transparent")
        footer.pack(fill="x", padx=20, pady=(0, 15))

        def save_article():
            reference = ref_entry.get().strip()
            designation = des_entry.get().strip()
            pa = pa_entry.get().strip()
            pv = pv_entry.get().strip()
            tva = tva_entry.get().strip()
            stock = stock_entry.get().strip()

            if not designation:
                messagebox.showerror(t("common.error"), t("article_dialog.error_designation_required"))
                return

            try:
                pa_cents = _validate_unit_price(pa) if pa else 0
                pv_cents = _validate_unit_price(pv) if pv else 0
                tva_f = float(tva) if tva else 0.0
                stock_i = int(stock) if stock else 0
            except ValueError:
                pa_cents = pv_cents = None
            if pa_cents is None or pv_cents is None:
                messagebox.showerror(t("common.error"), t("article_dialog.error_price_stock_invalid"))
                return

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            try:
                if article_id is None:
                    cursor.execute(
                        "INSERT INTO articles (reference, designation, prix_achat, prix_vente, tva, stock) VALUES (?, ?, ?, ?, ?, ?)",
                        (reference or None, designation, pa_cents, pv_cents, tva_f, stock_i),
                    )
                else:
                    cursor.execute(
                        "UPDATE articles SET reference = ?, designation = ?, prix_achat = ?, prix_vente = ?, tva = ?, stock = ? WHERE id = ?",
                        (
                            reference or None,
                            designation,
                            pa_cents,
                            pv_cents,
                            tva_f,
                            stock_i,
                            article_id,
                        ),
                    )
                conn.commit()
            finally:
                conn.close()

            self.load_articles()
            dialog.destroy()

        ctk.CTkButton(
            footer,
            text=t("common.close"),
            command=dialog.destroy,
            fg_color="gray",
            hover_color="darkgray",
        ).pack(side="right", padx=10)
        ctk.CTkButton(footer, text=t("common.save"), command=save_article).pack(
            side="right"
        )

        des_entry.focus_set()

    def on_article_double_click(self, event):
        selected = self.articles_tree.selection()
        if not selected:
            return
        values = self.articles_tree.item(selected, "values")
        if not values:
            return
        self.show_add_article_dialog(int(values[0]))

    def create_documents_frame(self, key, title):
        frame = ctk.CTkFrame(self.main_frame, corner_radius=0, fg_color="transparent")

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 20))

        self.doc_headers = getattr(self, "doc_headers", {})
        self.doc_headers[key] = ctk.CTkLabel(header, text=title, font=self.ui["font_h1"])
        self.doc_headers[key].pack(side="left")

        actions_left = ctk.CTkFrame(header, fg_color="transparent")
        actions_left.pack(side="right")

        self.doc_convert_buttons = getattr(self, "doc_convert_buttons", {})
        if key == "devis":
            self.doc_convert_buttons[key] = ctk.CTkButton(
                actions_left,
                text=t("documents.button_convert_devis_to_bl"),
                command=lambda k=key: self.convert_selected_document(k, "bl"),
            )
            self.doc_convert_buttons[key].pack(side="right", padx=(10, 0))
        elif key == "bl":
            self.doc_convert_buttons[key] = ctk.CTkButton(
                actions_left,
                text=t("documents.button_convert_bl_to_invoice"),
                command=lambda k=key: self.convert_selected_document(k, "factures"),
            )
            self.doc_convert_buttons[key].pack(side="right", padx=(10, 0))
            self.doc_validate_buttons = getattr(self, "doc_validate_buttons", {})
            self.doc_validate_buttons[key] = ctk.CTkButton(
                actions_left,
                text=t("documents.button_validate_bl"),
                command=lambda k=key: self.validate_selected_document(k),
            )
            self.doc_validate_buttons[key].pack(side="right", padx=(10, 0))
        elif key == "reception":
            self.doc_validate_buttons = getattr(self, "doc_validate_buttons", {})
            self.doc_validate_buttons[key] = ctk.CTkButton(
                actions_left,
                text=t("documents.button_validate_reception"),
                command=lambda k=key: self.validate_selected_document(k),
            )
            self.doc_validate_buttons[key].pack(side="right", padx=(10, 0))

        self.doc_delete_buttons = getattr(self, "doc_delete_buttons", {})
        del_btn = ctk.CTkButton(
            actions_left,
            text=t("common.delete"),
            command=lambda k=key: self.delete_selected_document(k),
            fg_color=self.ui.get("danger_fg", "#FF4B4B"),
            hover_color=self.ui.get("danger_hover", "#FF0000"),
            state="disabled",
        )
        del_btn.pack(side="right", padx=(10, 0))
        self.doc_delete_buttons[key] = del_btn

        self.doc_new_buttons = getattr(self, "doc_new_buttons", {})
        _new_btn_key = {"devis": "documents.button_new_devis", "bl": "documents.button_new_bl", "factures": "documents.button_new_factures", "reception": "documents.button_new_reception"}.get(key, "documents.button_new_devis")
        self.doc_new_buttons[key] = ctk.CTkButton(
            actions_left,
            text=t(_new_btn_key),
            command=lambda k=key, t=title: self.show_new_document_dialog(k, t),
        )
        self.doc_new_buttons[key].pack(side="right")

        self.document_search_entries = getattr(self, "document_search_entries", {})
        search_frame = ctk.CTkFrame(frame, fg_color="transparent")
        search_frame.pack(fill="x", pady=(0, 12))

        # Container for search entry and dropdown
        container = ctk.CTkFrame(search_frame, fg_color="transparent")
        container.pack(side="left", padx=(0, 10))

        self.document_search_entries[key] = ctk.CTkEntry(
            container, width=350, placeholder_text=t("documents.search_placeholder")
        )
        self.document_search_entries[key].pack(fill="x")

        # Autocomplete dropdown
        dropdown = ctk.CTkFrame(container, fg_color="gray20", corner_radius=6)
        dropdown.pack(fill="x", pady=(2, 0), expand=False)
        dropdown.pack_forget()

        # Store dropdown and items for this document type
        if not hasattr(self, "doc_dropdowns"):
            self.doc_dropdowns = {}
        self.doc_dropdowns[key] = {"frame": dropdown, "items": []}

        # Search button
        self.doc_search_buttons = getattr(self, "doc_search_buttons", {})
        self.doc_search_buttons[key] = ctk.CTkButton(
            search_frame,
            text=t("common.search"),
            command=lambda k=key: self.search_documents(k),
        )
        self.doc_search_buttons[key].pack(side="left")

        # Bind autocomplete events
        self.document_search_entries[key].bind(
            "<KeyRelease>", lambda e, k=key: self.on_doc_search_keyrelease(e, k)
        )
        self.document_search_entries[key].bind(
            "<Return>", lambda e, k=key: self.search_documents(k)
        )
        self.document_search_entries[key].bind(
            "<Escape>", lambda e, k=key: self.hide_doc_dropdown(k)
        )
        self.document_search_entries[key].bind(
            "<FocusOut>",
            lambda e, k=key: self.after(150, lambda: self.hide_doc_dropdown(k)),
        )
        dropdown.bind("<Button-1>", lambda e: "break")

        # Store reference for paiements frame
        if key in ("devis", "bl", "factures"):
            if not hasattr(self, "paiements_doc_search"):
                self.paiements_doc_search = None

        columns = (
            "ID",
            t("client_dialog.col_number"),
            t("documents.col_client"),
            t("article_dialog.col_date"),
            t("client_dialog.col_status"),
            t("documents.col_ht"),
            t("documents.col_tva"),
            t("documents.col_ttc"),
        )
        tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse"
        )
        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=140, anchor="center")

        tree.column(t("documents.col_client"), width=240, anchor="w")
        tree.column(t("article_dialog.col_date"), width=140, anchor="center")
        tree.column(t("documents.col_ht"), width=110, anchor="e")
        tree.column(t("documents.col_tva"), width=110, anchor="e")
        tree.column(t("documents.col_ttc"), width=110, anchor="e")

        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        scrollbar.pack(side="right", fill="y")
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(fill="both", expand=True)

        tree.bind(
            "<Double-1>",
            lambda e, k=key, tr=tree: self.on_document_double_click(e, k, tr),
        )

        tree.bind(
            "<<TreeviewSelect>>", lambda e, k=key: self._update_doc_delete_button(k)
        )

        self.documents_trees = getattr(self, "documents_trees", {})
        self.documents_trees[key] = tree
        
        # Add sorting functionality
        self.document_sort_state = getattr(self, "document_sort_state", {})
        self.document_sort_state[key] = {}  # Track sort state per column
        for col in columns:
            tree.heading(col, command=lambda c=col, k=key: self.sort_documents(k, c))
        
        self.load_documents(key)
        self.frames[key] = frame

    def sort_documents(self, doc_type, column):
        """Sort documents by column (ascending/descending toggle)."""
        tree = self.documents_trees.get(doc_type)
        if not tree:
            return
        
        # Get current sort state for this column
        if not hasattr(self, "document_sort_state"):
            self.document_sort_state = {}
        if doc_type not in self.document_sort_state:
            self.document_sort_state[doc_type] = {}
        
        current_state = self.document_sort_state[doc_type].get(column, False)  # False = ascending
        new_state = not current_state  # Toggle
        self.document_sort_state[doc_type][column] = new_state
        
        # Update all headers to show arrows
        arrow = "▲" if new_state else "▼"
        all_cols = ["ID", t("client_dialog.col_number"), t("documents.col_client"), t("article_dialog.col_date"), t("client_dialog.col_status"), t("documents.col_ht"), t("documents.col_tva"), t("documents.col_ttc")]
        for col in all_cols:
            if col == column:
                tree.heading(col, text=f"{col} {arrow}")
            else:
                # Remove arrow from other columns
                clean_col = col.replace(" ▲", "").replace(" ▼", "")
                tree.heading(col, text=clean_col)
        
        # Get all items from tree
        items = [(tree.item(item, "values"), item) for item in tree.get_children()]
        
        # Column index mapping
        col_index = {
            "ID": 0,
            t("client_dialog.col_number"): 1,
            t("documents.col_client"): 2,
            t("article_dialog.col_date"): 3,
            t("client_dialog.col_status"): 4,
            t("documents.col_ht"): 5,
            t("documents.col_tva"): 6,
            t("documents.col_ttc"): 7,
        }
        
        idx = col_index.get(column, 0)
        
        # Sort items
        def sort_key(item):
            values = item[0]
            val = values[idx]
            
            # Handle numeric columns
            if column in [t("documents.col_ht"), t("documents.col_tva"), t("documents.col_ttc")]:
                try:
                    return float(val)
                except:
                    return 0.0
            # Handle ID
            elif column == "ID":
                try:
                    return int(val)
                except:
                    return 0
            # Handle date
            elif column == t("article_dialog.col_date"):
                return str(val)
            # Handle text columns
            else:
                return str(val).lower()
        
        items.sort(key=sort_key, reverse=new_state)
        
        # Reinsert items in sorted order
        for item in tree.get_children():
            tree.delete(item)
        
        for values, item_id in items:
            tree.insert("", "end", values=values)
        
        self._apply_zebra(tree)

    def load_documents(self, doc_type):
        tree = self.documents_trees.get(doc_type)
        if not tree:
            return

        for item in tree.get_children():
            tree.delete(item)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT d.id, d.numero, COALESCE(c.nom, ''), d.date, d.statut,
                       d.total_ht, d.total_tva, d.total_ttc
                FROM documents d
                LEFT JOIN clients c ON c.id = d.client_id
                WHERE d.type = ?
                ORDER BY d.date DESC, d.id DESC
                """,
                (doc_type,),
            )
            for row in cursor.fetchall():
                tree.insert(
                    "",
                    "end",
                    values=(
                        row[0],
                        row[1],
                        row[2],
                        row[3],
                        row[4],
                        format_money(row[5]),
                        format_money(row[6]),
                        format_money(row[7]),
                    ),
                )
        finally:
            conn.close()

        self._apply_zebra(tree)

    def on_doc_search_keyrelease(self, event, doc_type):
        """Handle key release for document search autocomplete."""
        if not hasattr(
            self, "document_search_entries"
        ) or not self.document_search_entries.get(doc_type):
            return

        query = self.document_search_entries[doc_type].get().strip()

        if not query:
            self.hide_doc_dropdown(doc_type)
            return

        # Get matching documents from database (search by ID, numero, or client name)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT DISTINCT
                    d.id || ' - ' || d.numero || ' - ' || COALESCE(c.nom,'') as display_value,
                    d.id,
                    d.numero,
                    COALESCE(c.nom,'') as client_name
                FROM documents d
                LEFT JOIN clients c ON c.id = d.client_id
                WHERE d.type = ? AND (
                    CAST(d.id AS TEXT) LIKE ? OR
                    d.numero LIKE ? OR
                    COALESCE(c.nom,'') LIKE ?
                )
                ORDER BY d.date DESC, d.id DESC
                LIMIT 5
                """,
                (doc_type, f"%{query}%", f"%{query}%", f"%{query}%"),
            )
            results = cursor.fetchall()
        finally:
            conn.close()

        if results:
            # Show dropdown with suggestions (use display_value for display)
            suggestions = []
            for row in results:
                display_value = row[0]
                suggestions.append(display_value)
            self.show_doc_dropdown(doc_type, suggestions)
        else:
            self.hide_doc_dropdown(doc_type)

    def show_doc_dropdown(self, doc_type, items):
        """Show autocomplete dropdown with suggestions."""
        if not hasattr(self, "doc_dropdowns") or not self.doc_dropdowns.get(doc_type):
            return

        dropdown_info = self.doc_dropdowns[doc_type]
        dropdown = dropdown_info["frame"]

        # Clear existing items
        for widget in dropdown_info["items"]:
            widget.destroy()
        dropdown_info["items"] = []

        # Add new items
        for item in items[:5]:  # Max 5 suggestions
            btn = ctk.CTkButton(
                dropdown,
                text=item,
                command=lambda x=item: self.select_doc_suggestion(doc_type, x),
                fg_color="transparent",
                text_color="white",
                hover_color="gray40",
                anchor="w",
                height=28,
                corner_radius=4,
            )
            btn.pack(fill="x", padx=2, pady=1)
            dropdown_info["items"].append(btn)

        # Show dropdown
        dropdown.pack(fill="x", pady=(2, 0), expand=False)
        self.document_search_entries[doc_type].focus_set()

    def hide_doc_dropdown(self, doc_type):
        """Hide autocomplete dropdown."""
        if not hasattr(self, "doc_dropdowns") or not self.doc_dropdowns.get(doc_type):
            return

        dropdown_info = self.doc_dropdowns[doc_type]
        dropdown = dropdown_info["frame"]
        dropdown.pack_forget()

        for widget in dropdown_info["items"]:
            widget.destroy()
        dropdown_info["items"] = []

    def select_doc_suggestion(self, doc_type, suggestion):
        """Select an autocomplete suggestion."""
        if not hasattr(
            self, "document_search_entries"
        ) or not self.document_search_entries.get(doc_type):
            return

        self.document_search_entries[doc_type].delete(0, "end")
        self.document_search_entries[doc_type].insert(0, suggestion)
        self.hide_doc_dropdown(doc_type)
        self.search_documents(doc_type)

    def search_documents(self, doc_type, event=None):
        """Search documents by ID, number, or client name for the given type."""
        tree = self.documents_trees.get(doc_type)
        if not tree:
            return

        query_val = ""
        if hasattr(
            self, "document_search_entries"
        ) and self.document_search_entries.get(doc_type):
            try:
                query_val = self.document_search_entries[doc_type].get().strip()
            except Exception:
                query_val = ""

        if not query_val:
            return self.load_documents(doc_type)

        # Handle the new format "ID - Numéro - Client"
        if " - " in query_val:
            parts = query_val.split(" - ")
            # Extract ID from the first part if it's a number
            id_part = parts[0].strip() if parts else ""
            num_part = parts[1].strip() if len(parts) > 1 else ""
            client_part = parts[2].strip() if len(parts) > 2 else ""

            # Build conditions based on available parts
            conditions = ["d.type = ?"]
            params = [doc_type]

            if id_part and id_part.isdigit():
                conditions.append("CAST(d.id AS TEXT) LIKE ?")
                params.append(f"%{id_part}%")
            if num_part:
                conditions.append("d.numero LIKE ?")
                params.append(f"%{num_part}%")
            if client_part:
                conditions.append("COALESCE(c.nom,'') LIKE ?")
                params.append(f"%{client_part}%")

            # If no specific parts found, search with full query
            if len(conditions) == 1:
                conditions.append(
                    "(CAST(d.id AS TEXT) LIKE ? OR d.numero LIKE ? OR COALESCE(c.nom,'') LIKE ?)"
                )
                params.extend([f"%{query_val}%", f"%{query_val}%", f"%{query_val}%"])

            where_clause = " AND ".join(conditions)
            sql = f"""
                SELECT DISTINCT d.id, d.numero, COALESCE(c.nom, ''), d.date, d.statut,
                       d.total_ht, d.total_tva, d.total_ttc
                FROM documents d
                LEFT JOIN clients c ON c.id = d.client_id
                WHERE {where_clause}
                ORDER BY d.date DESC, d.id DESC
                """
            like_q = f"%{query_val}%"
        else:
            conditions = ["d.type = ?"]
            params = [doc_type]
            conditions.append(
                "(CAST(d.id AS TEXT) LIKE ? OR d.numero LIKE ? OR COALESCE(c.nom,'') LIKE ?)"
            )
            params.extend([f"%{query_val}%", f"%{query_val}%", f"%{query_val}%"])

            where_clause = " AND ".join(conditions)
            sql = f"""
                SELECT DISTINCT d.id, d.numero, COALESCE(c.nom, ''), d.date, d.statut,
                       d.total_ht, d.total_tva, d.total_ttc
                FROM documents d
                LEFT JOIN clients c ON c.id = d.client_id
                WHERE {where_clause}
                ORDER BY d.date DESC, d.id DESC
                """
            like_q = f"%{query_val}%"

        for item in tree.get_children():
            tree.delete(item)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(sql, tuple(params))
            for row in cursor.fetchall():
                tree.insert(
                    "",
                    "end",
                    values=(
                        row[0],
                        row[1],
                        row[2],
                        row[3],
                        row[4],
                        format_money(row[5]),
                        format_money(row[6]),
                        format_money(row[7]),
                    ),
                )
        finally:
            conn.close()

        self._apply_zebra(tree)
        self.hide_doc_dropdown(doc_type)

    def show_new_document_dialog(self, doc_type, title):
        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Nouveau {title}")
        dialog.geometry("800x600")
        try:
            dialog.transient(self)
            dialog.grab_set()
        except Exception as e:
            print(f"Warning: could not set modal grab for new document dialog: {e}")

        top = ctk.CTkFrame(dialog, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=15)

        numero = self.peek_next_numero(doc_type)
        ctk.CTkLabel(top, text=t("new_doc.label_number", number=numero), font=("Arial", 14, "bold")).pack(
            side="left"
        )

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT id, nom FROM clients ORDER BY nom")
        clients = cursor.fetchall()
        conn.close()

        client_names = [c[1] for c in clients]
        client_map = {c[1]: c[0] for c in clients}

        client_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        client_frame.pack(fill="x", padx=20)
        ctk.CTkLabel(client_frame, text=t("documents.col_client")).pack(side="left", padx=(0, 10))
        client_combo = ttk.Combobox(
            client_frame, values=client_names, state="readonly", width=40
        )
        client_combo.pack(side="left")

        lines_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        lines_frame.pack(fill="both", expand=True, padx=20, pady=15)

        columns = ("ArticleId", t("articles.col_reference"), t("new_doc.col_article"), t("new_doc.col_qty"), t("new_doc.col_pu"), t("articles.col_tva"))
        lines_tree = ttk.Treeview(
            lines_frame, columns=columns, show="headings", selectmode="browse"
        )
        for col in columns:
            lines_tree.heading(col, text=col)
            if col == "ArticleId":
                lines_tree.column(col, width=0, stretch=False)
                lines_tree.heading(col, text="")
            elif col == t("articles.col_reference"):
                lines_tree.column(col, width=100, anchor="center")
            elif col == t("new_doc.col_article"):
                lines_tree.column(col, width=200, anchor="w")
            elif col == t("new_doc.col_qty"):
                lines_tree.column(col, width=80, anchor="center")
            elif col == t("new_doc.col_pu"):
                lines_tree.column(col, width=100, anchor="center")
            elif col == t("articles.col_tva"):
                lines_tree.column(col, width=80, anchor="center")
        lines_tree.pack(fill="both", expand=True)

        # Variables for discount (will be defined later in the dialog)
        discount_enabled_var = None
        discount_entry = None
        discount_display_frame = None
        lbl_discount_val = None
        lbl_ht_val = None
        lbl_tva_val = None
        lbl_ttc_val = None

        def recalc_total():
            if discount_enabled_var is None or discount_entry is None:
                return
            ht = 0
            tva_sum = 0
            for item in lines_tree.get_children():
                _article_id, _ref, art, qty, pu, tva = lines_tree.item(item, "values")
                values, error_key = _validate_line_inputs(qty, pu, tva)
                if error_key:
                    continue
                qty_f, pu_cents, tva_f = values
                line_ht, line_tva, _line_ttc = line_totals(
                    qty_f, pu_cents, tva_f
                )
                ht += line_ht
                tva_sum += line_tva

            discount_percent = 0.0
            discount_amount = 0
            if discount_enabled_var.get():
                discount_percent = (
                    _validate_percentage(discount_entry.get().strip() or 0) or 0.0
                )
                discount_amount = discount_cents(ht, discount_percent)

            ttc = max(0, ht + tva_sum - discount_amount)

            if lbl_ht_val:
                lbl_ht_val.configure(text=format_money(ht))
            if lbl_tva_val:
                lbl_tva_val.configure(text=format_money(tva_sum))
            if lbl_discount_val:
                lbl_discount_val.configure(text=f"{discount_percent:.1f}%")
            if lbl_discount_amount:
                lbl_discount_amount.configure(text=f"-{format_money(discount_amount)}")
            if lbl_ttc_val:
                lbl_ttc_val.configure(text=format_money(ttc))

        def add_line():
            dlg = ctk.CTkToplevel(dialog)
            dlg.title(t("new_doc.line_dialog_title_add"))
            dlg.geometry("560x420")
            try:
                dlg.transient(dialog)
                dlg.grab_set()
            except Exception as e:
                print(f"Warning: could not set modal grab for add line dialog: {e}")

            header = ctk.CTkFrame(dlg, fg_color="transparent")
            header.pack(fill="x", padx=20, pady=(15, 10))
            ctk.CTkLabel(
                header, text=t("new_doc.line_dialog_title_add"), font=self.ui["font_h2"]
            ).pack(side="left")

            form = ctk.CTkFrame(dlg, fg_color="transparent")
            form.pack(fill="both", expand=True, padx=20, pady=(0, 10))
            form.grid_columnconfigure(0, weight=1)
            form.grid_columnconfigure(1, weight=1)

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, designation, prix_vente, tva FROM articles ORDER BY designation"
            )
            articles = cursor.fetchall()
            conn.close()

            names = [a[1] for a in articles]
            id_map = {a[1]: int(a[0]) for a in articles}
            price_map = {a[1]: as_cents(a[2]) for a in articles}
            tva_map = {a[1]: float(a[3] or 0) for a in articles}

            ctk.CTkLabel(form, text=t("new_doc.col_article"), font=self.ui["font_body"]).grid(
                row=0, column=0, columnspan=2, sticky="w"
            )

            # Container for article search entry and dropdown
            article_search_container = ctk.CTkFrame(form, fg_color="transparent")
            article_search_container.grid(
                row=1, column=0, columnspan=2, sticky="we", pady=(0, 10)
            )

            # Article search entry (like client search)
            article_search_entry = ctk.CTkEntry(
                article_search_container,
                width=540,
                placeholder_text=t("articles.search_placeholder"),
            )
            article_search_entry.pack(fill="x")

            # Autocomplete dropdown for articles
            article_dropdown = ctk.CTkFrame(
                article_search_container, fg_color="gray20", corner_radius=6
            )
            article_dropdown.pack(fill="x", pady=(2, 0), expand=False)
            article_dropdown.pack_forget()  # Hide initially

            article_dropdown_items = []

            def on_article_keyrelease(event):
                query = article_search_entry.get().strip()

                if not query:
                    for widget in article_dropdown_items:
                        widget.destroy()
                    article_dropdown_items.clear()
                    article_dropdown.pack_forget()
                    return

                # Get matching articles from database
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                try:
                    cursor.execute(
                        """
                        SELECT DISTINCT
                            CASE
                                WHEN CAST(a.id AS TEXT) LIKE ? THEN a.id || ' - ' || COALESCE(a.reference,'') || ' - ' || COALESCE(a.designation,'')
                                WHEN a.reference LIKE ? THEN a.id || ' - ' || COALESCE(a.reference,'') || ' - ' || COALESCE(a.designation,'')
                                ELSE a.id || ' - ' || COALESCE(a.reference,'') || ' - ' || COALESCE(a.designation,'')
                            END as display_value,
                            a.id,
                            a.reference,
                            a.designation,
                            a.prix_vente,
                            a.tva
                        FROM articles a
                        WHERE CAST(a.id AS TEXT) LIKE ? OR
                              a.reference LIKE ? OR
                              a.designation LIKE ?
                        ORDER BY
                            CASE
                                WHEN CAST(a.id AS TEXT) LIKE ? THEN 1
                                WHEN a.reference LIKE ? THEN 2
                                ELSE 3
                            END,
                            a.designation
                        LIMIT 5
                        """,
                        (
                            f"%{query}%",
                            f"%{query}%",
                            f"%{query}%",
                            f"%{query}%",
                            f"%{query}%",
                            f"%{query}%",
                            f"%{query}%",
                        ),
                    )
                    results = cursor.fetchall()
                finally:
                    conn.close()

                # Clear existing dropdown items
                for widget in article_dropdown_items:
                    widget.destroy()
                article_dropdown_items.clear()

                if results:
                    # Add new items
                    for row in results:
                        display_value = row[0]
                        article_id = row[1]
                        prix_vente = row[4]
                        tva = row[5]

                        btn = ctk.CTkButton(
                            article_dropdown,
                            text=display_value,
                            command=lambda d=display_value,
                            id=article_id,
                            pv=prix_vente,
                            tv=tva: select_article_suggestion(d, id, pv, tv),
                            fg_color="transparent",
                            text_color="white",
                            hover_color="gray40",
                            anchor="w",
                            height=28,
                            corner_radius=4,
                        )
                        btn.pack(fill="x", padx=2, pady=1)
                        article_dropdown_items.append(btn)

                    # Show dropdown
                    article_dropdown.pack(fill="x", pady=(2, 0), expand=False)
                else:
                    article_dropdown.pack_forget()

            def select_article_suggestion(display_value, article_id, prix_vente, tva):
                article_search_entry.delete(0, "end")
                article_search_entry.insert(0, display_value)

                # Clear dropdown
                for widget in article_dropdown_items:
                    widget.destroy()
                article_dropdown_items.clear()
                article_dropdown.pack_forget()

                # Auto-fill price and tva
                if prix_vente is not None:
                    pu_var.set(money_to_input(prix_vente))
                if tva is not None:
                    tva_var.set(str(tva))

                # Update preview
                update_preview()

            article_search_entry.bind("<KeyRelease>", on_article_keyrelease)
            article_search_entry.bind(
                "<Escape>",
                lambda e: [
                    article_dropdown.pack_forget(),
                    [w.destroy() for w in article_dropdown_items],
                    article_dropdown_items.clear(),
                ],
            )
            article_search_entry.bind(
                "<FocusOut>",
                lambda e: self.after(
                    150,
                    lambda: [
                        article_dropdown.pack_forget(),
                        [w.destroy() for w in article_dropdown_items],
                        article_dropdown_items.clear(),
                    ],
                ),
            )
            article_dropdown.bind("<Button-1>", lambda e: "break")

            ctk.CTkLabel(form, text=t("new_doc.col_qty"), font=self.ui["font_body"]).grid(
                row=2, column=0, sticky="w"
            )
            qty_var = ctk.StringVar(value="1")
            qty_entry = ctk.CTkEntry(form, textvariable=qty_var)
            qty_entry.grid(row=3, column=0, sticky="we", pady=(0, 10), padx=(0, 10))

            ctk.CTkLabel(form, text=t("new_doc.label_pu"), font=self.ui["font_body"]).grid(
                row=2, column=1, sticky="w"
            )
            pu_var = ctk.StringVar(value="0")
            pu_entry = ctk.CTkEntry(form, textvariable=pu_var)
            pu_entry.grid(row=3, column=1, sticky="we", pady=(0, 10))

            ctk.CTkLabel(form, text=t("article_dialog.label_tva"), font=self.ui["font_body"]).grid(
                row=4, column=0, sticky="w"
            )
            tva_var = ctk.StringVar(value="0")
            tva_entry = ctk.CTkEntry(form, textvariable=tva_var)
            tva_entry.grid(row=5, column=0, sticky="we", pady=(0, 10), padx=(0, 10))

            preview = ctk.CTkFrame(form, fg_color="transparent")
            preview.grid(row=5, column=1, sticky="we", pady=(0, 10))
            ctk.CTkLabel(
                preview,
                text=t("new_doc.preview_heading"),
                font=self.ui["font_body"],
                text_color=("gray35", "gray70"),
            ).pack(anchor="w")
            prev_ht_label = ctk.CTkLabel(
                preview, text="HT: 0.00", font=self.ui["font_body"]
            )
            prev_ht_label.pack(anchor="w")
            prev_tva_label = ctk.CTkLabel(
                preview, text="TVA: 0.00", font=self.ui["font_body"]
            )
            prev_tva_label.pack(anchor="w")
            prev_ttc_label = ctk.CTkLabel(
                preview, text="TTC: 0.00", font=self.ui["font_body"]
            )
            prev_ttc_label.pack(anchor="w")

            def update_preview(event=None):
                try:
                    qty_f = _parse_finite_number(qty_entry.get().strip() or 0)
                    pu_cents = parse_money(pu_entry.get().strip() or 0)
                    tva_f = _parse_finite_number(tva_entry.get().strip() or 0)
                    if qty_f is None or pu_cents is None or tva_f is None:
                        raise ValueError("non-finite number")
                except Exception:
                    prev_ht_label.configure(text="HT: -")
                    prev_tva_label.configure(text="TVA: -")
                    prev_ttc_label.configure(text="TTC: -")
                    return
                line_ht, line_tva, line_ttc = line_totals(qty_f, pu_cents, tva_f)
                prev_ht_label.configure(text=f"HT: {format_money(line_ht)}")
                prev_tva_label.configure(text=f"TVA: {format_money(line_tva)}")
                prev_ttc_label.configure(text=f"TTC: {format_money(line_ttc)}")

            qty_var.trace_add("write", lambda *_: update_preview())
            pu_var.trace_add("write", lambda *_: update_preview())
            tva_var.trace_add("write", lambda *_: update_preview())
            qty_entry.bind("<FocusOut>", update_preview)
            pu_entry.bind("<FocusOut>", update_preview)
            tva_entry.bind("<FocusOut>", update_preview)
            update_preview()

            def save_line():
                display_value = article_search_entry.get().strip()
                if not display_value:
                    messagebox.showerror(t("common.error"), t("new_doc.error_choose_article"))
                    return
                values, error_key = _validate_line_inputs(
                    qty_entry.get(), pu_entry.get(), tva_entry.get()
                )
                if error_key:
                    messagebox.showerror(t("common.error"), t(error_key))
                    return
                qty, pu, tva = values

                # Extract article ID and reference from display value (format: "ID - REF - Designation")
                article_id = None
                reference = ""
                designation = display_value
                if " - " in display_value:
                    parts = display_value.split(" - ", 2)
                    if len(parts) >= 2:
                        id_part = parts[0]
                        ref_part = parts[1]
                        if id_part.isdigit():
                            article_id = int(id_part)
                        reference = ref_part
                        if len(parts) == 3:
                            designation = parts[2]
                        else:
                            designation = display_value

                if article_id is None:
                    messagebox.showerror(t("common.error"), t("new_doc.error_choose_article"))
                    return

                lines_tree.insert(
                    "",
                    "end",
                    values=(
                        article_id,
                        reference,
                        designation,
                        qty,
                        format_money(pu),
                        tva,
                    ),
                )
                recalc_total()
                dlg.destroy()

            btns = ctk.CTkFrame(dlg, fg_color="transparent")
            btns.pack(fill="x", padx=20, pady=(0, 15))
            ctk.CTkButton(
                btns,
                text=t("common.cancel"),
                command=dlg.destroy,
                fg_color="gray",
                hover_color="darkgray",
            ).pack(side="right", padx=10)
            ctk.CTkButton(btns, text=t("common.add"), command=save_line).pack(side="right")

        def edit_line():
            sel = lines_tree.selection()
            if not sel:
                messagebox.showerror(t("common.error"), t("new_doc.error_select_line_edit"))
                return
            item = sel[0]
            vals = lines_tree.item(item, "values")
            if not vals:
                return

            dlg = ctk.CTkToplevel(dialog)
            dlg.title(t("new_doc.button_edit_line"))
            dlg.geometry("560x520")
            try:
                dlg.transient(dialog)
                dlg.grab_set()
            except Exception as e:
                print(f"Warning: could not set modal grab for edit line dialog: {e}")

            header = ctk.CTkFrame(dlg, fg_color="transparent")
            header.pack(fill="x", padx=20, pady=(15, 10))
            ctk.CTkLabel(
                header, text=t("new_doc.button_edit_line"), font=self.ui["font_h2"]
            ).pack(side="left")

            form = ctk.CTkFrame(dlg, fg_color="transparent")
            form.pack(fill="both", expand=True, padx=20, pady=(0, 10))
            form.grid_columnconfigure(0, weight=1)
            form.grid_columnconfigure(1, weight=1)

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, designation, prix_vente, tva FROM articles ORDER BY designation"
            )
            articles = cursor.fetchall()
            conn.close()

            names = [a[1] for a in articles]
            id_map = {a[1]: int(a[0]) for a in articles}
            price_map = {a[1]: as_cents(a[2]) for a in articles}
            tva_map = {a[1]: float(a[3] or 0) for a in articles}

            ctk.CTkLabel(form, text=t("new_doc.col_article"), font=self.ui["font_body"]).grid(
                row=0, column=0, columnspan=2, sticky="w"
            )

            # Container for article search entry and dropdown
            article_search_container = ctk.CTkFrame(form, fg_color="transparent")
            article_search_container.grid(
                row=1, column=0, columnspan=2, sticky="we", pady=(0, 10)
            )

            # Article search entry (like t("new_doc.line_dialog_title_add"))
            article_search_entry = ctk.CTkEntry(
                article_search_container,
                width=540,
                placeholder_text=t("articles.search_placeholder"),
            )
            article_search_entry.pack(fill="x")

            # Autocomplete dropdown for articles
            article_dropdown = ctk.CTkFrame(
                article_search_container, fg_color="gray20", corner_radius=6
            )
            article_dropdown.pack(fill="x", pady=(2, 0), expand=False)
            article_dropdown.pack_forget()  # Hide initially

            article_dropdown_items = []

            def on_article_keyrelease(event):
                query = article_search_entry.get().strip()

                if not query:
                    for widget in article_dropdown_items:
                        widget.destroy()
                    article_dropdown_items.clear()
                    article_dropdown.pack_forget()
                    return

                # Get matching articles from database
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                try:
                    cursor.execute(
                        """
                        SELECT DISTINCT
                            CASE
                                WHEN CAST(a.id AS TEXT) LIKE ? THEN a.id || ' - ' || COALESCE(a.reference,'') || ' - ' || COALESCE(a.designation,'')
                                WHEN a.reference LIKE ? THEN a.id || ' - ' || COALESCE(a.reference,'') || ' - ' || COALESCE(a.designation,'')
                                ELSE a.id || ' - ' || COALESCE(a.reference,'') || ' - ' || COALESCE(a.designation,'')
                            END as display_value,
                            a.id,
                            a.reference,
                            a.designation,
                            a.prix_vente,
                            a.tva
                        FROM articles a
                        WHERE CAST(a.id AS TEXT) LIKE ? OR
                              a.reference LIKE ? OR
                              a.designation LIKE ?
                        ORDER BY
                            CASE
                                WHEN CAST(a.id AS TEXT) LIKE ? THEN 1
                                WHEN a.reference LIKE ? THEN 2
                                ELSE 3
                            END,
                            a.designation
                        LIMIT 5
                        """,
                        (
                            f"%{query}%",
                            f"%{query}%",
                            f"%{query}%",
                            f"%{query}%",
                            f"%{query}%",
                            f"%{query}%",
                            f"%{query}%",
                        ),
                    )
                    results = cursor.fetchall()
                finally:
                    conn.close()

                # Clear existing dropdown items
                for widget in article_dropdown_items:
                    widget.destroy()
                article_dropdown_items.clear()

                if results:
                    # Add new items
                    for row in results:
                        display_value = row[0]
                        article_id = row[1]
                        prix_vente = row[4]
                        tva = row[5]

                        btn = ctk.CTkButton(
                            article_dropdown,
                            text=display_value,
                            command=lambda d=display_value,
                            id=article_id,
                            pv=prix_vente,
                            tv=tva: select_article_suggestion(d, id, pv, tv),
                            fg_color="transparent",
                            text_color="white",
                            hover_color="gray40",
                            anchor="w",
                            height=28,
                            corner_radius=4,
                        )
                        btn.pack(fill="x", padx=2, pady=1)
                        article_dropdown_items.append(btn)

                    # Show dropdown
                    article_dropdown.pack(fill="x", pady=(2, 0), expand=False)
                else:
                    article_dropdown.pack_forget()

            def select_article_suggestion(display_value, article_id, prix_vente, tva):
                article_search_entry.delete(0, "end")
                article_search_entry.insert(0, display_value)

                # Clear dropdown
                for widget in article_dropdown_items:
                    widget.destroy()
                article_dropdown_items.clear()
                article_dropdown.pack_forget()

                # Auto-fill price and tva
                if prix_vente is not None:
                    pu_entry.delete(0, "end")
                    pu_entry.insert(0, money_to_input(prix_vente))
                if tva is not None:
                    tva_entry.delete(0, "end")
                    tva_entry.insert(0, str(tva))

            article_search_entry.bind("<KeyRelease>", on_article_keyrelease)
            article_search_entry.bind(
                "<Escape>",
                lambda e: [
                    article_dropdown.pack_forget(),
                    [w.destroy() for w in article_dropdown_items],
                    article_dropdown_items.clear(),
                ],
            )
            article_search_entry.bind(
                "<FocusOut>",
                lambda e: self.after(
                    150,
                    lambda: [
                        article_dropdown.pack_forget(),
                        [w.destroy() for w in article_dropdown_items],
                        article_dropdown_items.clear(),
                    ],
                ),
            )
            article_dropdown.bind("<Button-1>", lambda e: "break")

            ctk.CTkLabel(form, text=t("new_doc.col_qty"), font=self.ui["font_body"]).grid(
                row=2, column=0, sticky="w"
            )
            qty_entry = ctk.CTkEntry(form, width=20)
            qty_entry.grid(row=3, column=0, sticky="we", padx=(0, 6))

            ctk.CTkLabel(form, text=t("new_doc.col_pu"), font=self.ui["font_body"]).grid(
                row=2, column=1, sticky="w"
            )
            pu_entry = ctk.CTkEntry(form, width=20)
            pu_entry.grid(row=3, column=1, sticky="we")

            ctk.CTkLabel(form, text=t("articles.col_tva"), font=self.ui["font_body"]).grid(
                row=4, column=0, sticky="w"
            )
            tva_entry = ctk.CTkEntry(form, width=20)
            tva_entry.grid(row=5, column=0, sticky="we")

            try:
                article_id_val = vals[0]
                ref_val = vals[1]
                art_val = vals[2]
                qty_val = vals[3]
                pu_val = vals[4]
                tva_val = vals[5]
            except Exception:
                article_id_val = ""
                ref_val = ""
                art_val = ""
                qty_val = "1"
                pu_val = "0"
                tva_val = "0"

            # Populate initial values
            if art_val:
                # Format as "ID - REF - Designation" for the search entry
                display_value = f"{article_id_val} - {ref_val} - {art_val}"
                article_search_entry.insert(0, display_value)
            qty_entry.insert(0, str(qty_val))
            pu_entry.insert(0, str(pu_val))
            tva_entry.insert(0, str(tva_val))

            def save_edit():
                display_value = article_search_entry.get().strip()
                if not display_value:
                    messagebox.showerror(t("common.error"), t("new_doc.error_choose_article"))
                    return
                
                # Extract article ID and reference from display value (format: "ID - REF - Designation")
                article_id = None
                reference = ""
                designation = display_value
                if " - " in display_value:
                    parts = display_value.split(" - ", 2)
                    if len(parts) >= 2:
                        id_part = parts[0]
                        ref_part = parts[1]
                        if id_part.isdigit():
                            article_id = int(id_part)
                        reference = ref_part
                        if len(parts) == 3:
                            designation = parts[2]
                        else:
                            designation = display_value
                else:
                    # If no format, try to find by name
                    designation = display_value
                    conn = sqlite3.connect(self.db_path)
                    cursor = conn.cursor()
                    cursor.execute("SELECT id, reference FROM articles WHERE designation = ?", (designation,))
                    result = cursor.fetchone()
                    if result:
                        article_id = result[0]
                        reference = result[1]
                    conn.close()

                if article_id is None:
                    messagebox.showerror(t("common.error"), t("new_doc.error_choose_article"))
                    return

                values, error_key = _validate_line_inputs(
                    qty_entry.get(), pu_entry.get(), tva_entry.get()
                )
                if error_key:
                    messagebox.showerror(t("common.error"), t(error_key))
                    return
                qty, pu, tva = values

                lines_tree.item(
                    item,
                    values=(
                        article_id,
                        reference,
                        designation,
                        qty,
                        format_money(pu),
                        tva,
                    ),
                )
                recalc_total()
                dlg.destroy()

            btns = ctk.CTkFrame(dlg, fg_color="transparent")
            btns.pack(fill="x", padx=20, pady=(0, 15))
            ctk.CTkButton(
                btns,
                text=t("common.cancel"),
                command=dlg.destroy,
                fg_color="gray",
                hover_color="darkgray",
            ).pack(side="right", padx=10)
            ctk.CTkButton(btns, text=t("common.save"), command=save_edit).pack(
                side="right"
            )

        def delete_line():
            sel = lines_tree.selection()
            if not sel:
                messagebox.showerror(t("common.error"), t("new_doc.error_select_line_delete"))
                return
            if not messagebox.askyesno(
                t("common.confirm"), t("new_doc.confirm_delete_line")
            ):
                return
            for s in sel:
                lines_tree.delete(s)
            recalc_total()

        actions = ctk.CTkFrame(dialog, fg_color="transparent")
        actions.pack(fill="x", padx=20, pady=(0, 10))
        add_line_btn = ctk.CTkButton(
            actions, text=t("new_doc.button_add_line"), command=add_line
        )
        add_line_btn.pack(side="left")
        edit_line_btn = ctk.CTkButton(
            actions, text=t("new_doc.button_edit_line"), command=edit_line, state="disabled"
        )
        edit_line_btn.pack(side="left", padx=(8, 0))
        del_line_btn = ctk.CTkButton(
            actions,
            text=t("new_doc.button_delete_line"),
            command=delete_line,
            fg_color=self.ui.get("danger_fg", "#FF4B4B"),
            hover_color=self.ui.get("danger_hover", "#FF0000"),
            state="disabled",
        )
        del_line_btn.pack(side="left", padx=(8, 0))

        def _update_line_buttons(evt=None):
            sel = lines_tree.selection()
            state = "normal" if sel else "disabled"
            try:
                edit_line_btn.configure(state=state)
                del_line_btn.configure(state=state)
            except Exception:
                pass

        lines_tree.bind("<<TreeviewSelect>>", _update_line_buttons)
        lines_tree.bind("<Double-1>", lambda e: edit_line())

        totals = ctk.CTkFrame(dialog, fg_color="transparent")
        totals.pack(fill="x", padx=20, pady=(0, 5))

        # Discount controls (left side)
        discount_frame = ctk.CTkFrame(totals, fg_color="transparent")
        discount_frame.pack(side="left", padx=(0, 20))
        
        discount_enabled_var = ctk.BooleanVar(value=False)
        discount_checkbox = ctk.CTkCheckBox(
            discount_frame, text=t("new_doc.discount_checkbox"), variable=discount_enabled_var
        )
        discount_checkbox.pack(anchor="w")
        
        discount_entry = ctk.CTkEntry(discount_frame, width=80, placeholder_text="%")
        discount_entry.pack(anchor="w", pady=(4, 0))
        discount_entry.configure(state="disabled")

        # Discount display table (middle - right of controls)
        discount_left_frame = ctk.CTkFrame(totals, fg_color="transparent")
        
        # Discount percentage row
        discount_pct_frame = ctk.CTkFrame(discount_left_frame, fg_color="transparent")
        discount_pct_frame.pack(fill="x")
        ctk.CTkLabel(
            discount_pct_frame,
            text=t("new_doc.discount_label"),
            font=("Arial", 10),
            text_color=("gray35", "gray70"),
        ).pack(side="left")
        lbl_discount_val = ctk.CTkLabel(
            discount_pct_frame,
            text="0.00%",
            font=("Arial", 14, "bold")
        )
        lbl_discount_val.pack(side="right")
        
        # Discount amount row
        discount_amount_frame = ctk.CTkFrame(discount_left_frame, fg_color="transparent")
        discount_amount_frame.pack(fill="x")
        ctk.CTkLabel(
            discount_amount_frame,
            text=t("new_doc.discount_amount_label"),
            font=("Arial", 10),
            text_color=("gray35", "gray70"),
        ).pack(side="left")
        lbl_discount_amount = ctk.CTkLabel(
            discount_amount_frame,
            text="-0.00",
            font=("Arial", 14, "bold")
        )
        lbl_discount_amount.pack(side="right")

        def toggle_discount():
            if discount_enabled_var.get():
                discount_entry.configure(state="normal")
                discount_left_frame.pack(side="left", padx=(0, 20))
            else:
                discount_entry.configure(state="disabled")
                discount_entry.delete(0, "end")
                discount_left_frame.pack_forget()
            recalc_total()

        def on_discount_change(*args):
            recalc_total()

        discount_enabled_var.trace_add("write", lambda *_: toggle_discount())
        discount_entry.bind("<KeyRelease>", lambda e: on_discount_change())
        discount_entry.bind("<FocusOut>", lambda e: on_discount_change())

        # Totals on the far right
        totals_right = ctk.CTkFrame(totals, fg_color="transparent")
        totals_right.pack(side="right")

        ttc_box = ctk.CTkFrame(totals_right, fg_color="transparent")
        ttc_box.pack(side="right", padx=(0, 20))
        ctk.CTkLabel(
            ttc_box,
            text=t("documents.col_ttc"),
            font=("Arial", 10),
            text_color=("gray35", "gray70"),
        ).pack(anchor="e")
        lbl_ttc_val = ctk.CTkLabel(
            ttc_box, text="0.00", font=("Arial", 14, "bold")
        )
        lbl_ttc_val.pack(anchor="e")

        tva_box = ctk.CTkFrame(totals_right, fg_color="transparent")
        tva_box.pack(side="right", padx=(0, 20))
        ctk.CTkLabel(
            tva_box,
            text=t("documents.col_tva"),
            font=("Arial", 10),
            text_color=("gray35", "gray70"),
        ).pack(anchor="e")
        lbl_tva_val = ctk.CTkLabel(
            tva_box, text="0.00", font=("Arial", 14, "bold")
        )
        lbl_tva_val.pack(anchor="e")

        ht_box = ctk.CTkFrame(totals_right, fg_color="transparent")
        ht_box.pack(side="right", padx=(0, 20))
        ctk.CTkLabel(
            ht_box,
            text=t("documents.col_ht"),
            font=("Arial", 10),
            text_color=("gray35", "gray70"),
        ).pack(anchor="e")
        lbl_ht_val = ctk.CTkLabel(
            ht_box, text="0.00", font=("Arial", 14, "bold")
        )
        lbl_ht_val.pack(anchor="e")

        recalc_total()

        def save_document():
            client_name = client_combo.get().strip()
            client_id = client_map.get(client_name) if client_name else None
            if client_id is None:
                messagebox.showerror(
                    t("common.error"), t("new_doc.error_client_required")
                )
                return

            items = lines_tree.get_children()
            if not items:
                messagebox.showerror(t("common.error"), t("new_doc.error_no_lines"))
                return

            # Validate every line before touching the database so a bad row
            # cannot leave a half-saved document behind.
            validated_lines = []
            ht = 0
            tva_sum = 0
            for item in items:
                article_id, _ref, art, qty, pu, tva = lines_tree.item(
                    item, "values"
                )
                values, error_key = _validate_line_inputs(qty, pu, tva)
                if error_key:
                    messagebox.showerror(t("common.error"), t(error_key))
                    return
                qty_f, pu_cents, tva_f = values
                line_ht, line_tva, _line_ttc = line_totals(
                    qty_f, pu_cents, tva_f
                )
                ht += line_ht
                tva_sum += line_tva
                validated_lines.append(
                    (
                        article_id,
                        art,
                        qty_f,
                        pu_cents,
                        tva_f,
                        line_ht,
                        line_tva,
                    )
                )

            discount_percent, error_key = _validate_discount_input(
                discount_enabled_var.get(), discount_entry.get()
            )
            if error_key:
                messagebox.showerror(t("common.error"), t(error_key))
                return

            discount_amount = discount_cents(ht, discount_percent)
            total_ttc = max(0, ht + tva_sum - discount_amount)

            doc_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            try:
                # The dialog only previewed this number; reserve the real one
                # inside the same transaction that creates the document.
                numero = self.get_next_numero(doc_type, cursor=cursor)
                cursor.execute(
                    """
                    INSERT INTO documents (type, numero, date, client_id, statut, total_ht, total_tva, total_ttc, paid_amount, payment_mode, discount_enabled, discount_value)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        doc_type,
                        numero,
                        doc_date,
                        client_id,
                        "brouillon",
                        ht,
                        tva_sum,
                        total_ttc,
                        0,
                        "none",
                        1 if discount_enabled_var.get() else 0,
                        discount_percent,
                    ),
                )
                doc_id = cursor.lastrowid

                for (
                    article_id,
                    art,
                    qty_f,
                    pu_cents,
                    tva_f,
                    line_ht,
                    line_tva,
                ) in validated_lines:
                    line_ttc = line_ht + line_tva
                    cursor.execute(
                        """
                        INSERT INTO document_lignes (document_id, article_id, designation, quantite, prix_unitaire, tva, total_ht, total_tva, total_ttc)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            doc_id,
                            int(article_id) if article_id not in (None, "") else None,
                            art,
                            qty_f,
                            pu_cents,
                            tva_f,
                            line_ht,
                            line_tva,
                            line_ttc,
                        ),
                    )

                conn.commit()
            except Exception as e:
                conn.rollback()
                messagebox.showerror(
                    t("common.error"), t("documents.error_save", error=str(e))
                )
                return
            finally:
                conn.close()

            self.load_documents(doc_type)
            self.load_paiements()
            
            # Show export dialog after saving
            dialog.destroy()
            self.show_new_doc_export_dialog(doc_id, doc_type)
            return

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=10)
        ctk.CTkButton(
            btn_frame,
            text=t("common.cancel"),
            command=dialog.destroy,
            fg_color="gray",
            hover_color="darkgray",
        ).pack(side="right", padx=10)
        ctk.CTkButton(btn_frame, text=t("common.save"), command=save_document).pack(
            side="right"
        )

    def on_document_double_click(self, event, doc_type, tree):
        selected = tree.selection()
        if not selected:
            return
        values = tree.item(selected, "values")
        if not values:
            return
        doc_id = int(values[0])
        self.show_document_details(doc_id)

    def show_document_details(self, document_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT d.id, d.type, d.numero, d.date, d.client_id, d.statut,
                       d.total_ht, d.total_tva, d.total_ttc,
                       d.paid_amount, d.payment_mode,
                       COALESCE(c.nom, ''), COALESCE(c.email, ''), COALESCE(c.telephone, ''), COALESCE(c.adresse, ''),
                       COALESCE(d.discount_enabled, 0), COALESCE(d.discount_value, 0)
                FROM documents d
                LEFT JOIN clients c ON c.id = d.client_id
                WHERE d.id = ?
                """,
                (document_id,),
            )
            doc = cursor.fetchone()
            if not doc:
                messagebox.showerror(t("common.error"), t("payment_dialog.error_doc_not_found"))
                return

            cursor.execute("SELECT id, nom FROM clients ORDER BY nom")
            clients = cursor.fetchall()

            cursor.execute(
                """
                SELECT dl.id, dl.article_id, a.reference, dl.designation, dl.quantite, dl.prix_unitaire, dl.tva
                FROM document_lignes dl
                LEFT JOIN articles a ON a.id = dl.article_id
                WHERE dl.document_id = ?
                ORDER BY dl.id
                """,
                (document_id,),
            )
            lines = cursor.fetchall()
        finally:
            conn.close()

        (
            _id,
            doc_type,
            numero,
            date_str,
            client_id,
            statut,
            total_ht,
            total_tva,
            total_ttc,
            paid_amount,
            payment_mode,
            client_nom,
            _client_email,
            _client_tel,
            _client_adresse,
            discount_enabled,
            discount_value,
        ) = doc

        title = {
            "devis": t("nav.devis"),
            "bl": t("nav.bl"),
            "factures": t("doc_type.factures"),
            "reception": t("nav.reception"),
        }.get(doc_type, doc_type)

        dialog = ctk.CTkToplevel(self)
        dialog.title(t("doc_details.dialog_title", title=title, number=numero))
        dialog.geometry("980x800")
        try:
            dialog.transient(self)
            dialog.grab_set()
        except Exception as e:
            print(f"Warning: could not set modal grab for document details dialog: {e}")

        is_locked = statut == "valide"

        header = ctk.CTkFrame(dialog, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 10))

        ctk.CTkLabel(header, text=f"{title}", font=self.ui["font_h1"]).pack(side="left")

        actions = ctk.CTkFrame(header, fg_color="transparent")
        actions.pack(side="right")
        ctk.CTkButton(
            actions,
            text=t("doc_details.button_export_print"),
            command=lambda: self.show_export_dialog_for_document(document_id),
        ).pack(side="right")

        top = ctk.CTkFrame(dialog, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(0, 10))

        left = ctk.CTkFrame(top, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        right = ctk.CTkFrame(top, fg_color="transparent")
        right.pack(side="right")

        ctk.CTkLabel(left, text=t("client_dialog.col_number"), font=self.ui["font_h2"]).grid(
            row=0, column=0, sticky="w"
        )
        numero_entry = ctk.CTkEntry(left, width=220)
        numero_entry.insert(0, str(numero or ""))
        numero_entry.grid(row=1, column=0, sticky="w", pady=(0, 8), padx=(0, 16))

        ctk.CTkLabel(left, text=t("article_dialog.col_date"), font=self.ui["font_h2"]).grid(
            row=0, column=1, sticky="w"
        )
        date_entry = ctk.CTkEntry(left, width=220)
        date_entry.insert(0, str(date_str or ""))
        date_entry.grid(row=1, column=1, sticky="w", pady=(0, 8))

        ctk.CTkLabel(left, text=t("documents.col_client"), font=self.ui["font_h2"]).grid(
            row=2, column=0, sticky="w", pady=(8, 0)
        )
        client_names = [c[1] for c in clients]
        client_map = {c[1]: c[0] for c in clients}
        reverse_client_map = {c[0]: c[1] for c in clients}
        client_combo = ttk.Combobox(
            left, values=client_names, state="readonly", width=46
        )
        if client_id in reverse_client_map:
            client_combo.set(reverse_client_map[client_id])
        elif client_nom:
            client_combo.set(client_nom)
        client_combo.grid(row=3, column=0, columnspan=2, sticky="w")

        statut_label = ctk.CTkLabel(
            right, text=f"Statut: {statut}", font=self.ui["font_h2"]
        )
        statut_label.pack(anchor="e")
        if is_locked:
            ctk.CTkLabel(
                right,
                text=t("doc_details.locked_notice"),
                font=self.ui["font_body"],
                text_color=("#B91C1C", "#EF4444"),
            ).pack(anchor="e", pady=(2, 0))

        content = ctk.CTkFrame(dialog, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=20, pady=(10, 10))

        columns = ("ArticleId", t("articles.col_reference"), t("articles.col_designation"), t("article_dialog.col_qty"), t("new_doc.col_pu"), t("articles.col_tva"))
        lines_tree = ttk.Treeview(
            content, columns=columns, show="headings", selectmode="browse"
        )
        for col in columns:
            lines_tree.heading(col, text=col)
            if col == "ArticleId":
                lines_tree.column(col, width=0, stretch=False)
                lines_tree.heading(col, text="")
            elif col == t("articles.col_reference"):
                lines_tree.column(col, width=100, anchor="center")
            elif col == t("articles.col_designation"):
                lines_tree.column(col, width=320, anchor="w")
            elif col == t("article_dialog.col_qty"):
                lines_tree.column(col, width=80, anchor="e")
            elif col == t("new_doc.col_pu"):
                lines_tree.column(col, width=100, anchor="e")
            elif col == t("articles.col_tva"):
                lines_tree.column(col, width=80, anchor="e")

        scrollbar = ttk.Scrollbar(content, orient="vertical", command=lines_tree.yview)
        scrollbar.pack(side="right", fill="y")
        lines_tree.configure(yscrollcommand=scrollbar.set)
        lines_tree.pack(fill="both", expand=True)

        for line_id, article_id, ref, des, qty, pu, tva in lines:
            lines_tree.insert(
                "",
                "end",
                values=(
                    int(article_id) if article_id is not None else "",
                    ref or "",
                    des or "",
                    float(qty or 0),
                    format_money(pu),
                    float(tva or 0),
                ),
            )
        self._apply_zebra(lines_tree)

        totals_bar = ctk.CTkFrame(dialog, fg_color="transparent")
        totals_bar.pack(fill="x", padx=20, pady=(0, 10))
        total_ht_value = format_money(total_ht)
        total_tva_value = format_money(total_tva)
        total_ttc_value = format_money(total_ttc)

        # Payment section (left side) - only invoices can receive payments.
        pay_entry = None
        pay_method = None
        if doc_type == "factures":
            payment_frame = ctk.CTkFrame(totals_bar, fg_color="transparent")
            payment_frame.pack(side="left", padx=(0, 20))

            ctk.CTkLabel(
                payment_frame, text=t("doc_details.payment_heading"), font=self.ui.get("font_body")
            ).pack(anchor="w")
            pay_entry = ctk.CTkEntry(payment_frame, width=140, placeholder_text=t("new_doc.discount_amount_label"))
            pay_entry.pack(anchor="w", pady=(4, 6))
            pay_method = ctk.CTkEntry(
                payment_frame, width=140, placeholder_text=t("doc_details.payment_method_placeholder")
            )
            pay_method.pack(anchor="w")
            if is_locked:
                pay_entry.configure(state="disabled")
                pay_method.configure(state="disabled")

        # Discount controls (middle - right of payment)
        discount_frame = ctk.CTkFrame(totals_bar, fg_color="transparent")
        discount_frame.pack(side="left", padx=(0, 20))
        
        discount_enabled_var = ctk.BooleanVar(value=bool(discount_enabled))
        discount_checkbox = ctk.CTkCheckBox(
            discount_frame, text=t("new_doc.discount_checkbox"), variable=discount_enabled_var
        )
        discount_checkbox.pack(anchor="w")
        
        discount_entry = ctk.CTkEntry(discount_frame, width=80, placeholder_text="%")
        discount_entry.pack(anchor="w", pady=(4, 0))
        if discount_value:
            discount_entry.insert(0, str(discount_value))
        
        if is_locked or not discount_enabled_var.get():
            discount_entry.configure(state="disabled")
            if is_locked:
                discount_checkbox.configure(state="disabled")

        def toggle_discount():
            if is_locked:
                return
            if discount_enabled_var.get():
                discount_entry.configure(state="normal")
                discount_left_frame.pack(side="left", padx=(0, 20))
            else:
                discount_entry.configure(state="disabled")
                discount_entry.delete(0, "end")
                discount_left_frame.pack_forget()
            recalc_totals_once()

        def on_discount_change(*args):
            if not is_locked:
                recalc_totals_once()

        discount_enabled_var.trace_add("write", lambda *_: toggle_discount())
        discount_entry.bind("<KeyRelease>", lambda e: on_discount_change())
        discount_entry.bind("<FocusOut>", lambda e: on_discount_change())

        # Discount display table (right side)
        discount_left_frame = ctk.CTkFrame(totals_bar, fg_color="transparent")
        if discount_enabled:
            discount_left_frame.pack(side="left", padx=(0, 20))
        
        # Discount percentage
        discount_pct_frame = ctk.CTkFrame(discount_left_frame, fg_color="transparent")
        discount_pct_frame.pack(fill="x")
        ctk.CTkLabel(
            discount_pct_frame,
            text=t("new_doc.discount_label"),
            font=self.ui["font_body"],
            text_color=("gray35", "gray70"),
        ).pack(side="left")
        discount_value_label = ctk.CTkLabel(
            discount_pct_frame,
            text=f"{float(discount_value or 0):.1f}%",
            font=self.ui["font_h2"]
        )
        discount_value_label.pack(side="right")
        
        # Discount amount
        discount_amount_frame = ctk.CTkFrame(discount_left_frame, fg_color="transparent")
        discount_amount_frame.pack(fill="x")
        ctk.CTkLabel(
            discount_amount_frame,
            text=t("new_doc.discount_amount_label"),
            font=self.ui["font_body"],
            text_color=("gray35", "gray70"),
        ).pack(side="left")
        discount_amount_value = discount_cents(total_ht, discount_value)
        discount_amount_label = ctk.CTkLabel(
            discount_amount_frame,
            text=f"-{format_money(discount_amount_value)}",
            font=self.ui["font_h2"]
        )
        discount_amount_label.pack(side="right")

        # Totals (far right)
        totals_right = ctk.CTkFrame(totals_bar, fg_color="transparent")
        totals_right.pack(side="right")

        ttc_box = ctk.CTkFrame(totals_right, fg_color="transparent")
        ttc_box.pack(side="right", padx=(0, 20))
        ctk.CTkLabel(
            ttc_box,
            text=t("documents.col_ttc"),
            font=self.ui["font_body"],
            text_color=("gray35", "gray70"),
        ).pack(anchor="e")
        ttc_value_label = ctk.CTkLabel(
            ttc_box, text=total_ttc_value, font=self.ui["font_h2"]
        )
        ttc_value_label.pack(anchor="e")

        tva_box = ctk.CTkFrame(totals_right, fg_color="transparent")
        tva_box.pack(side="right", padx=(0, 20))
        ctk.CTkLabel(
            tva_box,
            text=t("documents.col_tva"),
            font=self.ui["font_body"],
            text_color=("gray35", "gray70"),
        ).pack(anchor="e")
        tva_value_label = ctk.CTkLabel(
            tva_box, text=total_tva_value, font=self.ui["font_h2"]
        )
        tva_value_label.pack(anchor="e")

        ht_box = ctk.CTkFrame(totals_right, fg_color="transparent")
        ht_box.pack(side="right", padx=(0, 20))
        ctk.CTkLabel(
            ht_box,
            text=t("documents.col_ht"),
            font=self.ui["font_body"],
            text_color=("gray35", "gray70"),
        ).pack(anchor="e")
        ht_value_label = ctk.CTkLabel(
            ht_box, text=total_ht_value, font=self.ui["font_h2"]
        )
        ht_value_label.pack(anchor="e")

        def recalc_totals_once():
            ht = 0
            tva_sum = 0
            for item in lines_tree.get_children():
                _article_id, _ref, _des, qty, pu, tva = lines_tree.item(
                    item, "values"
                )
                values, error_key = _validate_line_inputs(qty, pu, tva)
                if error_key:
                    continue
                qty_f, pu_cents, tva_f = values
                line_ht, line_tva, _line_ttc = line_totals(
                    qty_f, pu_cents, tva_f
                )
                ht += line_ht
                tva_sum += line_tva
            
            discount_percent = 0.0
            discount_amount = 0
            if discount_enabled_var.get():
                discount_percent = (
                    _validate_percentage(discount_entry.get().strip() or 0) or 0.0
                )
                discount_amount = discount_cents(ht, discount_percent)
            
            ttc = max(0, ht + tva_sum - discount_amount)
            
            ht_value_label.configure(text=format_money(ht))
            tva_value_label.configure(text=format_money(tva_sum))
            ttc_value_label.configure(text=format_money(ttc))
            
            discount_value_label.configure(text=f"{discount_percent:.1f}%")
            discount_amount_label.configure(
                text=f"-{format_money(discount_amount)}"
            )

        def _get_selected_line_item():
            sel = lines_tree.selection()
            if not sel:
                return None
            return sel[0]

        def _on_client_selected(self):
            if not hasattr(self, "clients_unpaid_tree"):
                return
            sel = self.clients_unpaid_tree.selection()
            if not sel:
                return
            vals = self.clients_unpaid_tree.item(sel[0], "values")
            if not vals:
                return
            client_id = vals[0]
            try:
                client_id = int(client_id)
            except Exception:
                return

            self._refresh_documents_tree_for_client(client_id)

        def show_add_payment_dialog_for_selected(self):
            if not hasattr(self, "client_docs_tree"):
                messagebox.showerror(t("common.error"), t("payment_dialog.error_no_doc_selected"))
                return
            sel = self.client_docs_tree.selection()
            if not sel:
                messagebox.showerror(t("common.error"), t("documents.error_select"))
                return
            vals = self.client_docs_tree.item(sel[0], "values")
            if not vals:
                messagebox.showerror(t("common.error"), t("documents.error_select"))
                return
            doc_id = int(vals[0])
            self.show_add_payment_dialog_for(doc_id)

        def mark_selected_document_paid_full(self):
            if not hasattr(self, "client_docs_tree"):
                messagebox.showerror(t("common.error"), t("payment_dialog.error_no_doc_selected"))
                return
            sel = self.client_docs_tree.selection()
            if not sel:
                messagebox.showerror(t("common.error"), t("documents.error_select"))
                return
            vals = self.client_docs_tree.item(sel[0], "values")
            if not vals:
                messagebox.showerror(t("common.error"), t("documents.error_select"))
                return
            doc_id = int(vals[0])

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT total_ttc, paid_amount FROM documents WHERE id = ?",
                    (doc_id,),
                )
                row = cursor.fetchone()
                if not row:
                    messagebox.showerror(t("common.error"), t("payment_dialog.error_doc_not_found"))
                    return
                total_ttc = max(0, as_cents(row[0]))
                paid = max(0, as_cents(row[1]))
                delta = max(0, total_ttc - paid)
                date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if delta > 0:
                    cursor.execute(
                        "INSERT INTO payments (document_id, date, amount, method, note) VALUES (?, ?, ?, ?, ?)",
                        (doc_id, date_now, delta, "full", "Paiement total"),
                    )
                cursor.execute(
                    "UPDATE documents SET paid_amount = ?, payment_mode = ? WHERE id = ?",
                    (total_ttc, "full", doc_id),
                )
                conn.commit()
            finally:
                conn.close()
            self.load_paiements()
            for t in ("devis", "bl", "factures"):
                try:
                    self.load_documents(t)
                except Exception:
                    pass

        def open_line_editor(item_id=None):
            if is_locked:
                return

            dlg = ctk.CTkToplevel(dialog)
            dlg.title("Ligne")
            dlg.geometry("520x480")
            try:
                dlg.transient(dialog)
                dlg.grab_set()
            except Exception as e:
                print(f"Warning: could not set modal grab for line editor dialog: {e}")

            header = ctk.CTkFrame(dlg, fg_color="transparent")
            header.pack(fill="x", padx=20, pady=(15, 10))
            ctk.CTkLabel(
                header,
                text=t("new_doc.line_dialog_title_edit")
                if item_id is not None
                else t("new_doc.line_dialog_title_add"),
                font=self.ui["font_h2"],
            ).pack(side="left")

            form = ctk.CTkFrame(dlg, fg_color="transparent")
            form.pack(fill="both", expand=True, padx=20, pady=(0, 10))

            form.grid_columnconfigure(0, weight=1)
            form.grid_columnconfigure(1, weight=1)

            current = {
                "article_id": "",
                "reference": "",
                "designation": "",
                "qty": "1",
                "pu": "0",
                "tva": "0",
            }
            if item_id is not None:
                vals = lines_tree.item(item_id, "values")
                if vals:
                    current["article_id"] = vals[0]
                    current["reference"] = vals[1]
                    current["designation"] = vals[2]
                    current["qty"] = str(vals[3])
                    current["pu"] = str(vals[4])
                    current["tva"] = str(vals[5])

            conn2 = sqlite3.connect(self.db_path)
            cur2 = conn2.cursor()
            try:
                cur2.execute(
                    "SELECT id, designation, prix_vente, tva FROM articles ORDER BY designation"
                )
                articles = cur2.fetchall()
            finally:
                conn2.close()

            names = [a[1] for a in articles]
            id_map = {a[1]: int(a[0]) for a in articles}
            price_map = {a[1]: as_cents(a[2]) for a in articles}
            tva_map = {a[1]: float(a[3] or 0) for a in articles}

            ctk.CTkLabel(
                form, text=t("new_doc.label_article_optional"), font=self.ui["font_body"]
            ).grid(row=0, column=0, columnspan=2, sticky="w")

            # Container for article search entry and dropdown
            article_search_container = ctk.CTkFrame(form, fg_color="transparent")
            article_search_container.grid(
                row=1, column=0, columnspan=2, sticky="we", pady=(0, 10)
            )

            # Article search entry (like client search)
            article_search_entry = ctk.CTkEntry(
                article_search_container,
                width=540,
                placeholder_text=t("articles.search_placeholder"),
            )
            article_search_entry.pack(fill="x")

            # Autocomplete dropdown for articles
            article_dropdown = ctk.CTkFrame(
                article_search_container, fg_color="gray20", corner_radius=6
            )
            article_dropdown.pack(fill="x", pady=(2, 0), expand=False)
            article_dropdown.pack_forget()  # Hide initially

            article_dropdown_items = []

            def on_article_keyrelease(event):
                query = article_search_entry.get().strip()

                if not query:
                    for widget in article_dropdown_items:
                        widget.destroy()
                    article_dropdown_items.clear()
                    article_dropdown.pack_forget()
                    return

                # Get matching articles from database
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                try:
                    cursor.execute(
                        """
                        SELECT DISTINCT
                            CASE
                                WHEN CAST(a.id AS TEXT) LIKE ? THEN a.id || ' - ' || COALESCE(a.reference,'') || ' - ' || COALESCE(a.designation,'')
                                WHEN a.reference LIKE ? THEN a.id || ' - ' || COALESCE(a.reference,'') || ' - ' || COALESCE(a.designation,'')
                                ELSE a.id || ' - ' || COALESCE(a.reference,'') || ' - ' || COALESCE(a.designation,'')
                            END as display_value,
                            a.id,
                            a.reference,
                            a.designation,
                            a.prix_vente,
                            a.tva
                        FROM articles a
                        WHERE CAST(a.id AS TEXT) LIKE ? OR
                              a.reference LIKE ? OR
                              a.designation LIKE ?
                        ORDER BY
                            CASE
                                WHEN CAST(a.id AS TEXT) LIKE ? THEN 1
                                WHEN a.reference LIKE ? THEN 2
                                ELSE 3
                            END,
                            a.designation
                        LIMIT 5
                        """,
                        (
                            f"%{query}%",
                            f"%{query}%",
                            f"%{query}%",
                            f"%{query}%",
                            f"%{query}%",
                            f"%{query}%",
                            f"%{query}%",
                        ),
                    )
                    results = cursor.fetchall()
                finally:
                    conn.close()

                # Clear existing dropdown items
                for widget in article_dropdown_items:
                    widget.destroy()
                article_dropdown_items.clear()

                if results:
                    # Add new items
                    for row in results:
                        display_value = row[0]
                        article_id = row[1]
                        prix_vente = row[4]
                        tva = row[5]

                        btn = ctk.CTkButton(
                            article_dropdown,
                            text=display_value,
                            command=lambda d=display_value,
                            id=article_id,
                            pv=prix_vente,
                            tv=tva: select_article_suggestion(d, id, pv, tv),
                            fg_color="transparent",
                            text_color="white",
                            hover_color="gray40",
                            anchor="w",
                            height=28,
                            corner_radius=4,
                        )
                        btn.pack(fill="x", padx=2, pady=1)
                        article_dropdown_items.append(btn)

                    # Show dropdown
                    article_dropdown.pack(fill="x", pady=(2, 0), expand=False)
                else:
                    article_dropdown.pack_forget()

            def select_article_suggestion(display_value, article_id, prix_vente, tva):
                article_search_entry.delete(0, "end")
                article_search_entry.insert(0, display_value)

                # Clear dropdown
                for widget in article_dropdown_items:
                    widget.destroy()
                article_dropdown_items.clear()
                article_dropdown.pack_forget()

                # Auto-fill price and tva
                if prix_vente is not None:
                    pu_var.set(money_to_input(prix_vente))
                if tva is not None:
                    tva_var.set(str(tva))

                # Update preview
                update_preview()

            article_search_entry.bind("<KeyRelease>", on_article_keyrelease)
            article_search_entry.bind(
                "<Escape>",
                lambda e: [
                    article_dropdown.pack_forget(),
                    [w.destroy() for w in article_dropdown_items],
                    article_dropdown_items.clear(),
                ],
            )
            article_search_entry.bind(
                "<FocusOut>",
                lambda e: self.after(
                    150,
                    lambda: [
                        article_dropdown.pack_forget(),
                        [w.destroy() for w in article_dropdown_items],
                        article_dropdown_items.clear(),
                    ],
                ),
            )
            article_dropdown.bind("<Button-1>", lambda e: "break")

            ctk.CTkLabel(form, text=t("articles.col_designation"), font=self.ui["font_body"]).grid(
                row=2, column=0, columnspan=2, sticky="w"
            )
            des_entry = ctk.CTkEntry(form)
            des_entry.insert(0, current["designation"])
            des_entry.grid(row=3, column=0, columnspan=2, sticky="we", pady=(0, 10))

            ctk.CTkLabel(form, text=t("new_doc.col_qty"), font=self.ui["font_body"]).grid(
                row=4, column=0, sticky="w"
            )
            qty_var = ctk.StringVar(value=current["qty"])
            qty_entry = ctk.CTkEntry(form, textvariable=qty_var)
            qty_entry.grid(row=5, column=0, sticky="we", pady=(0, 10), padx=(0, 10))

            ctk.CTkLabel(form, text=t("new_doc.label_pu"), font=self.ui["font_body"]).grid(
                row=4, column=1, sticky="w"
            )
            pu_var = ctk.StringVar(value=current["pu"])
            pu_entry = ctk.CTkEntry(form, textvariable=pu_var)
            pu_entry.grid(row=5, column=1, sticky="we", pady=(0, 10))

            ctk.CTkLabel(form, text=t("article_dialog.label_tva"), font=self.ui["font_body"]).grid(
                row=6, column=0, sticky="w"
            )
            tva_var = ctk.StringVar(value=current["tva"])
            tva_entry = ctk.CTkEntry(form, textvariable=tva_var)
            tva_entry.grid(row=7, column=0, sticky="we", pady=(0, 10), padx=(0, 10))

            preview = ctk.CTkFrame(form, fg_color="transparent")
            preview.grid(row=7, column=1, sticky="we", pady=(0, 10))
            ctk.CTkLabel(
                preview,
                text=t("new_doc.preview_heading"),
                font=self.ui["font_body"],
                text_color=("gray35", "gray70"),
            ).pack(anchor="w")
            prev_ht_label = ctk.CTkLabel(
                preview, text="HT: 0.00", font=self.ui["font_body"]
            )
            prev_ht_label.pack(anchor="w")
            prev_tva_label = ctk.CTkLabel(
                preview, text="TVA: 0.00", font=self.ui["font_body"]
            )
            prev_tva_label.pack(anchor="w")
            prev_ttc_label = ctk.CTkLabel(
                preview, text="TTC: 0.00", font=self.ui["font_body"]
            )
            prev_ttc_label.pack(anchor="w")

            def update_preview(event=None):
                try:
                    qty_f = _parse_finite_number(qty_entry.get().strip() or 0)
                    pu_cents = parse_money(pu_entry.get().strip() or 0)
                    tva_f = _parse_finite_number(tva_entry.get().strip() or 0)
                    if qty_f is None or pu_cents is None or tva_f is None:
                        raise ValueError("non-finite number")
                except Exception:
                    prev_ht_label.configure(text="HT: -")
                    prev_tva_label.configure(text="TVA: -")
                    prev_ttc_label.configure(text="TTC: -")
                    return
                line_ht, line_tva, line_ttc = line_totals(qty_f, pu_cents, tva_f)
                prev_ht_label.configure(text=f"HT: {format_money(line_ht)}")
                prev_tva_label.configure(text=f"TVA: {format_money(line_tva)}")
                prev_ttc_label.configure(text=f"TTC: {format_money(line_ttc)}")

            qty_var.trace_add("write", lambda *_: update_preview())
            pu_var.trace_add("write", lambda *_: update_preview())
            tva_var.trace_add("write", lambda *_: update_preview())
            qty_entry.bind("<FocusOut>", update_preview)
            pu_entry.bind("<FocusOut>", update_preview)
            tva_entry.bind("<FocusOut>", update_preview)
            update_preview()

            def save_line():
                des = des_entry.get().strip()
                if not des:
                    messagebox.showerror(t("common.error"), t("article_dialog.error_designation_required"))
                    return
                values, error_key = _validate_line_inputs(
                    qty_entry.get(), pu_entry.get(), tva_entry.get()
                )
                if error_key:
                    messagebox.showerror(t("common.error"), t(error_key))
                    return
                qty_f, pu_cents, tva_f = values

                # Get article info from search entry (format: "ID - REF - Designation")
                display_value = article_search_entry.get().strip()
                article_id_val = ""
                reference_val = ""
                if " - " in display_value:
                    parts = display_value.split(" - ", 2)
                    if len(parts) >= 2:
                        id_part = parts[0]
                        ref_part = parts[1]
                        if id_part.isdigit():
                            article_id_val = int(id_part)
                        reference_val = ref_part

                row_values = (
                    article_id_val,
                    reference_val,
                    des,
                    qty_f,
                    format_money(pu_cents),
                    tva_f,
                )
                if item_id is None:
                    lines_tree.insert("", "end", values=row_values)
                else:
                    lines_tree.item(item_id, values=row_values)

                self._apply_zebra(lines_tree)
                recalc_totals_once()
                dlg.destroy()

            btns = ctk.CTkFrame(dlg, fg_color="transparent")
            btns.pack(fill="x", padx=20, pady=(0, 15))
            ctk.CTkButton(
                btns,
                text=t("common.cancel"),
                command=dlg.destroy,
                fg_color="gray",
                hover_color="darkgray",
            ).pack(side="right", padx=10)
            ctk.CTkButton(btns, text=t("common.save"), command=save_line).pack(
                side="right"
            )

            des_entry.focus_set()

        def add_line():
            open_line_editor(None)

        def edit_line():
            item_id = _get_selected_line_item()
            if not item_id:
                messagebox.showerror(t("common.error"), t("doc_details.error_select_line"))
                return
            open_line_editor(item_id)

        def delete_line():
            if is_locked:
                return
            item_id = _get_selected_line_item()
            if not item_id:
                messagebox.showerror(t("common.error"), t("doc_details.error_select_line"))
                return
            if not messagebox.askyesno(t("common.delete"), t("doc_details.confirm_delete_line")):
                return
            lines_tree.delete(item_id)
            self._apply_zebra(lines_tree)
            recalc_totals_once()

        def save_document_changes():
            if is_locked:
                return

            new_numero = numero_entry.get().strip()
            new_date = date_entry.get().strip()
            if not new_numero:
                messagebox.showerror(t("common.error"), t("doc_details.error_number_required"))
                return

            new_date_value = _validate_document_date(new_date)
            if new_date_value is None:
                messagebox.showerror(
                    t("common.error"), t("doc_details.error_date_invalid")
                )
                return

            client_name = client_combo.get().strip()
            new_client_id = client_map.get(client_name) if client_name else None
            if new_client_id is None:
                messagebox.showerror(
                    t("common.error"), t("doc_details.error_client_required")
                )
                return

            items = lines_tree.get_children()
            if not items:
                messagebox.showerror(t("common.error"), t("doc_details.error_no_lines"))
                return

            # Validate every line before touching the database so a bad row
            # cannot leave the document with partially replaced lines.
            validated_lines = []
            ht = 0
            tva_sum = 0
            for item in items:
                article_id_val, _ref, des, qty, pu, tva = lines_tree.item(
                    item, "values"
                )
                if not str(des).strip():
                    messagebox.showerror(
                        t("common.error"), t("doc_details.error_designation_required")
                    )
                    return
                values, error_key = _validate_line_inputs(qty, pu, tva)
                if error_key:
                    messagebox.showerror(t("common.error"), t(error_key))
                    return
                qty_f, pu_cents, tva_f = values
                line_ht, line_tva, _line_ttc = line_totals(
                    qty_f, pu_cents, tva_f
                )
                ht += line_ht
                tva_sum += line_tva
                validated_lines.append(
                    (
                        article_id_val,
                        des,
                        qty_f,
                        pu_cents,
                        tva_f,
                        line_ht,
                        line_tva,
                    )
                )

            discount_percent, error_key = _validate_discount_input(
                discount_enabled_var.get(), discount_entry.get()
            )
            if error_key:
                messagebox.showerror(t("common.error"), t(error_key))
                return

            discount_amount = discount_cents(ht, discount_percent)
            total_ttc = max(0, ht + tva_sum - discount_amount)

            conn3 = sqlite3.connect(self.db_path)
            cur3 = conn3.cursor()
            payment_applied = False
            try:
                cur3.execute(
                    "SELECT statut FROM documents WHERE id = ?",
                    (document_id,),
                )
                row = cur3.fetchone()
                if not row:
                    messagebox.showerror(
                        t("common.error"), t("payment_dialog.error_doc_not_found")
                    )
                    conn3.rollback()
                    return
                if row[0] == "valide":
                    messagebox.showerror(
                        t("common.error"), t("doc_details.error_already_validated")
                    )
                    conn3.rollback()
                    return

                if _numero_exists(cur3, new_numero, exclude_id=document_id):
                    conn3.rollback()
                    messagebox.showerror(
                        t("common.error"),
                        t("doc_details.error_number_duplicate", number=new_numero),
                    )
                    return

                cur3.execute(
                    """
                    UPDATE documents
                    SET numero = ?, date = ?, client_id = ?,
                        total_ht = ?, total_tva = ?, total_ttc = ?,
                        discount_enabled = ?, discount_value = ?
                    WHERE id = ?
                    """,
                    (
                        new_numero,
                        new_date_value,
                        new_client_id,
                        ht,
                        tva_sum,
                        total_ttc,
                        1 if discount_enabled_var.get() else 0,
                        discount_percent,
                        document_id,
                    ),
                )

                cur3.execute(
                    "DELETE FROM document_lignes WHERE document_id = ?", (document_id,)
                )
                for (
                    article_id_val,
                    des,
                    qty_f,
                    pu_cents,
                    tva_f,
                    line_ht,
                    line_tva,
                ) in validated_lines:
                    line_ttc = line_ht + line_tva

                    art_id = None
                    if article_id_val not in (None, ""):
                        try:
                            art_id = int(float(article_id_val))
                        except (TypeError, ValueError):
                            art_id = None

                    cur3.execute(
                        """
                        INSERT INTO document_lignes (document_id, article_id, designation, quantite, prix_unitaire, tva, total_ht, total_tva, total_ttc)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            document_id,
                            art_id,
                            des,
                            qty_f,
                            pu_cents,
                            tva_f,
                            line_ht,
                            line_tva,
                            line_ttc,
                        ),
                    )

                # Payments are validated against the freshly saved total.
                if pay_entry is not None:
                    amount_str = pay_entry.get().strip()
                    if amount_str:
                        error_key, error_kwargs = _apply_payment(
                            cur3,
                            document_id,
                            amount_str,
                            pay_method.get().strip() or None,
                        )
                        if error_key:
                            conn3.rollback()
                            messagebox.showerror(
                                t("common.error"), t(error_key, **error_kwargs)
                            )
                            return
                        payment_applied = True

                conn3.commit()
            except Exception as e:
                conn3.rollback()
                messagebox.showerror(
                    t("common.error"), t("documents.error_update", error=str(e))
                )
                return
            finally:
                conn3.close()

            if payment_applied:
                pay_entry.delete(0, "end")
                pay_method.delete(0, "end")

            self.load_documents(doc_type)
            self.load_paiements()
            messagebox.showinfo(
                t("doc_details.saved_title"), t("doc_details.saved_message")
            )
            dialog.title(f"Détails {title} - {new_numero}")

        toolbar = ctk.CTkFrame(dialog, fg_color="transparent")
        toolbar.pack(fill="x", padx=20, pady=(0, 10))
        
        # Left side - line action buttons
        add_btn = ctk.CTkButton(toolbar, text=t("doc_details.button_add_line"), command=add_line)
        add_btn.pack(side="left")
        edit_btn = ctk.CTkButton(toolbar, text=t("doc_details.button_edit_line"), command=edit_line)
        edit_btn.pack(side="left", padx=10)
        add_btn.configure(text=t("doc_details.button_add_line"))
        edit_btn.configure(text=t("doc_details.button_edit_line"))
        del_btn = ctk.CTkButton(
            toolbar,
            text=t("doc_details.button_delete_line"),
            command=delete_line,
            fg_color=self.ui["danger_fg"],
            hover_color=self.ui["danger_hover"],
        )
        del_btn.pack(side="left")
        
        # Right side - save and close buttons
        ctk.CTkButton(
            toolbar,
            text=t("common.close"),
            command=dialog.destroy,
            fg_color="gray",
            hover_color="darkgray",
        ).pack(side="right", padx=(10, 0))
        save_btn = ctk.CTkButton(
            toolbar, text=t("common.save"), command=save_document_changes
        )
        save_btn.pack(side="right")

        if is_locked:
            numero_entry.configure(state="disabled")
            date_entry.configure(state="disabled")
            add_btn.configure(state="disabled")
            edit_btn.configure(state="disabled")
            del_btn.configure(state="disabled")
            save_btn.configure(state="disabled")
            try:
                client_combo.configure(state="disabled")
            except Exception:
                pass

        def _update_line_buttons(evt=None):
            if is_locked:
                return
            sel = lines_tree.selection()
            state = "normal" if sel else "disabled"
            try:
                edit_btn.configure(state=state)
                del_btn.configure(state=state)
            except Exception:
                pass

        lines_tree.bind("<<TreeviewSelect>>", _update_line_buttons)
        lines_tree.bind("<Double-1>", lambda _e: edit_line())
        recalc_totals_once()

    def _get_selected_doc_id_from_type(self, doc_type):
        tree = getattr(self, "documents_trees", {}).get(doc_type)
        if not tree:
            return None
        sel = tree.selection()
        if not sel:
            return None
        vals = tree.item(sel, "values")
        if not vals:
            return None
        return int(vals[0])

    def convert_selected_document(self, from_type, to_type):
        doc_id = self._get_selected_doc_id_from_type(from_type)
        if not doc_id:
            messagebox.showerror(t("common.error"), t("documents.error_select"))
            return

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT id, type, numero, date, client_id, statut,
                       total_ht, total_tva, total_ttc,
                       paid_amount, payment_mode, echeance,
                       discount_enabled, discount_value, converted_to_id
                FROM documents
                WHERE id = ?
                """,
                (doc_id,),
            )
            doc = cursor.fetchone()
            if not doc:
                return

            if to_type == "bl" and doc[1] != "devis":
                messagebox.showerror(t("common.error"), t("documents.error_convert_devis_only"))
                return
            if to_type == "factures" and doc[1] != "bl":
                messagebox.showerror(t("common.error"), t("documents.error_convert_bl_only"))
                return

            # A document may only be converted once. A dangling link (target
            # deleted) is ignored so the conversion can be repeated.
            if doc[14]:
                cursor.execute("SELECT 1 FROM documents WHERE id = ?", (doc[14],))
                if cursor.fetchone():
                    messagebox.showerror(
                        t("common.error"), t("documents.error_already_converted")
                    )
                    return

            new_numero = self.get_next_numero(to_type, cursor=cursor)
            new_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            source_bl_id = doc_id if to_type == "factures" else None

            cursor.execute(
                """
                INSERT INTO documents (type, numero, date, client_id, statut,
                                       total_ht, total_tva, total_ttc,
                                       paid_amount, payment_mode, echeance,
                                       discount_enabled, discount_value,
                                       source_bl_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    to_type,
                    new_numero,
                    new_date,
                    doc[4],
                    "brouillon",
                    doc[6],
                    doc[7],
                    doc[8],
                    as_cents(doc[9]),
                    doc[10] or "none",
                    doc[11],
                    doc[12] or 0,
                    doc[13] or 0.0,
                    source_bl_id,
                ),
            )
            new_doc_id = cursor.lastrowid

            cursor.execute(
                """
                SELECT article_id, designation, quantite, prix_unitaire, tva, total_ht, total_tva, total_ttc
                FROM document_lignes
                WHERE document_id = ?
                """,
                (doc_id,),
            )
            for (
                article_id,
                designation,
                quantite,
                prix_unitaire,
                tva,
                tht,
                ttva,
                tttc,
            ) in cursor.fetchall():
                cursor.execute(
                    """
                    INSERT INTO document_lignes (document_id, article_id, designation, quantite, prix_unitaire, tva, total_ht, total_tva, total_ttc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_doc_id,
                        article_id,
                        designation,
                        quantite,
                        prix_unitaire,
                        tva,
                        tht,
                        ttva,
                        tttc,
                    ),
                )

            # Payments recorded against the source (for example a deposit on a
            # quote) follow the document into its converted form.
            cursor.execute(
                "UPDATE payments SET document_id = ? WHERE document_id = ?",
                (new_doc_id, doc_id),
            )

            cursor.execute(
                """
                UPDATE documents
                SET converted_to_id = ?, converted_at = ?,
                    paid_amount = 0, payment_mode = 'none'
                WHERE id = ?
                """,
                (new_doc_id, new_date, doc_id),
            )

            conn.commit()
        except Exception as e:
            conn.rollback()
            messagebox.showerror(t("common.error"), str(e))
            return
        finally:
            conn.close()

        self.load_documents(to_type)
        self.load_paiements()
        messagebox.showinfo(
            "Conversion", t("documents.convert_success", number=new_numero)
        )

    def validate_selected_document(self, doc_type):
        doc_id = self._get_selected_doc_id_from_type(doc_type)
        if not doc_id:
            messagebox.showerror(t("common.error"), t("documents.error_select"))
            return

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT type, statut FROM documents WHERE id = ?", (doc_id,))
            row = cursor.fetchone()
            if not row:
                return
            dtype, statut = row
            if statut == "valide":
                messagebox.showinfo("Validation", t("documents.already_validated"))
                return

            if dtype == "reception":
                cursor.execute(
                    "SELECT article_id, quantite FROM document_lignes WHERE document_id = ?",
                    (doc_id,),
                )
                for article_id, qty in cursor.fetchall():
                    if article_id is None:
                        continue
                    cursor.execute(
                        "UPDATE articles SET stock = stock + ? WHERE id = ?",
                        (int(float(qty)), int(article_id)),
                    )

            elif dtype == "bl":
                cursor.execute(
                    "SELECT article_id, quantite FROM document_lignes WHERE document_id = ?",
                    (doc_id,),
                )

                for article_id, qty in cursor.fetchall():
                    if article_id is None:
                        continue
                    cursor.execute(
                        "SELECT stock, designation FROM articles WHERE id = ?",
                        (int(article_id),),
                    )
                    art_row = cursor.fetchone()
                    if not art_row:
                        continue
                    stock, designation = art_row
                    needed = int(float(qty))
                    if stock is None:
                        stock = 0
                    if stock < needed:
                        raise ValueError(
                            t("documents.error_insufficient_stock", name=designation, stock=str(stock), required=str(needed))
                        )

                cursor.execute(
                    "SELECT article_id, quantite FROM document_lignes WHERE document_id = ?",
                    (doc_id,),
                )
                for article_id, qty in cursor.fetchall():
                    if article_id is None:
                        continue
                    cursor.execute(
                        "UPDATE articles SET stock = stock - ? WHERE id = ?",
                        (int(float(qty)), int(article_id)),
                    )

            cursor.execute(
                "UPDATE documents SET statut = 'valide' WHERE id = ?", (doc_id,)
            )
            conn.commit()
        except ValueError as e:
            conn.rollback()
            messagebox.showerror(t("articles.col_stock"), str(e))
            return
        finally:
            conn.close()

        self.load_documents(doc_type)
        self.load_paiements()
        self.load_articles()
        messagebox.showinfo("Validation", t("documents.validated_success"))

    def create_paiements_frame(self):
        frame = ctk.CTkFrame(self.main_frame, corner_radius=0, fg_color="transparent")
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 8))

        self.paiements_header_label = ctk.CTkLabel(header, text=t("paiements.header_title"), font=self.ui["font_h1"])
        self.paiements_header_label.pack(side="left")

        container = ctk.CTkFrame(frame, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=6, pady=(8, 0))

        left = ctk.CTkFrame(container, width=320, fg_color="transparent")
        left.pack(side="left", fill="y", padx=(0, 8), pady=6)

        right = ctk.CTkFrame(container, fg_color="transparent")
        right.pack(side="right", fill="both", expand=True, pady=6)

        self.paiements_clients_heading = ctk.CTkLabel(left, text=t("paiements.clients_unpaid_heading"), font=self.ui.get("font_h2"))
        self.paiements_clients_heading.pack(anchor="w", pady=(0, 8))

        client_search_frame = ctk.CTkFrame(left, fg_color="transparent")
        client_search_frame.pack(fill="x", pady=(0, 6))

        # Container for client search entry and dropdown
        self.paiements_client_search_container = ctk.CTkFrame(
            client_search_frame, fg_color="transparent"
        )
        self.paiements_client_search_container.pack(side="left", fill="x", expand=True)

        self.paiements_client_search = ctk.CTkEntry(
            self.paiements_client_search_container,
            width=250,
            placeholder_text=t("paiements.search_client_placeholder"),
        )
        self.paiements_client_search.pack(fill="x")

        # Autocomplete dropdown for unpaid clients
        self.paiements_client_dropdown = ctk.CTkFrame(
            self.paiements_client_search_container, fg_color="gray20", corner_radius=6
        )
        self.paiements_client_dropdown.pack(fill="x", pady=(2, 0), expand=False)
        self.paiements_client_dropdown.pack_forget()

        self.paiements_client_dropdown_items = []

        # Search button
        self.paiements_search_btn = ctk.CTkButton(
            client_search_frame,
            text=t("common.search"),
            command=self.search_paiements_clients,
        )
        self.paiements_search_btn.pack(side="left", padx=(4, 0))

        # Bind autocomplete events
        self.paiements_client_search.bind(
            "<KeyRelease>", self.on_paiements_client_search_keyrelease
        )
        self.paiements_client_search.bind("<Return>", self.search_paiements_clients)
        self.paiements_client_search.bind(
            "<Escape>", lambda e: self.hide_paiements_client_dropdown()
        )
        self.paiements_client_search.bind(
            "<FocusOut>", lambda e: self.after(150, self.hide_paiements_client_dropdown)
        )
        self.paiements_client_dropdown.bind("<Button-1>", lambda e: "break")

        client_cols = ("ClientID", t("documents.col_client"), "Impayé")
        self.clients_unpaid_tree = ttk.Treeview(
            left, columns=client_cols, show="headings", selectmode="browse", height=20
        )
        for c in client_cols:
            self.clients_unpaid_tree.heading(
                c, text=c if c != "Impayé" else t("paiements.col_unpaid")
            )
            self.clients_unpaid_tree.column(c, width=100, anchor="center")
        self.clients_unpaid_tree.column(t("documents.col_client"), width=180, anchor="w")
        self.clients_unpaid_tree.pack(fill="y", expand=True)
        client_scroll = ttk.Scrollbar(
            left, orient="vertical", command=self.clients_unpaid_tree.yview
        )
        client_scroll.pack(side="right", fill="y")
        self.clients_unpaid_tree.configure(yscrollcommand=client_scroll.set)

        clients_actions = ctk.CTkFrame(left, fg_color="transparent")
        clients_actions.pack(fill="x", pady=(8, 0))
        self.paiements_refresh_btn = ctk.CTkButton(
            clients_actions, text=t("paiements.button_refresh"), command=self.load_paiements
        )
        self.paiements_refresh_btn.pack(side="left")

        self.paiements_docs_heading = ctk.CTkLabel(right, text=t("paiements.docs_unpaid_heading"), font=self.ui.get("font_h2"))
        self.paiements_docs_heading.pack(anchor="w", pady=(0, 8))

        doc_search_frame = ctk.CTkFrame(right, fg_color="transparent")
        doc_search_frame.pack(fill="x", pady=(0, 6))

        # Container for document search entry and dropdown
        self.paiements_doc_search_container = ctk.CTkFrame(
            doc_search_frame, fg_color="transparent"
        )
        self.paiements_doc_search_container.pack(side="left", fill="x", expand=True)

        self.paiements_doc_search = ctk.CTkEntry(
            self.paiements_doc_search_container,
            width=250,
            placeholder_text=t("paiements.search_doc_placeholder"),
        )
        self.paiements_doc_search.pack(fill="x")

        # Autocomplete dropdown for unpaid documents
        self.paiements_doc_dropdown = ctk.CTkFrame(
            self.paiements_doc_search_container, fg_color="gray20", corner_radius=6
        )
        self.paiements_doc_dropdown.pack(fill="x", pady=(2, 0), expand=False)
        self.paiements_doc_dropdown.pack_forget()

        self.paiements_doc_dropdown_items = []

        # Search button
        self.paiements_doc_search_btn = ctk.CTkButton(
            doc_search_frame, text=t("common.search"), command=self.search_paiements_docs
        )
        self.paiements_doc_search_btn.pack(side="left", padx=(4, 0))

        # Bind autocomplete events
        self.paiements_doc_search.bind(
            "<KeyRelease>", self.on_paiements_doc_search_keyrelease
        )
        self.paiements_doc_search.bind("<Return>", self.search_paiements_docs)
        self.paiements_doc_search.bind(
            "<Escape>", lambda e: self.hide_paiements_doc_dropdown()
        )
        self.paiements_doc_search.bind(
            "<FocusOut>", lambda e: self.after(150, self.hide_paiements_doc_dropdown)
        )
        self.paiements_doc_dropdown.bind("<Button-1>", lambda e: "break")

        doc_cols = ("ID", t("client_dialog.col_number"), t("client_dialog.col_type"), t("documents.col_ttc"), t("common.paid"), t("paiements.col_remaining"))
        self.client_docs_tree = ttk.Treeview(
            right, columns=doc_cols, show="headings", selectmode="browse"
        )
        for c in doc_cols:
            self.client_docs_tree.heading(c, text=c)
            self.client_docs_tree.column(c, width=120, anchor="center")
        self.client_docs_tree.column(t("client_dialog.col_number"), width=140, anchor="w")
        self.client_docs_tree.column(t("client_dialog.col_type"), width=100, anchor="center")
        self.client_docs_tree.pack(fill="both", expand=True)
        doc_scroll = ttk.Scrollbar(
            right, orient="vertical", command=self.client_docs_tree.yview
        )
        doc_scroll.pack(side="right", fill="y")
        self.client_docs_tree.configure(yscrollcommand=doc_scroll.set)

        doc_actions = ctk.CTkFrame(right, fg_color="transparent")
        doc_actions.pack(fill="x", pady=(8, 0))
        self.paiements_record_btn = ctk.CTkButton(
            doc_actions,
            text=t("paiements.button_record_payment"),
            command=lambda: self.show_add_payment_dialog_for_selected(),
        )
        self.paiements_record_btn.pack(side="left")
        self.paiements_mark_paid_btn = ctk.CTkButton(
            doc_actions,
            text=t("paiements.button_mark_paid"),
            command=lambda: self.mark_selected_document_paid_full(),
        )
        self.paiements_mark_paid_btn.pack(side="left", padx=8)

        self.clients_unpaid_tree.bind(
            "<<TreeviewSelect>>", lambda e: self._on_client_selected()
        )
        
        # Add sorting functionality to paiements trees
        self.paiements_sort_state = {}  # Track sort state per tree and column
        
        # Clients unpaid tree sorting
        client_cols = ("ClientID", t("documents.col_client"), "Impayé")
        for col in client_cols:
            self.clients_unpaid_tree.heading(col, command=lambda c=col: self.sort_paiements_clients(c))
        
        # Documents tree sorting
        doc_cols = ("ID", t("client_dialog.col_number"), t("client_dialog.col_type"), t("documents.col_ttc"), t("common.paid"), t("paiements.col_remaining"))
        for col in doc_cols:
            self.client_docs_tree.heading(col, command=lambda c=col: self.sort_paiements_docs(c))

        self.load_paiements()
        self.frames["paiements"] = frame

    def sort_paiements_clients(self, column):
        """Sort unpaid clients by column (ascending/descending toggle)."""
        if not hasattr(self, "clients_unpaid_tree"):
            return
        
        # Get current sort state for this column
        if not hasattr(self, "paiements_sort_state"):
            self.paiements_sort_state = {}
        if "clients" not in self.paiements_sort_state:
            self.paiements_sort_state["clients"] = {}
        
        current_state = self.paiements_sort_state["clients"].get(column, False)
        new_state = not current_state
        self.paiements_sort_state["clients"][column] = new_state
        
        # Update all headers to show arrows
        arrow = "▲" if new_state else "▼"
        all_cols = ["ClientID", t("documents.col_client"), "Impayé"]
        for col in all_cols:
            if col == column:
                self.clients_unpaid_tree.heading(col, text=f"{col} {arrow}")
            else:
                # Remove arrow from other columns
                clean_col = col.replace(" ▲", "").replace(" ▼", "")
                self.clients_unpaid_tree.heading(col, text=clean_col)
        
        # Get all items with their tags
        items = []
        for item in self.clients_unpaid_tree.get_children():
            values = self.clients_unpaid_tree.item(item, "values")
            tags = self.clients_unpaid_tree.item(item, "tags")
            items.append((values, item, tags))
        
        # Column index
        col_index = {"ClientID": 0, t("documents.col_client"): 1, "Impayé": 2}
        idx = col_index.get(column, 0)
        
        def sort_key(item):
            values = item[0]
            val = values[idx]
            if column == "ClientID":
                try:
                    return int(val)
                except:
                    return 0
            elif column == "Impayé":
                # Handle t("common.paid") text
                if val == t("common.paid"):
                    return 0.0
                try:
                    return float(val)
                except:
                    return 0.0
            else:
                return str(val).lower()
        
        items.sort(key=sort_key, reverse=new_state)
        
        for item in self.clients_unpaid_tree.get_children():
            self.clients_unpaid_tree.delete(item)
        for values, item_id, tags in items:
            self.clients_unpaid_tree.insert("", "end", values=values, tags=tags)
        
        self._apply_zebra(self.clients_unpaid_tree)

    def sort_paiements_docs(self, column):
        """Sort unpaid documents by column (ascending/descending toggle)."""
        if not hasattr(self, "client_docs_tree"):
            return
        
        # Get current sort state for this column
        if not hasattr(self, "paiements_sort_state"):
            self.paiements_sort_state = {}
        if "docs" not in self.paiements_sort_state:
            self.paiements_sort_state["docs"] = {}
        
        current_state = self.paiements_sort_state["docs"].get(column, False)
        new_state = not current_state
        self.paiements_sort_state["docs"][column] = new_state
        
        # Get all items
        items = [(self.client_docs_tree.item(item, "values"), item) for item in self.client_docs_tree.get_children()]
        
        # Column index
        col_index = {"ID": 0, t("client_dialog.col_number"): 1, t("client_dialog.col_type"): 2, t("documents.col_ttc"): 3, t("common.paid"): 4, t("paiements.col_remaining"): 5}
        idx = col_index.get(column, 0)
        
        def sort_key(item):
            values = item[0]
            val = values[idx]
            if column in ["ID", t("documents.col_ttc"), t("common.paid"), t("paiements.col_remaining")]:
                try:
                    return float(val)
                except:
                    return 0.0
            else:
                return str(val).lower()
        
        items.sort(key=sort_key, reverse=new_state)
        
        for item in self.client_docs_tree.get_children():
            self.client_docs_tree.delete(item)
        for values, item_id in items:
            self.client_docs_tree.insert("", "end", values=values)
        
        self._apply_zebra(self.client_docs_tree)

    def load_paiements(self):
        if not hasattr(self, "clients_unpaid_tree"):
            return

        for item in self.clients_unpaid_tree.get_children():
            self.clients_unpaid_tree.delete(item)
        for item in self.client_docs_tree.get_children():
            self.client_docs_tree.delete(item)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT c.id, COALESCE(c.nom,''), COALESCE(SUM(MAX(COALESCE(d.total_ttc,0) - COALESCE(d.paid_amount,0), 0)), 0) as unpaid
                FROM clients c
                LEFT JOIN documents d ON c.id = d.client_id AND d.type = 'factures'
                GROUP BY c.id
                ORDER BY unpaid DESC, c.nom
                """
            )
            for row in cursor.fetchall():
                client_id = row[0]
                name = row[1] or ""
                unpaid = as_cents(row[2])
                
                # Determine display text and tag based on unpaid amount
                if unpaid <= 0:
                    unpaid_text = t("common.paid")
                    tag = "paid"
                else:
                    unpaid_text = format_money(unpaid)
                    tag = "unpaid"
                
                self.clients_unpaid_tree.insert(
                    "", "end", values=(client_id, name, unpaid_text), tags=(tag,)
                )
        finally:
            conn.close()

        self._apply_zebra(self.clients_unpaid_tree)

    def _on_client_selected(self):
        if not hasattr(self, "clients_unpaid_tree"):
            return
        sel = self.clients_unpaid_tree.selection()
        if not sel:
            return
        vals = self.clients_unpaid_tree.item(sel[0], "values")
        if not vals:
            return
        client_id = vals[0]
        try:
            client_id = int(client_id)
        except Exception:
            return

        self._refresh_documents_tree_for_client(client_id)

    def _refresh_documents_tree_for_client(self, client_id):
        """Refresh the documents tree for a specific client ID."""
        if not hasattr(self, "client_docs_tree"):
            return

        for item in self.client_docs_tree.get_children():
            self.client_docs_tree.delete(item)

        # Configure tags for payment status
        self.client_docs_tree.tag_configure("paid", foreground="#22c55e")  # Green
        self.client_docs_tree.tag_configure("unpaid", foreground="#ef4444")  # Red

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        try:
            # Outstanding balances are tracked per invoice only.
            cur.execute(
                """
                SELECT id, numero, type, total_ttc, COALESCE(paid_amount,0) FROM documents
                WHERE client_id = ? AND type = 'factures'
                ORDER BY date DESC, id DESC
                """,
                (client_id,),
            )
            for r in cur.fetchall():
                total_ttc = as_cents(r[3])
                paid = as_cents(r[4])
                reste = max(0, total_ttc - paid)
                
                # Determine tag based on remainder
                if reste <= 0:
                    reste_text = t("common.paid")
                    tag = "paid"
                else:
                    reste_text = format_money(reste)
                    tag = "unpaid"
                
                self.client_docs_tree.insert(
                    "",
                    "end",
                    values=(
                        r[0],
                        r[1],
                        r[2],
                        format_money(total_ttc),
                        format_money(paid),
                        reste_text,
                    ),
                    tags=(tag,),
                )
        finally:
            conn.close()

        # Apply zebra striping after adding items
        try:
            for idx, item in enumerate(self.client_docs_tree.get_children()):
                # Get the existing tags
                current_tags = self.client_docs_tree.item(item, "tags")
                if not current_tags:
                    current_tags = ()
                elif isinstance(current_tags, str):
                    current_tags = (current_tags,)
                # Add zebra tag
                zebra_tag = "even" if idx % 2 == 0 else "odd"
                new_tags = (zebra_tag,) + current_tags
                self.client_docs_tree.item(item, tags=new_tags)
        except Exception:
            pass

    def search_paiements_clients(self, event=None):
        """Search unpaid clients by name or id."""
        q = ""
        try:
            q = self.paiements_client_search.get().strip()
        except Exception:
            q = ""

        # Handle the new format "ID - Nom"
        if " - " in q:
            parts = q.split(" - ")
            # Extract ID from the first part if it's a number
            id_part = parts[0].strip() if parts else ""
            nom_part = parts[1].strip() if len(parts) > 1 else ""

            # Build conditions based on available parts
            conditions = []
            params = []

            if id_part and id_part.isdigit():
                conditions.append("CAST(c.id AS TEXT) LIKE ?")
                params.append(f"%{id_part}%")
            if nom_part:
                conditions.append("c.nom LIKE ?")
                params.append(f"%{nom_part}%")

            # If no specific parts found, search with full query
            if not conditions:
                conditions = ["CAST(c.id AS TEXT) LIKE ? OR c.nom LIKE ?"]
                params = [f"%{q}%", f"%{q}%"]

            where_clause = " OR ".join(conditions)
            sql = f"""
                SELECT DISTINCT c.id, COALESCE(c.nom,''), COALESCE(SUM(MAX(COALESCE(d.total_ttc,0) - COALESCE(d.paid_amount,0), 0)), 0) as unpaid
                FROM clients c
                LEFT JOIN documents d ON c.id = d.client_id AND d.type = 'factures'
                WHERE ({where_clause})
                GROUP BY c.id
                ORDER BY unpaid DESC, c.nom
                """
        else:
            if not q:
                sql = """
                    SELECT c.id, COALESCE(c.nom,''), COALESCE(SUM(MAX(COALESCE(d.total_ttc,0) - COALESCE(d.paid_amount,0), 0)), 0) as unpaid
                    FROM clients c
                    LEFT JOIN documents d ON c.id = d.client_id AND d.type = 'factures'
                    GROUP BY c.id
                    ORDER BY unpaid DESC, c.nom
                    """
                params = []
            else:
                like_q = f"%{q}%"
                sql = """
                    SELECT c.id, COALESCE(c.nom,''), COALESCE(SUM(MAX(COALESCE(d.total_ttc,0) - COALESCE(d.paid_amount,0), 0)), 0) as unpaid
                    FROM clients c
                    LEFT JOIN documents d ON c.id = d.client_id AND d.type = 'factures'
                    WHERE (c.nom LIKE ? OR CAST(c.id AS TEXT) LIKE ?)
                    GROUP BY c.id
                    ORDER BY unpaid DESC, c.nom
                    """
                params = [like_q, like_q]

        for item in self.clients_unpaid_tree.get_children():
            self.clients_unpaid_tree.delete(item)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        try:
            cur.execute(sql, tuple(params))
            for row in cur.fetchall():
                client_id = row[0]
                name = row[1] or ""
                unpaid = as_cents(row[2])
                
                # Determine display text and tag based on unpaid amount
                if unpaid <= 0:
                    unpaid_text = t("common.paid")
                    tag = "paid"
                else:
                    unpaid_text = format_money(unpaid)
                    tag = "unpaid"
                
                self.clients_unpaid_tree.insert(
                    "", "end", values=(client_id, name, unpaid_text), tags=(tag,)
                )
        finally:
            conn.close()

        self._apply_zebra(self.clients_unpaid_tree)

    def search_paiements_docs(self, event=None):
        """Search documents for the currently selected client by number/type."""
        q = ""
        try:
            q = self.paiements_doc_search.get().strip()
        except Exception:
            q = ""

        # Handle the new format "ID - Numéro - Type"
        if " - " in q:
            parts = q.split(" - ")
            # Extract ID from the first part if it's a number
            id_part = parts[0].strip() if parts else ""
            num_part = parts[1].strip() if len(parts) > 1 else ""
            type_part = parts[2].strip() if len(parts) > 2 else ""

            # Build conditions based on available parts
            conditions = []
            params = []

            if id_part and id_part.isdigit():
                conditions.append("CAST(d.id AS TEXT) LIKE ?")
                params.append(f"%{id_part}%")
            if num_part:
                conditions.append("d.numero LIKE ?")
                params.append(f"%{num_part}%")
            if type_part:
                conditions.append("d.type LIKE ?")
                params.append(f"%{type_part}%")

            # If no specific parts found, search with full query
            if not conditions:
                conditions = [
                    "CAST(d.id AS TEXT) LIKE ? OR d.numero LIKE ? OR d.type LIKE ?"
                ]
                params = [f"%{q}%", f"%{q}%", f"%{q}%"]

            where_clause = " OR ".join(conditions)
            sql = f"""
                SELECT DISTINCT d.id, d.numero, d.type, d.total_ttc, COALESCE(d.paid_amount,0)
                FROM documents d
                WHERE d.client_id = ? AND d.type = 'factures'
                  AND (COALESCE(d.total_ttc,0) - COALESCE(d.paid_amount,0)) > 0
                  AND ({where_clause})
                ORDER BY d.date DESC, d.id DESC
                """
        else:
            if not q:
                sql = """
                    SELECT id, numero, type, total_ttc, COALESCE(paid_amount,0) FROM documents
                    WHERE client_id = ? AND type = 'factures'
                      AND (COALESCE(total_ttc,0) - COALESCE(paid_amount,0)) > 0
                    ORDER BY date DESC, id DESC
                    """
                params = []
            else:
                like_q = f"%{q}%"
                sql = """
                    SELECT id, numero, type, total_ttc, COALESCE(paid_amount,0) FROM documents
                    WHERE client_id = ? AND type = 'factures'
                      AND (COALESCE(total_ttc,0) - COALESCE(paid_amount,0)) > 0
                      AND (CAST(id AS TEXT) LIKE ? OR numero LIKE ? OR type LIKE ?)
                    ORDER BY date DESC, id DESC
                    """
                params = [like_q, like_q, like_q]

        sel = self.clients_unpaid_tree.selection()
        if not sel:
            for i in self.client_docs_tree.get_children():
                self.client_docs_tree.delete(i)
            return
        vals = self.clients_unpaid_tree.item(sel[0], "values")
        if not vals:
            return
        try:
            client_id = int(vals[0])
        except Exception:
            return

        for i in self.client_docs_tree.get_children():
            self.client_docs_tree.delete(i)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        try:
            # Build the query - remove the unpaid filter to show all documents
            if " - " in q:
                # Use the already built sql but remove the unpaid filter
                sql = sql.replace("AND (COALESCE(d.total_ttc,0) - COALESCE(d.paid_amount,0)) > 0", "")
                cur.execute(sql, (client_id,) + tuple(params))
            else:
                if not q:
                    sql = """
                        SELECT id, numero, type, total_ttc, COALESCE(paid_amount,0) FROM documents
                        WHERE client_id = ? AND type = 'factures'
                        ORDER BY date DESC, id DESC
                        """
                    cur.execute(sql, (client_id,))
                else:
                    sql = """
                        SELECT id, numero, type, total_ttc, COALESCE(paid_amount,0) FROM documents
                        WHERE client_id = ? AND type = 'factures'
                          AND (CAST(id AS TEXT) LIKE ? OR numero LIKE ? OR type LIKE ?)
                        ORDER BY date DESC, id DESC
                        """
                    cur.execute(sql, (client_id,) + tuple(params))
            
            # Configure tags for payment status
            self.client_docs_tree.tag_configure("paid", foreground="#22c55e")  # Green
            self.client_docs_tree.tag_configure("unpaid", foreground="#ef4444")  # Red
            
            for r in cur.fetchall():
                total_ttc = as_cents(r[3])
                paid = as_cents(r[4])
                reste = max(0, total_ttc - paid)
                
                # Determine tag based on remainder
                if reste <= 0:
                    reste_text = t("common.paid")
                    tag = "paid"
                else:
                    reste_text = format_money(reste)
                    tag = "unpaid"
                
                self.client_docs_tree.insert(
                    "",
                    "end",
                    values=(
                        r[0],
                        r[1],
                        r[2],
                        format_money(total_ttc),
                        format_money(paid),
                        reste_text,
                    ),
                    tags=(tag,),
                )
        finally:
            conn.close()

        # Apply zebra striping after adding items
        try:
            for idx, item in enumerate(self.client_docs_tree.get_children()):
                # Get the existing tags
                current_tags = self.client_docs_tree.item(item, "tags")
                if not current_tags:
                    current_tags = ()
                elif isinstance(current_tags, str):
                    current_tags = (current_tags,)
                # Add zebra tag
                zebra_tag = "even" if idx % 2 == 0 else "odd"
                new_tags = (zebra_tag,) + current_tags
                self.client_docs_tree.item(item, tags=new_tags)
        except Exception:
            pass

    def show_add_payment_dialog_for_selected(self):
        if not hasattr(self, "client_docs_tree"):
            messagebox.showerror(t("common.error"), t("payment_dialog.error_no_doc_selected"))
            return
        sel = self.client_docs_tree.selection()
        if not sel:
            messagebox.showerror(t("common.error"), t("documents.error_select"))
            return
        vals = self.client_docs_tree.item(sel[0], "values")
        if not vals:
            messagebox.showerror(t("common.error"), t("documents.error_select"))
            return
        try:
            doc_id = int(vals[0])
        except Exception:
            messagebox.showerror(t("common.error"), t("payment_dialog.error_invalid_doc_id"))
            return
        self.show_add_payment_dialog_for(doc_id)

    def mark_selected_document_paid_full(self):
        if not hasattr(self, "client_docs_tree"):
            messagebox.showerror(t("common.error"), t("payment_dialog.error_no_doc_selected"))
            return
        sel = self.client_docs_tree.selection()
        if not sel:
            messagebox.showerror(t("common.error"), t("documents.error_select"))
            return
        vals = self.client_docs_tree.item(sel[0], "values")
        if not vals:
            messagebox.showerror(t("common.error"), t("documents.error_select"))
            return
        try:
            doc_id = int(vals[0])
        except Exception:
            messagebox.showerror(t("common.error"), t("payment_dialog.error_invalid_doc_id"))
            return

        preserved_client_id = None
        try:
            if hasattr(self, "clients_unpaid_tree"):
                csel = self.clients_unpaid_tree.selection()
                if csel:
                    cvals = self.clients_unpaid_tree.item(csel[0], "values")
                    if cvals:
                        preserved_client_id = int(cvals[0])

        except Exception:
            preserved_client_id = None

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT total_ttc, paid_amount FROM documents WHERE id = ?", (doc_id,)
            )
            row = cursor.fetchone()
            if not row:
                messagebox.showerror(t("common.error"), t("payment_dialog.error_doc_not_found"))
                return
            total_ttc = max(0, as_cents(row[0]))
            paid = max(0, as_cents(row[1]))
            delta = max(0, total_ttc - paid)
            date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if delta > 0:
                cursor.execute(
                    "INSERT INTO payments (document_id, date, amount, method, note) VALUES (?, ?, ?, ?, ?)",
                    (doc_id, date_now, delta, "full", "Paiement total"),
                )
            cursor.execute(
                "UPDATE documents SET paid_amount = ?, payment_mode = ? WHERE id = ?",
                (total_ttc, "full", doc_id),
            )
            conn.commit()
        finally:
            conn.close()

        self.load_paiements()

        # Refresh documents tree for the preserved client
        if preserved_client_id is not None and hasattr(self, "clients_unpaid_tree") and hasattr(self, "client_docs_tree"):
            # First try to select the client in the tree
            client_found = False
            for iid in self.clients_unpaid_tree.get_children():
                try:
                    vals = self.clients_unpaid_tree.item(iid, "values")
                except Exception:
                    continue
                if vals and int(vals[0]) == preserved_client_id:
                    self.clients_unpaid_tree.selection_set(iid)
                    try:
                        self.clients_unpaid_tree.focus(iid)
                    except Exception:
                        pass
                    try:
                        self._on_client_selected()
                    except Exception:
                        pass
                    client_found = True
                    break
            
            # If client not found in tree (no more unpaid docs), manually refresh documents tree
            if not client_found:
                self._refresh_documents_tree_for_client(preserved_client_id)

        for t in ("devis", "bl", "factures"):
            try:
                self.load_documents(t)
            except Exception:
                pass

    def _get_selected_invoice_id(self):
        if hasattr(self, "paiements_tree"):
            sel = self.paiements_tree.selection()
            if not sel:
                return None
            vals = self.paiements_tree.item(sel, "values")
            if not vals:
                return None
            return int(vals[0])

        if hasattr(self, "client_docs_tree"):
            sel = self.client_docs_tree.selection()
            if not sel:
                return None
            vals = self.client_docs_tree.item(sel, "values")
            if not vals:
                return None
            return int(vals[0])
        return None

    def mark_invoice_paid_full(self):
        doc_id = self._get_selected_invoice_id()
        if not doc_id:
            messagebox.showerror(t("common.error"), t("documents.error_select"))
            return

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT total_ttc, paid_amount FROM documents WHERE id = ?", (doc_id,)
            )
            row = cursor.fetchone()
            if not row:
                return
            total_ttc = max(0, as_cents(row[0]))
            paid = max(0, as_cents(row[1]))
            delta = max(0, total_ttc - paid)

            date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if delta > 0:
                cursor.execute(
                    "INSERT INTO payments (document_id, date, amount, method, note) VALUES (?, ?, ?, ?, ?)",
                    (doc_id, date_now, delta, "full", "Paiement total"),
                )

            cursor.execute(
                "UPDATE documents SET paid_amount = ?, payment_mode = ? WHERE id = ?",
                (total_ttc, "full", doc_id),
            )
            conn.commit()
        finally:
            conn.close()

        self.load_paiements()
        for t in ("devis", "bl", "factures"):
            try:
                self.load_documents(t)
            except Exception:
                pass

    def show_add_payment_dialog(self):
        invoice_id = self._get_selected_invoice_id()
        if not invoice_id:
            messagebox.showerror(t("common.error"), t("payment_dialog.error_select_invoice"))
            return
        self.show_add_payment_dialog_for(invoice_id)

    def show_add_payment_dialog_for(self, document_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT type FROM documents WHERE id = ?", (document_id,))
            row = cursor.fetchone()
        finally:
            conn.close()

        if not row:
            messagebox.showerror(
                t("common.error"), t("payment_dialog.error_doc_not_found")
            )
            return
        if row[0] != "factures":
            messagebox.showerror(
                t("common.error"), t("payment_dialog.error_not_invoice")
            )
            return

        dialog = ctk.CTkToplevel(self)
        dialog.title(t("paiements.button_record_payment"))
        dialog.geometry("420x280")
        try:
            dialog.transient(self)
            dialog.grab_set()
        except Exception as e:
            print(f"Warning: could not set modal grab for payment dialog: {e}")

        ctk.CTkLabel(
            dialog, text=f"Document ID: {document_id}", font=("Arial", 14, "bold")
        ).pack(pady=(15, 5))
        ctk.CTkLabel(dialog, text=t("new_doc.discount_amount_label")).pack(pady=(10, 0))
        amount_entry = ctk.CTkEntry(dialog, width=220)
        amount_entry.pack(pady=(0, 10))

        ctk.CTkLabel(dialog, text=t("payment_dialog.label_method")).pack(pady=(5, 0))
        method_entry = ctk.CTkEntry(dialog, width=220)
        method_entry.pack(pady=(0, 10))

        def save_payment():
            method = method_entry.get().strip() or None

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            try:
                error_key, error_kwargs = _apply_payment(
                    cursor, document_id, amount_entry.get().strip(), method
                )
                if error_key:
                    conn.rollback()
                    messagebox.showerror(
                        t("common.error"), t(error_key, **error_kwargs)
                    )
                    return
                conn.commit()
            except Exception as e:
                conn.rollback()
                messagebox.showerror(
                    t("common.error"), t("documents.error_save", error=str(e))
                )
                return
            finally:
                conn.close()

            dialog.destroy()
            self.load_paiements()

            # Refresh documents tree if a client is selected
            if hasattr(self, "clients_unpaid_tree") and hasattr(self, "client_docs_tree"):
                csel = self.clients_unpaid_tree.selection()
                if csel:
                    try:
                        self._on_client_selected()
                    except Exception:
                        pass

            for t in ("devis", "bl", "factures"):
                try:
                    self.load_documents(t)
                except Exception:
                    pass

        btns = ctk.CTkFrame(dialog, fg_color="transparent")
        btns.pack(fill="x", padx=20, pady=10)
        ctk.CTkButton(
            btns,
            text=t("common.cancel"),
            command=dialog.destroy,
            fg_color="gray",
            hover_color="darkgray",
        ).pack(side="right", padx=10)
        ctk.CTkButton(btns, text=t("common.save"), command=save_payment).pack(side="right")

        amount_entry.focus_set()

    def create_export_frame(self):
        frame = ctk.CTkFrame(self.main_frame, corner_radius=0, fg_color="transparent")

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 20))
        ctk.CTkLabel(header, text=t("export.header_title"), font=self.ui["font_h1"]).pack(
            side="left"
        )

        ctk.CTkLabel(
            frame,
            text=t("export.instruction"),
            font=("Arial", 12),
        ).pack(pady=10)

        self.frames["export"] = frame

    def show_export_dialog_for_document(self, document_id):
        dialog = ctk.CTkToplevel(self)
        dialog.title(t("export.dialog_title"))
        dialog.geometry("420x260")
        try:
            dialog.transient(self)
            dialog.grab_set()
        except Exception as e:
            print(f"Warning: could not set modal grab for export dialog: {e}")

        ctk.CTkLabel(
            dialog, text=f"Document #{document_id}", font=("Arial", 14, "bold")
        ).pack(pady=(20, 10))

        def export_html():
            path = filedialog.asksaveasfilename(
                defaultextension=".html",
                filetypes=[("HTML", "*.html")],
                initialfile=f"document_{document_id}.html",
            )
            if not path:
                return
            self.export_document_html(document_id, path)
            messagebox.showinfo("Export", f"Export HTML terminÃ©:\n{path}")

        def print_direct():
            tmp = os.path.join(tempfile.gettempdir(), f"document_{document_id}.html")
            self.export_document_html(document_id, tmp)
            try:
                os.startfile(tmp, "print")
            except Exception:
                os.startfile(tmp)

        def export_pdf():
            if canvas is None or A4 is None:
                messagebox.showerror(
                    "PDF", "ReportLab n'est pas installÃ©. Installe les dÃ©pendances."
                )
                return
            path = filedialog.asksaveasfilename(
                defaultextension=".pdf",
                filetypes=[("PDF", "*.pdf")],
                initialfile=f"document_{document_id}.pdf",
            )
            if not path:
                return
            self.export_document_pdf(document_id, path)
            messagebox.showinfo("Export", f"Export PDF terminÃ©:\n{path}")

        def print_pdf_windows():
            if canvas is None or A4 is None:
                messagebox.showerror(
                    "PDF", "ReportLab n'est pas installÃ©. Installe les dÃ©pendances."
                )
                return
            tmp = os.path.join(tempfile.gettempdir(), f"document_{document_id}.pdf")
            self.export_document_pdf(document_id, tmp)
            try:
                os.startfile(tmp, "print")
            except Exception:
                os.startfile(tmp)

        ctk.CTkButton(dialog, text=t("export.button_html"), command=export_html).pack(pady=6)
        ctk.CTkButton(
            dialog, text=t("export.button_print_html"), command=print_direct
        ).pack(pady=6)
        ctk.CTkButton(dialog, text=t("export.button_pdf"), command=export_pdf).pack(pady=6)
        ctk.CTkButton(
            dialog, text=t("export.button_print_pdf"), command=print_pdf_windows
        ).pack(pady=6)
        ctk.CTkButton(
            dialog,
            text=t("common.close"),
            command=dialog.destroy,
            fg_color="gray",
            hover_color="darkgray",
        ).pack(pady=8)

    def show_new_doc_export_dialog(self, doc_id, doc_type):
        """Show export dialog after creating a new document."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Document enregistrÃ©")
        dialog.geometry("420x260")
        try:
            dialog.transient(self)
            dialog.grab_set()
        except Exception as e:
            print(f"Warning: could not set modal grab for new doc export dialog: {e}")

        ctk.CTkLabel(
            dialog, text=f"Document #{doc_id} enregistrÃ©", font=("Arial", 14, "bold")
        ).pack(pady=(20, 10))
        
        ctk.CTkLabel(
            dialog, text=t("export.new_doc_question"), font=("Arial", 12)
        ).pack(pady=(0, 15))

        def export_html():
            path = filedialog.asksaveasfilename(
                defaultextension=".html",
                filetypes=[("HTML", "*.html")],
                initialfile=f"document_{doc_id}.html",
            )
            if not path:
                return
            self.export_document_html(doc_id, path)
            messagebox.showinfo("Export", f"Export HTML terminÃ©:\n{path}")

        def print_direct():
            tmp = os.path.join(tempfile.gettempdir(), f"document_{doc_id}.html")
            self.export_document_html(doc_id, tmp)
            try:
                os.startfile(tmp, "print")
            except Exception:
                os.startfile(tmp)

        def export_pdf():
            if canvas is None or A4 is None:
                messagebox.showerror(
                    "PDF", "ReportLab n'est pas installÃ©. Installe les dÃ©pendances."
                )
                return
            path = filedialog.asksaveasfilename(
                defaultextension=".pdf",
                filetypes=[("PDF", "*.pdf")],
                initialfile=f"document_{doc_id}.pdf",
            )
            if not path:
                return
            self.export_document_pdf(doc_id, path)
            messagebox.showinfo("Export", f"Export PDF terminÃ©:\n{path}")

        def print_pdf_windows():
            if canvas is None or A4 is None:
                messagebox.showerror(
                    "PDF", "ReportLab n'est pas installÃ©. Installe les dÃ©pendances."
                )
                return
            tmp = os.path.join(tempfile.gettempdir(), f"document_{doc_id}.pdf")
            self.export_document_pdf(doc_id, tmp)
            try:
                os.startfile(tmp, "print")
            except Exception:
                os.startfile(tmp)

        ctk.CTkButton(dialog, text=t("export.button_html"), command=export_html).pack(pady=6)
        ctk.CTkButton(
            dialog, text=t("export.button_print_html"), command=print_direct
        ).pack(pady=6)
        ctk.CTkButton(dialog, text=t("export.button_pdf"), command=export_pdf).pack(pady=6)
        ctk.CTkButton(
            dialog, text=t("export.button_print_pdf"), command=print_pdf_windows
        ).pack(pady=6)
        ctk.CTkButton(
            dialog,
            text=t("common.close"),
            command=dialog.destroy,
            fg_color="gray",
            hover_color="darkgray",
        ).pack(pady=8)

    def export_document_html(self, document_id, file_path):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT d.type, d.numero, d.date, d.statut,
                       d.total_ht, d.total_tva, d.total_ttc,
                       d.paid_amount, d.payment_mode,
                       COALESCE(c.nom, ''), COALESCE(c.adresse, ''), COALESCE(c.telephone, ''), COALESCE(c.email, ''), COALESCE(c.matricule_fiscale, ''),
                       COALESCE(d.discount_enabled, 0), COALESCE(d.discount_value, 0)
                FROM documents d
                LEFT JOIN clients c ON c.id = d.client_id
                WHERE d.id = ?
                """,
                (document_id,),
            )
            doc = cursor.fetchone()
            if not doc:
                raise ValueError(t("payment_dialog.error_doc_not_found"))

            cursor.execute(
                """
                SELECT dl.article_id, a.reference, dl.designation, dl.quantite, dl.prix_unitaire, dl.tva, dl.total_ht, dl.total_tva, dl.total_ttc
                FROM document_lignes dl
                LEFT JOIN articles a ON a.id = dl.article_id
                WHERE dl.document_id = ?
                """,
                (document_id,),
            )
            lines = cursor.fetchall()
        finally:
            conn.close()

        (
            doc_type,
            numero,
            date_str,
            statut,
            total_ht,
            total_tva,
            total_ttc,
            paid_amount,
            payment_mode,
            client_nom,
            client_adresse,
            client_tel,
            client_email,
            client_matricule,
            discount_enabled,
            discount_value,
        ) = doc
        title = {
            "devis": t("nav.devis"),
            "bl": t("nav.bl"),
            "factures": t("doc_type.factures"),
            "reception": t("nav.reception"),
        }.get(doc_type, doc_type)

        # Determine if we should show reference column (only for devis)
        show_ref = doc_type == "devis"
        
        if show_ref:
            rows_html = "".join(
                f"<tr><td>{ref if ref else ''}</td><td>{d}</td><td style='text-align:right'>{q}</td><td style='text-align:right'>{format_money(pu)}</td><td style='text-align:right'>{float(tva or 0):.1f}%</td><td style='text-align:right'>{format_money(ht)}</td><td style='text-align:right'>{format_money(ttc)}</td></tr>"
                for (article_id, ref, d, q, pu, tva, ht, tv, ttc) in lines
            )
            colspan = "7"
            ref_col = "<th>Référence</th>"
        else:
            rows_html = "".join(
                f"<tr><td>{d}</td><td style='text-align:right'>{q}</td><td style='text-align:right'>{format_money(pu)}</td><td style='text-align:right'>{float(tva or 0):.1f}%</td><td style='text-align:right'>{format_money(ht)}</td><td style='text-align:right'>{format_money(ttc)}</td></tr>"
                for (article_id, ref, d, q, pu, tva, ht, tv, ttc) in lines
            )
            colspan = "6"
            ref_col = ""

        html = f"""<!doctype html>
<html lang='fr'>
<head>
  <meta charset='utf-8'>
  <title>{title} {numero}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; }}
    .header {{ display:flex; justify-content:space-between; align-items:flex-start; }}
    .box {{ border: 1px solid #ddd; padding: 12px; border-radius: 8px; }}
    table {{ width:100%; border-collapse: collapse; margin-top: 16px; border: 2px solid #333; }}
    th, td {{ border: 1px solid #333; padding: 8px; }}
    th {{ background:#333; color:#ffffff; font-weight:bold; border: 1px solid #333; }}
    td {{ border: 1px solid #333; }}
    .total {{ text-align:right; font-size: 18px; font-weight: bold; margin-top: 12px; }}
    .muted {{ color:#666; font-size: 12px; }}
    .user-info {{ background:#f0f4f8; padding: 8px; border-radius: 4px; margin-top: 8px; font-size: 11px; }}
    @media print {{ .no-print {{ display:none; }} }}
  </style>
</head>
<body>
  <div class='header'>
    <div>
      <h1 style='margin:0'>{title}</h1>
      <div class='muted'>Numéro: <b>{numero}</b></div>
      <div class='muted'>Date: {date_str}</div>
      <div class='muted'>Statut: {statut}</div>
      <div class='user-info'>Document généré par: <b>{self.user["username"]}</b> le {datetime.now().strftime("%d/%m/%Y à %H:%M")}</div>
    </div>
    <div class='box' style='min-width:320px'>
      <div><b>Client</b> : {client_nom}</div>
      <div class='muted'><b>Code TVA</b> : {client_matricule}</div>
      <div class='muted'><b>Adresse</b> : {client_adresse}</div>
      <div class='muted'><b>Telephone</b> : {client_tel}</div>
      <div class='muted'><b>Email</b> : {client_email}</div>
    </div>
  </div>

  <table>
    <thead>
      <tr>
        {ref_col}
        <th>Désignation</th>
        <th style='text-align:right'>Qté</th>
        <th style='text-align:right'>PU</th>
        <th style='text-align:right'>TVA%</th>
        <th style='text-align:right'>HT</th>
        <th style='text-align:right'>TTC</th>
      </tr>
    </thead>
    <tbody>
      {rows_html if rows_html else f"<tr><td colspan='{colspan}' style='text-align:center;color:#666'>Aucune ligne</td></tr>"}
    </tbody>
  </table>

  <div style='display: flex; justify-content: space-between; margin-top: 16px; gap: 40px;'>
    <div style='flex: 1;'>
      {f"<div class='total'>Remise: {float(discount_value):.1f}% = -{format_money(discount_cents(total_ht, discount_value))}</div>" if discount_enabled and float(discount_value or 0) > 0 else ""}
    </div>
    <div style='flex: 1; text-align: right;'>
      <div class='total'>HT: {format_money(total_ht)}</div>
      <div class='total'>TVA: {format_money(total_tva)}</div>
      <div class='total'>TTC: {format_money(total_ttc)}</div>
    </div>
  </div>

  <div class='no-print' style='margin-top:16px'>
    <button onclick='window.print()'>Imprimer</button>
  </div>
</body>
</html>"""

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(html)

    def export_document_pdf(self, document_id, file_path):
        if (
            (SimpleDocTemplate is None)
            or (A4 is None)
            or (Table is None)
            or (Paragraph is None)
        ):
            raise RuntimeError("ReportLab non disponible")

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT d.type, d.numero, d.date, d.statut,
                       d.total_ht, d.total_tva, d.total_ttc,
                       d.paid_amount, d.payment_mode,
                       COALESCE(c.nom, ''), COALESCE(c.adresse, ''), COALESCE(c.telephone, ''), COALESCE(c.email, ''), COALESCE(c.matricule_fiscale, ''),
                       COALESCE(d.discount_enabled, 0), COALESCE(d.discount_value, 0)
                FROM documents d
                LEFT JOIN clients c ON c.id = d.client_id
                WHERE d.id = ?
                """,
                (document_id,),
            )
            doc = cursor.fetchone()
            if not doc:
                raise ValueError(t("payment_dialog.error_doc_not_found"))

            cursor.execute(
                """
                SELECT dl.article_id, a.reference, dl.designation, dl.quantite, dl.prix_unitaire, dl.tva, dl.total_ht, dl.total_tva, dl.total_ttc
                FROM document_lignes dl
                LEFT JOIN articles a ON a.id = dl.article_id
                WHERE dl.document_id = ?
                """,
                (document_id,),
            )
            lines = cursor.fetchall()
        finally:
            conn.close()

        (
            doc_type,
            numero,
            date_str,
            statut,
            total_ht,
            total_tva,
            total_ttc,
            paid_amount,
            payment_mode,
            client_nom,
            client_adresse,
            client_tel,
            client_email,
            client_matricule,
            discount_enabled,
            discount_value,
        ) = doc

        title = {
            "devis": t("nav.devis"),
            "bl": t("nav.bl"),
            "factures": t("doc_type.factures"),
            "reception": t("nav.reception"),
        }.get(doc_type, doc_type)

        doc = SimpleDocTemplate(
            file_path,
            pagesize=A4,
            leftMargin=18 * mm,
            rightMargin=18 * mm,
            topMargin=16 * mm,
            bottomMargin=16 * mm,
            title=f"{title} {numero}",
        )

        styles = getSampleStyleSheet()
        h1 = ParagraphStyle(
            name="H1",
            parent=styles["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=18,
            spaceAfter=10,
        )
        small = ParagraphStyle(
            name="Small",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=9,
            leading=11,
        )
        normal = ParagraphStyle(
            name="NormalGC",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=10,
            leading=12,
        )
        bold = ParagraphStyle(
            name="BoldGC",
            parent=normal,
            fontName="Helvetica-Bold",
        )

        story = []

        story.append(
            Paragraph(
                t("app.title"),
                ParagraphStyle(name="Brand", parent=bold, fontSize=12, spaceAfter=2),
            )
        )
        story.append(Paragraph(f"{title} <b>{numero}</b>", h1))

        meta_left = f"<b>Date</b> : {date_str}<br/><b>Statut</b> : {statut}"
        meta_right = f"<b>Client</b> : {client_nom or ''}"
        if client_matricule:
            meta_right += f"<br/><b>Code TVA</b> : {client_matricule}"
        if client_adresse:
            meta_right += f"<br/><b>Adresse</b> : {client_adresse}"
        if client_tel:
            meta_right += f"<br/><b>Telephone</b> : {client_tel}"
        if client_email:
            meta_right += f"<br/><b>Email</b> : {client_email}"

        meta_table = Table(
            [[Paragraph(meta_left, normal), Paragraph(meta_right, normal)]],
            colWidths=[88 * mm, 88 * mm],
        )
        meta_table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BOX", (0, 0), (-1, -1), 1.0, colors.black),
                    ("INNERGRID", (0, 0), (-1, -1), 0.6, colors.black),
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FAFBFD")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.append(meta_table)

        # Add user info
        user_info = Paragraph(
            f"<b>Document généré par:</b> {self.user['username']} le {datetime.now().strftime('%d/%m/%Y à %H:%M')}",
            small,
        )
        story.append(Spacer(1, 6))
        story.append(user_info)
        story.append(Spacer(1, 10))

        # Determine if we should show reference column (only for devis)
        show_ref = doc_type == "devis"
        
        if show_ref:
            table_header = [
                Paragraph(t("articles.col_reference"), bold),
                Paragraph(t("articles.col_designation"), bold),
                Paragraph(t("article_dialog.col_qty"), bold),
                Paragraph(t("new_doc.col_pu"), bold),
                Paragraph(t("articles.col_tva"), bold),
                Paragraph(t("documents.col_ht"), bold),
                Paragraph(t("documents.col_ttc"), bold),
            ]
            col_widths = [
                25 * mm,
                40 * mm,
                14 * mm,
                18 * mm,
                14 * mm,
                18 * mm,
                18 * mm,
            ]
            empty_row = [Paragraph(t("export.html_no_lines"), normal), "", "", "", "", "", ""]
        else:
            table_header = [
                Paragraph(t("articles.col_designation"), bold),
                Paragraph(t("article_dialog.col_qty"), bold),
                Paragraph(t("new_doc.col_pu"), bold),
                Paragraph(t("articles.col_tva"), bold),
                Paragraph(t("documents.col_ht"), bold),
                Paragraph(t("documents.col_ttc"), bold),
            ]
            col_widths = [
                65 * mm,
                14 * mm,
                18 * mm,
                14 * mm,
                18 * mm,
                18 * mm,
            ]
            empty_row = [Paragraph(t("export.html_no_lines"), normal), "", "", "", "", ""]
        
        table_rows = [table_header]

        for article_id, ref, des, qty, pu, tva, tht, ttva, tttc in lines:
            des_txt = str(des or "")
            if show_ref:
                ref_txt = str(ref or "")
                table_rows.append(
                    [
                        Paragraph(ref_txt, normal),
                        Paragraph(des_txt.replace("\n", "<br/>") or "-", normal),
                        Paragraph(f"{float(qty or 0):.2f}", normal),
                        Paragraph(format_money(pu), normal),
                        Paragraph(f"{float(tva or 0):.1f}%", normal),
                        Paragraph(format_money(tht), normal),
                        Paragraph(format_money(tttc), normal),
                    ]
                )
            else:
                table_rows.append(
                    [
                        Paragraph(des_txt.replace("\n", "<br/>") or "-", normal),
                        Paragraph(f"{float(qty or 0):.2f}", normal),
                        Paragraph(format_money(pu), normal),
                        Paragraph(f"{float(tva or 0):.1f}%", normal),
                        Paragraph(format_money(tht), normal),
                        Paragraph(format_money(tttc), normal),
                    ]
                )

        if len(table_rows) == 1:
            table_rows.append(empty_row)

        lines_table = Table(
            table_rows,
            colWidths=col_widths,
            repeatRows=1,
        )
        lines_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E0E0E0")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, 0), 9),
                    ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                    ("ALIGN", (0, 0), (0, -1), "LEFT"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("GRID", (0, 0), (-1, -1), 0.8, colors.black),
                    ("BOX", (0, 0), (-1, -1), 1.2, colors.black),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.HexColor("#F6F8FB")],
                    ),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.append(lines_table)
        story.append(Spacer(1, 10))

        # Create totals table (right side)
        totals_rows = [
            [
                Paragraph("Total HT", bold),
                Paragraph(format_money(total_ht), bold),
            ],
            [
                Paragraph("Total TVA", bold),
                Paragraph(format_money(total_tva), bold),
            ],
            [
                Paragraph(t("client_dialog.col_ttc"), bold),
                Paragraph(format_money(total_ttc), bold),
            ],
        ]

        totals_table = Table(totals_rows, colWidths=[35 * mm, 35 * mm])
        totals_table.setStyle(
            TableStyle(
                [
                    ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("BOX", (0, 0), (-1, -1), 1.0, colors.black),
                    ("INNERGRID", (0, 0), (-1, -1), 0.6, colors.black),
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FAFBFD")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )

        # Create discount table (left side) if discount is enabled and has a value
        if discount_enabled and float(discount_value or 0) > 0:
            discount_amount = discount_cents(total_ht, discount_value)
            discount_rows = [
                [
                    Paragraph(t("new_doc.discount_label"), bold),
                    Paragraph(f"{float(discount_value):.1f}%", bold),
                ],
                [
                    Paragraph(t("new_doc.discount_amount_label"), bold),
                    Paragraph(f"-{format_money(discount_amount)}", bold),
                ],
            ]
            discount_table = Table(discount_rows, colWidths=[35 * mm, 35 * mm])
            discount_table.setStyle(
                TableStyle(
                    [
                        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("BOX", (0, 0), (-1, -1), 1.0, colors.black),
                        ("INNERGRID", (0, 0), (-1, -1), 0.6, colors.black),
                        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FAFBFD")),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )
            wrapper = Table([[discount_table, "", totals_table]], colWidths=[70 * mm, 10 * mm, 70 * mm])
        else:
            wrapper = Table([["", "", totals_table]], colWidths=[70 * mm, 10 * mm, 70 * mm])
        
        wrapper.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story.append(wrapper)
        story.append(Spacer(1, 8))
        story.append(Paragraph(t("export.pdf_thank_you"), small))

        def on_page(c, _doc):
            c.setFont("Helvetica", 8)
            c.setFillColor(colors.HexColor("#6B7280"))
            c.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {_doc.page}")

        doc.build(story, onFirstPage=on_page, onLaterPages=on_page)

    def create_dashboard_frame(self):
        frame = ctk.CTkFrame(self.main_frame, corner_radius=0, fg_color="transparent")

        title = ctk.CTkLabel(frame, text=t("dashboard.header"), font=("Arial", 24, "bold"))
        title.pack(pady=20)

        cards_frame = ctk.CTkFrame(frame, fg_color="transparent")
        cards_frame.pack(fill="x", pady=10)

        def create_card(parent, title, value, icon=None):
            card = ctk.CTkFrame(parent, height=120, corner_radius=10)

            if icon:
                icon_label = ctk.CTkLabel(card, text=icon, font=("Arial", 24))
                icon_label.pack(pady=(15, 5))

            title_label = ctk.CTkLabel(card, text=title, font=("Arial", 14))
            title_label.pack(pady=(5, 0))

            value_label = ctk.CTkLabel(card, text=value, font=("Arial", 24, "bold"))
            value_label.pack(pady=5)

            return card

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM products")
        product_count = cursor.fetchone()[0]

        cursor.execute("""
            SELECT COUNT(*), COALESCE(SUM(total_price), 0)
            FROM sales
            WHERE date(sale_date) = date('now')
        """)
        today_sales = cursor.fetchone()

        cursor.execute("""
            SELECT COUNT(*), COALESCE(SUM(total_price), 0)
            FROM sales
            WHERE strftime('%Y-%m', sale_date) = strftime('%Y-%m', 'now')
        """)
        month_sales = cursor.fetchone()

        conn.close()

        card1 = create_card(cards_frame, "Produits en stock", str(product_count), "📦")
        card1.pack(side="left", fill="both", expand=True, padx=10)

        card2 = create_card(
            cards_frame,
            "Ventes du jour",
            f"{today_sales[0]}\n{format_money(today_sales[1])} €",
            "💰",
        )
        card2.pack(side="left", fill="both", expand=True, padx=10)

        card3 = create_card(
            cards_frame,
            "Ventes du mois",
            f"{month_sales[0]}\n{format_money(month_sales[1])} €",
            "📊",
        )
        card3.pack(side="left", fill="both", expand=True, padx=10)

        sales_frame = ctk.CTkFrame(frame, corner_radius=10)
        sales_frame.pack(fill="both", expand=True, pady=20, padx=10)

        ctk.CTkLabel(
            sales_frame, text="Dernières ventes", font=("Arial", 16, "bold")
        ).pack(pady=10)

        columns = ("ID", "Produit", t("new_doc.col_qty"), "Prix total", t("article_dialog.col_date"))
        self.sales_tree = ttk.Treeview(
            sales_frame, columns=columns, show="headings", selectmode="browse"
        )

        for col in columns:
            self.sales_tree.heading(col, text=col)
            self.sales_tree.column(col, width=100, anchor="center")

        style = ttk.Style()
        style.theme_use("default")
        style.configure(
            "Treeview",
            background="#f5f5f5",
            foreground="black",
            rowheight=25,
            fieldbackground="#f5f5f5",
            borderwidth=0,
        )
        style.map("Treeview", background=[("selected", "#0078d7")])

        scrollbar = ttk.Scrollbar(
            sales_frame, orient="vertical", command=self.sales_tree.yview
        )
        scrollbar.pack(side="right", fill="y")
        self.sales_tree.configure(yscrollcommand=scrollbar.set)

        self.sales_tree.pack(fill="both", expand=True, padx=10, pady=10)

        self.load_recent_sales()

        self.frames["dashboard"] = frame

    def load_recent_sales(self, limit=10):
        for item in self.sales_tree.get_children():
            self.sales_tree.delete(item)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT s.id, p.name, s.quantity, s.total_price, s.sale_date
            FROM sales s
            JOIN products p ON s.product_id = p.id
            ORDER BY s.sale_date DESC
            LIMIT ?
        """,
            (limit,),
        )

        for row in cursor.fetchall():
            sale_date = datetime.strptime(row[4], "%Y-%m-%d %H:%M:%S")
            formatted_date = sale_date.strftime("%d/%m/%Y %H:%M")

            self.sales_tree.insert(
                "",
                "end",
                values=(
                    row[0],
                    row[1],
                    row[2],
                    f"{format_money(row[3])} €",
                    formatted_date,
                ),
            )

        conn.close()

    def create_sales_frame(self):
        """Créer le cadre pour la gestion des ventes"""
        frame = ctk.CTkFrame(self.main_frame)

        title = ctk.CTkLabel(
            frame, text=t("sales.header"), font=("Arial", 20, "bold")
        )
        title.pack(pady=20)

        toolbar = ctk.CTkFrame(frame, fg_color="transparent")
        toolbar.pack(fill="x", padx=10, pady=5)

        new_sale_btn = ctk.CTkButton(
            toolbar, text="+ Nouvelle Vente", command=self.show_new_sale_dialog
        )
        new_sale_btn.pack(side="left", padx=5)

        search_frame = ctk.CTkFrame(toolbar, fg_color="transparent")
        search_frame.pack(side="right")

        self.sale_search_entry = ctk.CTkEntry(
            search_frame, placeholder_text="Rechercher une vente...", width=250
        )
        self.sale_search_entry.pack(side="left", padx=5)
        self.sale_search_entry.bind("<Return>", self.search_sales)

        search_btn = ctk.CTkButton(
            search_frame, text=t("common.search"), command=self.search_sales
        )
        search_btn.pack(side="left")

        columns = ("id", "date", "client", "total", "statut", "actions")
        self.sales_tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse"
        )

        self.sales_tree.heading("id", text="ID")
        self.sales_tree.heading("date", text=t("article_dialog.col_date"))
        self.sales_tree.heading("client", text=t("documents.col_client"))
        self.sales_tree.heading("total", text="Total")
        self.sales_tree.heading("statut", text=t("client_dialog.col_status"))
        self.sales_tree.heading("actions", text="Actions")

        self.sales_tree.column("id", width=50, anchor="center")
        self.sales_tree.column("date", width=150, anchor="center")
        self.sales_tree.column("client", width=200, anchor="w")
        self.sales_tree.column("total", width=100, anchor="e")
        self.sales_tree.column("statut", width=100, anchor="center")
        self.sales_tree.column("actions", width=150, anchor="center")

        scrollbar = ttk.Scrollbar(
            frame, orient="vertical", command=self.sales_tree.yview
        )
        scrollbar.pack(side="right", fill="y")
        self.sales_tree.configure(yscrollcommand=scrollbar.set)

        self.sales_tree.pack(fill="both", expand=True, padx=10, pady=10)

        self.load_sales()

        self.sales_tree.bind("<Double-1>", self.on_sale_double_click)

        self.frames["sales"] = frame

    def create_products_frame(self):
        frame = ctk.CTkFrame(self.main_frame, corner_radius=0, fg_color="transparent")

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 20))

        title = ctk.CTkLabel(
            header, text=t("products.header"), font=("Arial", 24, "bold")
        )
        title.pack(side="left")

        add_button = ctk.CTkButton(
            header, text="+ Ajouter un produit", command=self.show_add_product_dialog
        )
        add_button.pack(side="right")

        search_frame = ctk.CTkFrame(frame, fg_color="transparent")
        search_frame.pack(fill="x", pady=(0, 20))

        self.search_entry = ctk.CTkEntry(
            search_frame, placeholder_text="Rechercher un produit...", width=400
        )
        self.search_entry.pack(side="left", padx=(0, 10))

        search_button = ctk.CTkButton(
            search_frame, text=t("common.search"), command=self.search_products
        )
        search_button.pack(side="left")

        columns = ("ID", t("clients.col_name"), "Description", "Prix", t("articles.col_stock"), "Actions")
        self.products_tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse"
        )

        col_widths = [50, 150, 250, 80, 80, 150]
        for idx, col in enumerate(columns):
            self.products_tree.heading(col, text=col)
            self.products_tree.column(col, width=col_widths[idx], anchor="center")

        style = ttk.Style()
        style.theme_use("default")
        style.configure(
            "Treeview",
            background="#f5f5f5",
            foreground="black",
            rowheight=25,
            fieldbackground="#f5f5f5",
            borderwidth=0,
        )
        style.map("Treeview", background=[("selected", "#0078d7")])

        scrollbar = ttk.Scrollbar(
            frame, orient="vertical", command=self.products_tree.yview
        )
        scrollbar.pack(side="right", fill="y")
        self.products_tree.configure(yscrollcommand=scrollbar.set)

        self.products_tree.pack(fill="both", expand=True)

        self.load_products()

        self.products_tree.bind("<Double-1>", self.on_product_double_click)

        self.frames["products"] = frame

    def load_products(self, search_query=None):
        for item in self.products_tree.get_children():
            self.products_tree.delete(item)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        if search_query:
            cursor.execute(
                """
                SELECT * FROM products
                WHERE name LIKE ? OR description LIKE ?
                ORDER BY name
            """,
                (f"%{search_query}%", f"%{search_query}%"),
            )
        else:
            cursor.execute("SELECT * FROM products ORDER BY name")

        products = cursor.fetchall()
        conn.close()

        for product in products:
            product_id = product[0]
            name = product[1]
            description = product[2] if product[2] else ""
            price = (
                f"{format_money(product[3])} €"
                if product[3] is not None
                else "0.00 €"
            )
            stock = product[4]

            self.products_tree.insert(
                "",
                "end",
                values=(
                    product_id,
                    name,
                    description,
                    price,
                    stock,
                    "Modifier | Supprimer",
                ),
            )

    def show_add_product_dialog(self):
        """Afficher la boîte de dialogue d'ajout de produit"""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Ajouter un produit")
        dialog.geometry("400x350")
        try:
            dialog.transient(self)
            dialog.grab_set()
        except Exception as e:
            print(f"Warning: could not set modal grab for add product dialog: {e}")

        def save_product():
            name = name_entry.get()
            description = desc_entry.get("1.0", "end-1c")
            price = price_entry.get()
            stock = stock_entry.get()

            if not all([name, price, stock]):
                messagebox.showerror(
                    t("common.error"), t("validation.fill_required")
                )
                return

            try:
                price = _validate_unit_price(price)
                stock = int(stock)
                if price is None:
                    raise ValueError("invalid price")

                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO products (name, description, price, stock)
                    VALUES (?, ?, ?, ?)
                    """,
                    (name, description, price, stock),
                )
                conn.commit()
                conn.close()

                messagebox.showinfo(t("register.success_title"), "Produit ajouté avec succès!")
                self.load_products()
                dialog.destroy()

            except ValueError:
                messagebox.showerror(
                    t("common.error"),
                    t("validation.price_stock_numeric"),
                )

        ctk.CTkLabel(dialog, text="Nom du produit*").pack(pady=(10, 0))
        name_entry = ctk.CTkEntry(dialog, width=300)
        name_entry.pack(pady=(0, 10))

        ctk.CTkLabel(dialog, text="Description").pack(pady=(5, 0))
        desc_entry = ctk.CTkTextbox(dialog, width=300, height=80)
        desc_entry.pack(pady=(0, 10))

        ctk.CTkLabel(dialog, text="Prix*").pack(pady=(5, 0))
        price_entry = ctk.CTkEntry(dialog, width=150)
        price_entry.pack(pady=(0, 10))

        ctk.CTkLabel(dialog, text="Stock initial*").pack(pady=(5, 0))
        stock_entry = ctk.CTkEntry(dialog, width=150)
        stock_entry.pack(pady=(0, 20))

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkButton(
            btn_frame,
            text=t("common.cancel"),
            command=dialog.destroy,
            fg_color="gray",
            hover_color="darkgray",
        ).pack(side="right", padx=10)

        ctk.CTkButton(btn_frame, text=t("common.save"), command=save_product).pack(
            side="right"
        )

        name_entry.focus_set()

    def search_products(self, event=None):
        """Rechercher des produits en fonction du texte saisi"""
        search_query = self.search_entry.get().strip()
        if search_query:
            self.load_products(search_query)
        else:
            self.load_products()

    def on_sale_search_keyrelease(self, event):
        """Handle key release for sale search autocomplete."""
        if not hasattr(self, "sale_search_entry"):
            return

        query = self.sale_search_entry.get().strip()

        if not query:
            self.hide_sale_dropdown()
            return

        # Get matching sales from database (by client name or document number)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT DISTINCT c.nom
                FROM documents d
                LEFT JOIN clients c ON c.id = d.client_id
                WHERE d.type IN ('factures', 'bl') AND c.nom LIKE ?
                ORDER BY c.nom
                LIMIT 5
                """,
                (f"%{query}%",),
            )
            results = cursor.fetchall()
        finally:
            conn.close()

        if results:
            # Show dropdown with suggestions
            self.show_sale_dropdown([r[0] for r in results])
        else:
            self.hide_sale_dropdown()

    def show_sale_dropdown(self, items):
        """Show autocomplete dropdown for sales."""
        if not hasattr(self, "sale_search_container"):
            return

        # Clear existing dropdown if exists
        if hasattr(self, "sale_dropdown"):
            for widget in self.sale_dropdown_items:
                widget.destroy()
            self.sale_dropdown_items = []
            self.sale_dropdown.pack_forget()

        # Create dropdown container if needed
        if not hasattr(self, "sale_dropdown"):
            self.sale_dropdown = ctk.CTkFrame(
                self.sale_search_container, fg_color="gray20", corner_radius=6
            )
            self.sale_dropdown_items = []

        # Add new items
        for item in items[:5]:
            btn = ctk.CTkButton(
                self.sale_dropdown,
                text=item,
                command=lambda x=item: self.select_sale_suggestion(x),
                fg_color="transparent",
                text_color="white",
                hover_color="gray40",
                anchor="w",
                height=28,
                corner_radius=4,
            )
            btn.pack(fill="x", padx=2, pady=1)
            self.sale_dropdown_items.append(btn)

        # Show dropdown
        self.sale_dropdown.pack(fill="x", pady=(2, 0), expand=False)
        self.sale_search_entry.focus_set()

    def hide_sale_dropdown(self):
        """Hide sale autocomplete dropdown."""
        if hasattr(self, "sale_dropdown"):
            self.sale_dropdown.pack_forget()
            for widget in self.sale_dropdown_items:
                widget.destroy()
            self.sale_dropdown_items = []

    def select_sale_suggestion(self, suggestion):
        """Select an autocomplete suggestion for sales."""
        if hasattr(self, "sale_search_entry"):
            self.sale_search_entry.delete(0, "end")
            self.sale_search_entry.insert(0, suggestion)
        self.hide_sale_dropdown()
        self.search_sales()

    def search_sales(self, event=None):
        """Rechercher des ventes"""
        search_query = self.sale_search_entry.get().strip()
        self.load_sales(search_query)
        self.hide_sale_dropdown()

    def on_product_search_keyrelease(self, event):
        """Handle key release for product search autocomplete."""
        if not hasattr(self, "search_entry"):
            return

        query = self.search_entry.get().strip()

        if not query:
            self.hide_product_dropdown()
            return

        # Get matching products from database
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT name FROM products
                WHERE name LIKE ? OR description LIKE ?
                ORDER BY name
                LIMIT 5
                """,
                (f"%{query}%", f"%{query}%"),
            )
            results = cursor.fetchall()
        finally:
            conn.close()

        if results:
            # Show dropdown with suggestions
            self.show_product_dropdown([r[0] for r in results])
        else:
            self.hide_product_dropdown()

    def show_product_dropdown(self, items):
        """Show autocomplete dropdown for products."""
        if not hasattr(self, "product_search_container"):
            # Create container if it doesn't exist
            search_frame = self.search_entry.master
            self.product_search_container = ctk.CTkFrame(
                search_frame, fg_color="transparent"
            )
            self.product_search_container.pack(side="left", padx=(0, 10))

            # Move search entry to container
            self.search_entry.pack_forget()
            self.search_entry.pack(fill="x")

            # Create dropdown
            self.product_dropdown = ctk.CTkFrame(
                self.product_search_container, fg_color="gray20", corner_radius=6
            )
            self.product_dropdown.pack(fill="x", pady=(2, 0), expand=False)
            self.product_dropdown.pack_forget()
            self.product_dropdown_items = []

        # Clear existing items
        for widget in self.product_dropdown_items:
            widget.destroy()
        self.product_dropdown_items = []

        # Add new items
        for item in items[:5]:
            btn = ctk.CTkButton(
                self.product_dropdown,
                text=item,
                command=lambda x=item: self.select_product_suggestion(x),
                fg_color="transparent",
                text_color="white",
                hover_color="gray40",
                anchor="w",
                height=28,
                corner_radius=4,
            )
            btn.pack(fill="x", padx=2, pady=1)
            self.product_dropdown_items.append(btn)

        # Show dropdown
        self.product_dropdown.pack(fill="x", pady=(2, 0), expand=False)
        self.search_entry.focus_set()

    def hide_product_dropdown(self):
        """Hide product autocomplete dropdown."""
        if hasattr(self, "product_dropdown"):
            self.product_dropdown.pack_forget()
            for widget in self.product_dropdown_items:
                widget.destroy()
            self.product_dropdown_items = []

    def select_product_suggestion(self, suggestion):
        """Select an autocomplete suggestion for products."""
        if hasattr(self, "search_entry"):
            self.search_entry.delete(0, "end")
            self.search_entry.insert(0, suggestion)
        self.hide_product_dropdown()
        self.load_products(suggestion)

    def load_sales(self, search_query=None):
        """Charger les ventes depuis la base de données"""

        for item in self.sales_tree.get_children():
            self.sales_tree.delete(item)

        example_sales = [
            (
                1,
                "2025-01-01 10:30:00",
                "Client 1",
                format_money(15050),
                "Terminée",
                "Voir | Supprimer",
            ),
            (
                2,
                "2025-01-02 14:45:00",
                "Client 2",
                format_money(23075),
                "En attente",
                "Voir | Supprimer",
            ),
        ]

        for sale in example_sales:
            self.sales_tree.insert("", "end", values=sale)

    def search_sales(self, event=None):
        """Rechercher des ventes"""
        search_query = self.sale_search_entry.get().strip()

        self.load_sales(search_query)

    def show_new_sale_dialog(self):
        """Afficher la boîte de dialogue de nouvelle vente"""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Nouvelle Vente")
        dialog.geometry("800x600")
        try:
            dialog.transient(self)
            dialog.grab_set()
        except Exception as e:
            print(f"Warning: could not set modal grab for new sale dialog: {e}")

        label = ctk.CTkLabel(dialog, text="Formulaire de nouvelle vente")
        label.pack(pady=20)

    def on_sale_double_click(self, event):
        """Gérer le double-clic sur une vente pour voir les détails"""
        selected_item = self.sales_tree.selection()
        if not selected_item:
            return

        sale_id = self.sales_tree.item(selected_item, "values")[0]

        messagebox.showinfo("Détails de la vente", f"Détails de la vente #{sale_id}")

    def on_product_double_click(self, event):
        """Gérer le double-clic sur un produit pour le modifier"""

        selected_item = self.products_tree.selection()
        if not selected_item:
            return

        item_values = self.products_tree.item(selected_item, "values")
        product_id = item_values[0]

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM products WHERE id = ?", (product_id,))
        product = cursor.fetchone()
        conn.close()

        if not product:
            messagebox.showerror(t("common.error"), "Produit introuvable")
            return

        dialog = ctk.CTkToplevel(self)
        dialog.title("Modifier le produit")
        dialog.geometry("400x400")
        try:
            dialog.transient(self)
            dialog.grab_set()
        except Exception as e:
            print(
                f"Warning: could not set modal grab for modify product dialog (small): {e}"
            )

        dialog = ctk.CTkToplevel(self)
        dialog.title("Modifier le produit")
        dialog.geometry("500x550")
        try:
            dialog.transient(self)
            dialog.grab_set()
        except Exception as e:
            print(
                f"Warning: could not set modal grab for modify product dialog (large): {e}"
            )

        header = ctk.CTkFrame(dialog, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 10))
        ctk.CTkLabel(header, text="Modifier le produit", font=self.ui["font_h1"]).pack(
            side="left"
        )

        form_frame = ctk.CTkScrollableFrame(dialog, fg_color="transparent")
        form_frame.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        form_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            form_frame, text="Nom du produit*", font=self.ui["font_body"]
        ).grid(row=0, column=0, sticky="w", pady=(10, 0))
        name_entry = ctk.CTkEntry(form_frame)
        name_entry.insert(0, product[1])
        name_entry.grid(row=1, column=0, sticky="we", pady=(0, 10))

        ctk.CTkLabel(form_frame, text="Description", font=self.ui["font_body"]).grid(
            row=2, column=0, sticky="w", pady=(5, 0)
        )
        desc_entry = ctk.CTkTextbox(form_frame, height=80)
        desc_entry.insert("1.0", product[2] if product[2] else "")
        desc_entry.grid(row=3, column=0, sticky="we", pady=(0, 10))

        price_stock_inner_frame = ctk.CTkFrame(form_frame, fg_color="transparent")
        price_stock_inner_frame.grid(row=4, column=0, sticky="we", pady=(5, 0))
        price_stock_inner_frame.grid_columnconfigure(0, weight=1)
        price_stock_inner_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            price_stock_inner_frame, text="Prix*", font=self.ui["font_body"]
        ).grid(row=0, column=0, sticky="w")
        price_entry = ctk.CTkEntry(price_stock_inner_frame)
        price_entry.insert(0, money_to_input(product[3]))
        price_entry.grid(row=1, column=0, sticky="we", padx=(0, 10), pady=(0, 10))

        ctk.CTkLabel(
            price_stock_inner_frame, text="Stock*", font=self.ui["font_body"]
        ).grid(row=0, column=1, sticky="w")
        stock_entry = ctk.CTkEntry(price_stock_inner_frame)
        stock_entry.insert(0, str(product[4] if product[4] is not None else "0"))
        stock_entry.grid(row=1, column=1, sticky="we", pady=(0, 10))

        def save_changes():
            name = name_entry.get()
            description = desc_entry.get("1.0", "end-1c")
            price = price_entry.get()
            stock = stock_entry.get()

            if not all([name, price, stock]):
                messagebox.showerror(
                    t("common.error"), t("validation.fill_required")
                )
                return

            try:
                price = _validate_unit_price(price)
                stock = int(stock)
                if price is None:
                    raise ValueError("invalid price")

                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE products
                    SET name = ?, description = ?, price = ?, stock = ?
                    WHERE id = ?
                    """,
                    (name, description, price, stock, product_id),
                )
                conn.commit()
                conn.close()

                messagebox.showinfo(t("register.success_title"), "Produit mis à jour avec succès!")
                self.load_products()
                dialog.destroy()

            except ValueError:
                messagebox.showerror(
                    t("common.error"),
                    t("validation.price_stock_numeric"),
                )

        footer = ctk.CTkFrame(dialog, fg_color="transparent")
        footer.pack(fill="x", padx=20, pady=(0, 15))

        ctk.CTkButton(
            footer,
            text=t("common.cancel"),
            command=dialog.destroy,
            fg_color="gray",
            hover_color="darkgray",
        ).pack(side="right", padx=10)

        ctk.CTkButton(footer, text=t("common.save"), command=save_changes).pack(
            side="right"
        )

        name_entry.focus_set()

    def create_clients_frame(self):
        frame = ctk.CTkFrame(self.main_frame, corner_radius=0, fg_color="transparent")

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 20))

        self.clients_header_label = ctk.CTkLabel(header, text=t("clients.header_title"), font=self.ui["font_h1"])
        self.clients_header_label.pack(side="left")

        self.delete_client_btn = ctk.CTkButton(
            header,
            text=t("common.delete"),
            command=self.delete_selected_client,
            fg_color=self.ui.get("danger_fg", "#FF4B4B"),
            hover_color=self.ui.get("danger_hover", "#FF0000"),
            width=120,
            state="disabled",
        )
        self.delete_client_btn.pack(side="right", padx=(6, 0))

        self.clients_add_btn = ctk.CTkButton(
            header,
            text=t("clients.button_add"),
            command=lambda: self.show_client_dialog(None),
        )
        self.clients_add_btn.pack(side="right", padx=(0, 6))

        search_frame = ctk.CTkFrame(frame, fg_color="transparent")
        search_frame.pack(fill="x", pady=(0, 20))

        # Container for client search entry and dropdown
        self.client_search_container = ctk.CTkFrame(
            search_frame, fg_color="transparent"
        )
        self.client_search_container.pack(side="left", padx=(0, 10))

        self.client_search_entry = ctk.CTkEntry(
            self.client_search_container,
            width=350,
            placeholder_text=t("clients.search_placeholder"),
        )
        self.client_search_entry.pack(fill="x")

        # Autocomplete dropdown for clients
        self.client_dropdown = ctk.CTkFrame(
            self.client_search_container, fg_color="gray20", corner_radius=6
        )
        self.client_dropdown.pack(fill="x", pady=(2, 0), expand=False)
        self.client_dropdown.pack_forget()  # Hide initially

        self.client_dropdown_items = []

        # Search button
        self.clients_search_btn = ctk.CTkButton(
            search_frame, text=t("common.search"), command=self.search_clients
        )
        self.clients_search_btn.pack(side="left")

        # Bind events for autocomplete
        self.client_search_entry.bind("<KeyRelease>", self.on_client_search_keyrelease)
        self.client_search_entry.bind("<Return>", self.search_clients)
        self.client_search_entry.bind("<Escape>", lambda e: self.hide_client_dropdown())
        self.client_search_entry.bind(
            "<FocusOut>", lambda e: self.after(150, self.hide_client_dropdown)
        )
        self.client_dropdown.bind("<Button-1>", lambda e: "break")  # Prevent focus loss

        columns = ("ID", t("clients.col_name"), t("register.email_placeholder"), t("clients.col_phone"), t("clients.col_address"), t("clients.col_tax_id"))
        self.clients_tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse"
        )

        self.clients_tree.heading("ID", text="ID")
        self.clients_tree.heading(t("clients.col_name"), text=t("clients.col_name"))
        self.clients_tree.heading(t("register.email_placeholder"), text=t("register.email_placeholder"))
        self.clients_tree.heading(t("clients.col_phone"), text=t("clients.col_phone"))
        self.clients_tree.heading(t("clients.col_address"), text=t("clients.col_address"))
        self.clients_tree.heading(t("clients.col_tax_id"), text=t("clients.col_tax_id"))

        self.clients_tree.column("ID", width=60, anchor="center")
        self.clients_tree.column(t("clients.col_name"), width=180, anchor="w")
        self.clients_tree.column(t("register.email_placeholder"), width=220, anchor="w")
        self.clients_tree.column(t("clients.col_phone"), width=140, anchor="center")
        self.clients_tree.column(t("clients.col_address"), width=260, anchor="w")
        self.clients_tree.column(t("clients.col_tax_id"), width=150, anchor="w")

        scrollbar = ttk.Scrollbar(
            frame, orient="vertical", command=self.clients_tree.yview
        )
        scrollbar.pack(side="right", fill="y")
        self.clients_tree.configure(yscrollcommand=scrollbar.set)
        self.clients_tree.pack(fill="both", expand=True)

        self.clients_tree.bind("<Double-1>", self.on_client_double_click)

        self.clients_tree.bind(
            "<<TreeviewSelect>>", lambda e: self._update_client_delete_button()
        )
        
        # Add sorting functionality
        self.client_sort_state = {}  # Track sort state per column
        columns = ("ID", t("clients.col_name"), t("register.email_placeholder"), t("clients.col_phone"), t("clients.col_address"), t("clients.col_tax_id"))
        for col in columns:
            self.clients_tree.heading(col, command=lambda c=col: self.sort_clients(c))
        
        self.load_clients()

        self.frames["clients"] = frame

    def sort_clients(self, column):
        """Sort clients by column (ascending/descending toggle)."""
        if not hasattr(self, "clients_tree"):
            return
        
        # Get current sort state for this column
        current_state = self.client_sort_state.get(column, False)  # False = ascending
        new_state = not current_state  # Toggle
        self.client_sort_state[column] = new_state
        
        # Update all headers to show arrows
        arrow = "▲" if new_state else "▼"
        all_cols = ["ID", t("clients.col_name"), t("register.email_placeholder"), t("clients.col_phone"), t("clients.col_address"), t("clients.col_tax_id")]
        for col in all_cols:
            if col == column:
                self.clients_tree.heading(col, text=f"{col} {arrow}")
            else:
                # Remove arrow from other columns
                clean_col = col.replace(" ▲", "").replace(" ▼", "")
                self.clients_tree.heading(col, text=clean_col)
        
        # Get all items from tree
        items = [(self.clients_tree.item(item, "values"), item) for item in self.clients_tree.get_children()]
        
        # Column index mapping
        col_index = {
            "ID": 0,
            t("clients.col_name"): 1,
            t("register.email_placeholder"): 2,
            t("clients.col_phone"): 3,
            t("clients.col_address"): 4,
            t("clients.col_tax_id"): 5,
        }
        
        idx = col_index.get(column, 0)
        
        # Sort items
        def sort_key(item):
            values = item[0]
            val = values[idx]
            
            # Handle ID
            if column == "ID":
                try:
                    return int(val)
                except:
                    return 0
            # Handle text columns
            else:
                return str(val).lower()
        
        items.sort(key=sort_key, reverse=new_state)
        
        # Reinsert items in sorted order
        for item in self.clients_tree.get_children():
            self.clients_tree.delete(item)
        
        for values, item_id in items:
            self.clients_tree.insert("", "end", values=values)
        
        self._apply_zebra(self.clients_tree)

    def load_clients(self, search_query=None):
        if not hasattr(self, "clients_tree"):
            return

        for item in self.clients_tree.get_children():
            self.clients_tree.delete(item)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS clients (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nom TEXT NOT NULL,
                    email TEXT,
                    telephone TEXT,
                    adresse TEXT
                )
                """
            )

            if search_query:
                q = str(search_query).strip()
                # Handle the new format "ID - Nom - Matricule"
                if " - " in q:
                    parts = q.split(" - ")
                    # Extract ID from the first part if it's a number
                    id_part = parts[0].strip() if parts else ""
                    nom_part = parts[1].strip() if len(parts) > 1 else ""
                    matricule_part = parts[2].strip() if len(parts) > 2 else ""

                    # Build conditions based on available parts
                    conditions = []
                    params = []

                    if id_part and id_part.isdigit():
                        conditions.append("CAST(id AS TEXT) LIKE ?")
                        params.append(f"%{id_part}%")
                    if nom_part:
                        conditions.append("nom LIKE ?")
                        params.append(f"%{nom_part}%")
                    if matricule_part:
                        conditions.append("matricule_fiscale LIKE ?")
                        params.append(f"%{matricule_part}%")

                    # If no specific parts found, search with full query
                    if not conditions:
                        conditions = [
                            "CAST(id AS TEXT) LIKE ? OR nom LIKE ? OR matricule_fiscale LIKE ?"
                        ]
                        params = [f"%{q}%", f"%{q}%", f"%{q}%"]

                    where_clause = " OR ".join(conditions)
                    sql = f"""
                        SELECT DISTINCT id, nom, email, telephone, adresse, matricule_fiscale
                        FROM clients
                        WHERE {where_clause}
                        ORDER BY nom
                        """
                    cursor.execute(sql, tuple(params))
                else:
                    cursor.execute(
                        """
                        SELECT id, nom, email, telephone, adresse, matricule_fiscale
                        FROM clients
                        WHERE CAST(id AS TEXT) LIKE ? OR
                              nom LIKE ? OR
                              matricule_fiscale LIKE ? OR
                              email LIKE ? OR
                              telephone LIKE ?
                        ORDER BY nom
                        """,
                        (f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"),
                    )
            else:
                cursor.execute(
                    "SELECT id, nom, email, telephone, adresse, matricule_fiscale FROM clients ORDER BY nom"
                )

            for row in cursor.fetchall():
                self.clients_tree.insert(
                    "",
                    "end",
                    values=(
                        row[0],
                        row[1] or "",
                        row[2] or "",
                        row[3] or "",
                        row[4] or "",
                        row[5] or "",
                    ),
                )
        finally:
            conn.close()

        self._apply_zebra(self.clients_tree)

    def search_clients(self, event=None):
        """Search clients by name, matricule fiscale, email, or phone."""
        query = ""
        if hasattr(self, "client_search_entry"):
            query = self.client_search_entry.get().strip()

        if not query:
            self.load_clients()
            return

        # Handle the new format "ID - Nom - Matricule"
        if " - " in query:
            parts = query.split(" - ")
            # Extract ID from the first part if it's a number
            id_part = parts[0].strip() if parts else ""
            nom_part = parts[1].strip() if len(parts) > 1 else ""
            matricule_part = parts[2].strip() if len(parts) > 2 else ""

            # Build conditions based on available parts
            conditions = []
            params = []

            if id_part and id_part.isdigit():
                conditions.append("CAST(id AS TEXT) LIKE ?")
                params.append(f"%{id_part}%")
            if nom_part:
                conditions.append("nom LIKE ?")
                params.append(f"%{nom_part}%")
            if matricule_part:
                conditions.append("matricule_fiscale LIKE ?")
                params.append(f"%{matricule_part}%")

            # If no specific parts found, search with full query
            if not conditions:
                conditions = [
                    "CAST(id AS TEXT) LIKE ? OR nom LIKE ? OR matricule_fiscale LIKE ?"
                ]
                params = [f"%{query}%", f"%{query}%", f"%{query}%"]

            where_clause = " OR ".join(conditions)
            sql = f"""
                SELECT DISTINCT id, nom, email, telephone, adresse, matricule_fiscale
                FROM clients
                WHERE {where_clause}
                ORDER BY nom
                """
        else:
            # Get matching clients from database
            sql = """
                SELECT DISTINCT id, nom, email, telephone, adresse, matricule_fiscale
                FROM clients
                WHERE CAST(id AS TEXT) LIKE ? OR
                      nom LIKE ? OR
                      matricule_fiscale LIKE ? OR
                      email LIKE ? OR
                      telephone LIKE ?
                ORDER BY nom
                """
            params = [
                f"%{query}%",
                f"%{query}%",
                f"%{query}%",
                f"%{query}%",
                f"%{query}%",
            ]

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(sql, tuple(params))
            results = cursor.fetchall()
        finally:
            conn.close()

        # Clear current tree
        if hasattr(self, "clients_tree"):
            for item in self.clients_tree.get_children():
                self.clients_tree.delete(item)

            # Populate with search results
            for row in results:
                self.clients_tree.insert(
                    "",
                    "end",
                    values=(
                        row[0],
                        row[1] or "",
                        row[2] or "",
                        row[3] or "",
                        row[4] or "",
                        row[5] or "",
                    ),
                )

            self._apply_zebra(self.clients_tree)

        self.hide_client_dropdown()

    def show_client_dialog(self, client_id=None):
        dialog = ctk.CTkToplevel(self)
        dialog.title(t("client_dialog.title_edit") if client_id else t("client_dialog.title_new"))
        dialog.geometry("980x850")
        try:
            dialog.transient(self)
            dialog.grab_set()
        except Exception as e:
            print(f"Warning: could not set modal grab for client dialog: {e}")

        nom_value = ""
        email_value = ""
        tel_value = ""
        addr_value = ""
        matricule_fiscale_value = ""

        if client_id:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT nom, email, telephone, adresse, matricule_fiscale FROM clients WHERE id = ?",
                    (client_id,),
                )
                row = cursor.fetchone()
                if row:
                    nom_value = row[0] or ""
                    email_value = row[1] or ""
                    tel_value = row[2] or ""
                    addr_value = row[3] or ""
                    matricule_fiscale_value = row[4] or ""
            finally:
                conn.close()

        header = ctk.CTkFrame(dialog, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 10))

        ctk.CTkLabel(
            header,
            text=t("client_dialog.title_edit"),
            font=self.ui["font_h1"],
        ).pack(side="left")

        actions = ctk.CTkFrame(header, fg_color="transparent")
        actions.pack(side="right")
        ctk.CTkButton(
            actions,
            text=t("client_dialog.button_documents"),
            state="disabled",
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "gray90"),
        ).pack(side="right")

        top = ctk.CTkFrame(dialog, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(0, 10))

        left = ctk.CTkFrame(top, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        right = ctk.CTkFrame(top, fg_color="transparent")
        right.pack(side="right")

        ctk.CTkLabel(left, text=t("client_dialog.label_name"), font=self.ui["font_h2"]).grid(
            row=0, column=0, sticky="w"
        )
        ctk.CTkLabel(left, text=t("clients.col_phone"), font=self.ui["font_h2"]).grid(
            row=0, column=1, sticky="w", padx=(10, 0)
        )

        nom_entry = ctk.CTkEntry(left, width=300)
        nom_entry.insert(0, nom_value)
        nom_entry.grid(row=1, column=0, sticky="w", pady=(0, 8))

        tel_entry = ctk.CTkEntry(left, width=200)
        tel_entry.insert(0, tel_value)
        tel_entry.grid(row=1, column=1, sticky="w", pady=(0, 8), padx=(10, 0))

        ctk.CTkLabel(left, text=t("register.email_placeholder"), font=self.ui["font_h2"]).grid(
            row=2, column=0, sticky="w"
        )

        email_entry = ctk.CTkEntry(left, width=300)
        email_entry.insert(0, email_value)
        email_entry.grid(row=3, column=0, sticky="w", pady=(0, 8))

        ctk.CTkLabel(left, text=t("clients.col_address"), font=self.ui["font_h2"]).grid(
            row=4, column=0, sticky="w"
        )

        addr_entry = ctk.CTkEntry(left, width=510)
        addr_entry.insert(0, addr_value)
        addr_entry.grid(row=5, column=0, columnspan=2, sticky="w")

        ctk.CTkLabel(left, text=t("clients.col_tax_id"), font=self.ui["font_h2"]).grid(
            row=6, column=0, sticky="w"
        )

        matricule_fiscale_entry = ctk.CTkEntry(left, width=510)
        matricule_fiscale_entry.insert(0, matricule_fiscale_value)
        matricule_fiscale_entry.grid(row=7, column=0, columnspan=2, sticky="w")

        statut_label = ctk.CTkLabel(
            right,
            text=t("client_dialog.label_client_id", id=str(client_id if client_id else "New")),
            font=self.ui["font_h2"],
        )
        statut_label.pack(anchor="e")

        content = ctk.CTkFrame(dialog, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=20, pady=(10, 10))

        ctk.CTkLabel(
            content, text=t("client_dialog.history_heading"), font=self.ui["font_h2"]
        ).pack(anchor="w", pady=(0, 5))

        columns = (t("article_dialog.col_date"), t("client_dialog.col_type"), t("client_dialog.col_number"), t("client_dialog.col_ttc"), t("client_dialog.col_status"))
        history_tree = ttk.Treeview(
            content, columns=columns, show="headings", selectmode="browse"
        )
        for col in columns:
            history_tree.heading(col, text=col)
            history_tree.column(col, anchor="center")

        history_tree.column(t("client_dialog.col_type"), width=100)
        history_tree.column(t("client_dialog.col_number"), width=120)
        history_tree.column(t("client_dialog.col_ttc"), width=100, anchor="e")

        scrollbar = ttk.Scrollbar(
            content, orient="vertical", command=history_tree.yview
        )
        scrollbar.pack(side="right", fill="y")
        history_tree.configure(yscrollcommand=scrollbar.set)
        history_tree.pack(fill="both", expand=True)

        total_spent = 0
        if client_id:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    SELECT date, type, numero, total_ttc, statut
                    FROM documents
                    WHERE client_id = ?
                    ORDER BY date DESC
                    """,
                    (client_id,),
                )
                docs = cursor.fetchall()
                for d in docs:
                    dt = d[0]
                    typ = d[1]
                    num = d[2]
                    ttc = as_cents(d[3])
                    st = d[4]
                    history_tree.insert(
                        "",
                        "end",
                        values=(dt, typ, num, format_money(ttc), st),
                    )
                    if typ in ("factures", "reception"):
                        total_spent += ttc
            finally:
                conn.close()

            self._apply_zebra(history_tree)

        totals_bar = ctk.CTkFrame(dialog, fg_color="transparent")
        totals_bar.pack(fill="x", padx=20, pady=(0, 10))

        totals_right = ctk.CTkFrame(totals_bar, fg_color="transparent")
        totals_right.pack(side="right")

        spent_box = ctk.CTkFrame(totals_right, fg_color="transparent")
        spent_box.pack(side="right")
        ctk.CTkLabel(
            spent_box,
            text=t("client_dialog.total_spent"),
            font=self.ui["font_body"],
            text_color=("gray35", "gray70"),
        ).pack(anchor="e")
        spent_label = ctk.CTkLabel(
            spent_box, text=format_money(total_spent), font=self.ui["font_h2"]
        )
        spent_label.pack(anchor="e")

        footer = ctk.CTkFrame(dialog, fg_color="transparent")
        footer.pack(fill="x", padx=20, pady=(0, 15))

        def save_client():
            nom = nom_entry.get().strip()
            email = email_entry.get().strip()
            telephone = tel_entry.get().strip()
            adresse = addr_entry.get().strip()
            matricule_fiscale = matricule_fiscale_entry.get().strip()

            if not nom:
                messagebox.showerror(t("common.error"), t("clients.error_name_required"))
                return

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            try:
                if client_id:
                    cursor.execute(
                        "UPDATE clients SET nom=?, email=?, telephone=?, adresse=?, matricule_fiscale=? WHERE id=?",
                        (nom, email, telephone, adresse, matricule_fiscale, client_id),
                    )
                else:
                    cursor.execute(
                        "INSERT INTO clients (nom, email, telephone, adresse, matricule_fiscale) VALUES (?, ?, ?, ?, ?)",
                        (nom, email, telephone, adresse, matricule_fiscale),
                    )
                conn.commit()
            finally:
                conn.close()

            self.load_clients()
            dialog.destroy()

        ctk.CTkButton(
            footer,
            text=t("common.close"),
            command=dialog.destroy,
            fg_color="gray",
            hover_color="darkgray",
        ).pack(side="right", padx=10)

        ctk.CTkButton(footer, text=t("common.save"), command=save_client).pack(
            side="right"
        )

        nom_entry.focus_set()

    def on_client_double_click(self, event):
        selected = self.clients_tree.selection()
        if not selected:
            return
        values = self.clients_tree.item(selected, "values")
        if not values:
            return
        self.show_client_dialog(int(values[0]))

    def _delete_documents_cascade(self, cur, doc_ids):
        """Delete documents and their dependent rows.

        Stock movements for validated BL/reception documents are reversed,
        and conversion/source links pointing at the deleted documents are
        cleared so no dangling references remain.
        """
        doc_ids = [int(doc_id) for doc_id in doc_ids]
        if not doc_ids:
            return

        placeholders = ",".join("?" for _ in doc_ids)
        ids = tuple(doc_ids)

        cur.execute(
            f"SELECT id, type, statut FROM documents WHERE id IN ({placeholders})",
            ids,
        )
        documents = cur.fetchall()

        for doc_id, doc_type, statut in documents:
            if statut != "valide" or doc_type not in ("bl", "reception"):
                continue

            cur.execute(
                "SELECT article_id, quantite FROM document_lignes WHERE document_id = ?",
                (doc_id,),
            )
            for article_id, quantite in cur.fetchall():
                if article_id is None:
                    continue
                qty = int(float(quantite or 0))
                if doc_type == "bl":
                    cur.execute(
                        "UPDATE articles SET stock = stock + ? WHERE id = ?",
                        (qty, int(article_id)),
                    )
                else:
                    cur.execute(
                        "UPDATE articles SET stock = stock - ? WHERE id = ?",
                        (qty, int(article_id)),
                    )

        cur.execute(
            f"""
            UPDATE documents
            SET converted_to_id = NULL, converted_at = NULL
            WHERE converted_to_id IN ({placeholders})
            """,
            ids,
        )
        cur.execute(
            f"UPDATE documents SET source_bl_id = NULL WHERE source_bl_id IN ({placeholders})",
            ids,
        )
        cur.execute(
            f"DELETE FROM payments WHERE document_id IN ({placeholders})",
            ids,
        )
        cur.execute(
            f"DELETE FROM document_lignes WHERE document_id IN ({placeholders})",
            ids,
        )
        cur.execute(
            f"DELETE FROM documents WHERE id IN ({placeholders})",
            ids,
        )

    def delete_selected_client(self):
        if not hasattr(self, "clients_tree"):
            messagebox.showerror(t("common.error"), t("clients.error_no_list"))
            return
        sel = self.clients_tree.selection()
        if not sel:
            messagebox.showerror(t("common.error"), t("clients.error_select_to_delete"))
            return
        try:
            vals = self.clients_tree.item(sel[0], "values")
        except Exception:
            messagebox.showerror(t("common.error"), t("articles.error_read_selection"))
            return
        if not vals:
            messagebox.showerror(t("common.error"), t("clients.error_select_to_delete"))
            return
        try:
            client_id = int(vals[0])
        except Exception:
            messagebox.showerror(t("common.error"), t("clients.error_invalid_id"))
            return

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT COUNT(*) FROM documents WHERE client_id = ?", (client_id,)
            )
            row = cur.fetchone()
            docs_count = row[0] if row else 0

            if docs_count > 0:
                if not messagebox.askyesno(
                    t("common.confirm"),
                    t("clients.confirm_delete_with_docs", count=docs_count),
                    icon="warning",
                ):
                    return
            elif not messagebox.askyesno(
                t("common.confirm"), t("clients.confirm_delete")
            ):
                return

            cur.execute("SELECT id FROM documents WHERE client_id = ?", (client_id,))
            doc_ids = [r[0] for r in cur.fetchall()]
            self._delete_documents_cascade(cur, doc_ids)

            cur.execute("DELETE FROM clients WHERE id = ?", (client_id,))
            conn.commit()
        except Exception as e:
            conn.rollback()
            messagebox.showerror(
                t("common.error"), t("clients.error_delete", error=str(e))
            )
            return
        finally:
            conn.close()

        try:
            self.load_clients()
        except Exception:
            pass
        try:
            self.load_paiements()
        except Exception:
            pass
        try:
            self.load_articles()
        except Exception:
            pass
        for doc_type in ("devis", "bl", "factures", "reception"):
            try:
                self.load_documents(doc_type)
            except Exception:
                pass

        messagebox.showinfo(t("common.deleted"), t("clients.deleted_success"))

    def delete_selected_document(self, doc_type):
        tree = getattr(self, "documents_trees", {}).get(doc_type)
        if not tree:
            messagebox.showerror(t("common.error"), t("payment_dialog.error_no_doc_selected"))
            return
        sel = tree.selection()
        if not sel:
            messagebox.showerror(t("common.error"), t("documents.error_select"))
            return
        try:
            vals = tree.item(sel[0], "values")
        except Exception:
            messagebox.showerror(t("common.error"), t("articles.error_read_selection"))
            return
        if not vals:
            messagebox.showerror(t("common.error"), t("documents.error_select"))
            return
        try:
            doc_id = int(vals[0])
        except Exception:
            messagebox.showerror(t("common.error"), t("payment_dialog.error_invalid_doc_id"))
            return

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        try:
            cur.execute("SELECT type, statut FROM documents WHERE id = ?", (doc_id,))
            if not cur.fetchone():
                messagebox.showerror(
                    t("common.error"), t("payment_dialog.error_doc_not_found")
                )
                return

            if not messagebox.askyesno(
                t("common.confirm"),
                t("documents.confirm_delete_irreversible"),
            ):
                return

            self._delete_documents_cascade(cur, [doc_id])
            conn.commit()
        except Exception as e:
            conn.rollback()
            messagebox.showerror(t("common.error"), f"Erreur lors de la suppression : {e}")
            return
        finally:
            conn.close()

        try:
            self.load_documents(doc_type)
        except Exception:
            pass
        try:
            self.load_paiements()
        except Exception:
            pass
        try:
            self.load_articles()
        except Exception:
            pass

        messagebox.showinfo(t("common.deleted"), t("documents.deleted_success"))

    def _update_client_delete_button(self):
        try:
            btn = getattr(self, "delete_client_btn", None)
            if not btn or not hasattr(self, "clients_tree"):
                return
            sel = self.clients_tree.selection()
            btn.configure(state=("normal" if sel else "disabled"))
        except Exception:
            pass

    def _update_doc_delete_button(self, doc_type):
        try:
            btn = getattr(self, "doc_delete_buttons", {}).get(doc_type)
            tree = getattr(self, "documents_trees", {}).get(doc_type)
            if not btn or not tree:
                return
            sel = tree.selection()
            btn.configure(state=("normal" if sel else "disabled"))
        except Exception:
            pass

    def create_reports_frame(self):
        frame = ctk.CTkFrame(self.main_frame, corner_radius=0, fg_color="transparent")

        ctk.CTkLabel(frame, text=t("reports.header"), font=("Arial", 24, "bold")).pack(pady=20)

        ctk.CTkButton(frame, text="Exporter", command=self.export_report).pack(
            pady=(0, 20)
        )

        info = ctk.CTkLabel(
            frame,
            text="Cette section sera enrichie (statistiques, exports, etc.).",
            font=("Arial", 12),
        )
        info.pack(pady=10)

        self.frames["reports"] = frame

    def export_report(self):
        messagebox.showinfo("Export", "Export à implémenter.")

    def create_admin_frame(self):
        if not self._require_admin():
            return
        frame = ctk.CTkFrame(self.main_frame, corner_radius=0, fg_color="transparent")

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 20))

        ctk.CTkLabel(header, text=t("nav.admin"), font=("Arial", 24, "bold")).pack(
            side="left"
        )

        tabview = ctk.CTkTabview(frame)
        tabview.pack(fill="both", expand=True)
        tabview.add(t("admin.section_users"))
        tabview.add(t("admin.tab_maintenance"))

        users_frame = tabview.tab(t("admin.section_users"))

        columns = ("ID", "Username", t("register.email_placeholder"), t("clients.col_name"), t("admin.col_admin"))
        self.users_tree = ttk.Treeview(
            users_frame, columns=columns, show="headings", selectmode="browse"
        )
        for col in columns:
            self.users_tree.heading(col, text=col)
            self.users_tree.column(col, width=160, anchor="center")

        scrollbar = ttk.Scrollbar(
            users_frame, orient="vertical", command=self.users_tree.yview
        )
        scrollbar.pack(side="right", fill="y")
        self.users_tree.configure(yscrollcommand=scrollbar.set)

        self.users_tree.pack(fill="both", expand=True)
        self.load_users()

        maint_frame = tabview.tab(t("admin.tab_maintenance"))

        ctk.CTkLabel(
            maint_frame,
            text=t("admin.danger_zone"),
            font=("Arial", 20, "bold"),
            text_color="#FF4B4B",
        ).pack(pady=(20, 10), anchor="w", padx=20)

        ctk.CTkLabel(
            maint_frame,
            text="ATTENTION: Cette action effacera définitivement toutes les données (Articles, Clients, Documents, etc.).\nElle ne peut pas être annulée.",
            text_color="gray",
        ).pack(anchor="w", padx=20, pady=(0, 20))

        ctk.CTkButton(
            maint_frame,
            text=t("admin.button_reset_all"),
            command=self.wipe_database,
            fg_color="#FF4B4B",
            hover_color="#FF0000",
            height=40,
        ).pack(anchor="w", padx=20)

        self.frames["admin"] = frame

    def wipe_database(self):
        if not self._require_admin():
            return
        if not messagebox.askyesno(
            t("admin.critical_confirm_title"),
            "Êtes-vous SÛR de vouloir effacer TOUTE la base de données ?\n\nCette action est irréversible !",
            icon="warning",
        ):
            return

        dialog = ctk.CTkInputDialog(
            text=t("admin.type_delete_prompt"), title=t("admin.final_confirm_title")
        )
        text = dialog.get_input()

        if text != t("admin.delete_keyword"):
            messagebox.showinfo(t("msgbox.cancelled"), t("admin.cancelled"))
            return

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = cursor.fetchall()

            for table in tables:
                if table[0] != "sqlite_sequence" and table[0] != "users":
                    cursor.execute(f"DROP TABLE IF EXISTS {table[0]}")

            conn.commit()
            conn.close()

            self.init_db()

            if hasattr(self, "articles_tree"):
                self.load_articles()

            if hasattr(self, "clients_tree"):
                self.load_clients()

            if hasattr(self, "documents_trees"):
                for doc_type in ["devis", "bl", "factures", "reception"]:
                    self.load_documents(doc_type)

            if hasattr(self, "paiements_tree"):
                self.load_paiements()

            if hasattr(self, "sales_tree"):
                self.load_recent_sales()

            if hasattr(self, "products_tree") and hasattr(self, "load_products"):
                self.load_products()

            messagebox.showinfo(t("register.success_title"), t("admin.reset_success"))
            self.show_frame("admin")

        except Exception as e:
            messagebox.showerror(t("common.error"), t("admin.error_reset", error=str(e)))

    def load_users(self):
        for item in self.users_tree.get_children():
            self.users_tree.delete(item)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT id, username, email, full_name, is_admin FROM users ORDER BY username"
            )
            for row in cursor.fetchall():
                self.users_tree.insert(
                    "",
                    "end",
                    values=(
                        row[0],
                        row[1] or "",
                        row[2] or "",
                        row[3] or "",
                        t("common.yes") if row[4] else t("common.no"),
                    ),
                )
        finally:
            conn.close()

    def logout(self):
        """Fermer l'application et revenir à l'écran de connexion"""
        self.destroy()
        from main import LoginWindow

        login_window = LoginWindow()
        login_window.mainloop()

    def on_closing(self):
        """Gérer la fermeture de la fenêtre"""
        if messagebox.askokcancel(
            t("login.quit_title"), t("login.quit_confirm")
        ):
            self.quit()


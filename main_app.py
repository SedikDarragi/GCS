import os
import sqlite3
import tempfile
import shutil
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
from PIL import Image, ImageTk
import webbrowser

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


class MainApp(ctk.CTkToplevel):
    def __init__(self, user, parent=None):
        super().__init__(parent)

        self.user = {
            "id": user[0],
            "username": user[1],
            "is_admin": user[5] if len(user) > 5 else 0,
        }

        # Empêcher la fermeture immédiate de l'application
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        # Configuration de la fenêtre
        self.title(
            f"Gestion Commerciale - Connecté en tant que {self.user['username']}"
        )
        self.geometry("1200x700")
        self.minsize(1000, 600)

        # Configuration du style
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        # Ensure toplevel inherits theme from root if parent provided
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

        # Variables globales
        self.current_frame = None

        self.db_path = "commercial.db"
        self.init_db()

        self._setup_ttk_style()

        # Création de l'interface
        self.create_widgets()

        # Afficher la première vue par défaut
        self.show_frame("articles")

    def _update_article_search_suggestions(self, event=None):
        if not hasattr(self, "article_search_entry"):
            return
        query = self.article_search_entry.get().strip()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            if query:
                cursor.execute(
                    """
                    SELECT reference, designation
                    FROM articles
                    WHERE reference LIKE ? OR designation LIKE ?
                    ORDER BY designation
                    LIMIT 25
                    """,
                    (f"%{query}%", f"%{query}%"),
                )
            else:
                cursor.execute(
                    """
                    SELECT reference, designation
                    FROM articles
                    ORDER BY designation
                    LIMIT 25
                    """
                )
            values = []
            for ref, des in cursor.fetchall():
                ref = (ref or "").strip()
                des = (des or "").strip()
                if ref and des:
                    values.append(f"{ref} - {des}")
                elif des:
                    values.append(des)
                elif ref:
                    values.append(ref)

            self.article_search_entry.configure(values=values)

            if values and self.focus_get() == self.article_search_entry:
                self.after(
                    1, lambda: self._safe_open_combobox(self.article_search_entry)
                )
        finally:
            conn.close()

    def _update_client_search_suggestions(self, event=None):
        if not hasattr(self, "client_search_entry"):
            return
        query = self.client_search_entry.get().strip()
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            if query:
                cursor.execute(
                    """
                    SELECT nom, email, telephone
                    FROM clients
                    WHERE nom LIKE ? OR email LIKE ? OR telephone LIKE ?
                    ORDER BY nom
                    LIMIT 25
                    """,
                    (f"%{query}%", f"%{query}%", f"%{query}%"),
                )
            else:
                cursor.execute(
                    """
                    SELECT nom, email, telephone
                    FROM clients
                    ORDER BY nom
                    LIMIT 25
                    """
                )
            values = []
            for nom, email, tel in cursor.fetchall():
                nom = (nom or "").strip()
                email = (email or "").strip()
                tel = (tel or "").strip()
                parts = [p for p in [nom, email, tel] if p]
                if parts:
                    values.append(" | ".join(parts))

            self.client_search_entry.configure(values=values)

            if values and self.focus_get() == self.client_search_entry:
                self.after(
                    1, lambda: self._safe_open_combobox(self.client_search_entry)
                )
        finally:
            conn.close()

    def _safe_open_combobox(self, combo):
        try:
            combo.event_generate("<Down>")
        except Exception:
            pass

    def create_widgets(self):
        # Barre latérale
        self.sidebar = ctk.CTkFrame(
            self, width=self.ui["sidebar_width"], corner_radius=0
        )
        self.sidebar.pack(side="left", fill="y", expand=False)

        # Logo dans la barre latérale (robuste: CTkImage, fallback PhotoImage)
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            logo_path = os.path.join(base_dir, "assets", "logo.png")
            if os.path.exists(logo_path):
                # Open and normalize image
                pil_img = Image.open(logo_path).convert("RGBA")
                pil_resized = pil_img.resize((100, 100), Image.LANCZOS)
                # Try creating CTkImage from the PIL image first (preferred)
                try:
                    self.logo_image = ctk.CTkImage(
                        light_image=pil_resized, dark_image=pil_resized, size=(100, 100)
                    )
                    self.logo_label = ctk.CTkLabel(self.sidebar, image=self.logo_image, text="")
                except Exception as e:
                    print(f"CTkImage creation failed: {e}; falling back to tk.PhotoImage")
                    try:
                        # Create a PhotoImage explicitly with the current CTk instance as master
                        self._logo_tk = ImageTk.PhotoImage(pil_resized, master=self)
                        import tkinter as tk

                        self.logo_label = tk.Label(self.sidebar, image=self._logo_tk, bd=0)
                    except Exception as e2:
                        print(f"Fallback PhotoImage failed: {e2}")
                        # Last resort: text label
                        self.logo_label = ctk.CTkLabel(self.sidebar, text="LOGO", font=("Arial", 16, "bold"))
            else:
                print(f"Sidebar logo not found at {logo_path}")
                raise FileNotFoundError("Logo file missing")
        except Exception as e:
            print(f"Error loading sidebar logo: {e}")
            self.logo_label = ctk.CTkLabel(self.sidebar, text="LOGO", font=("Arial", 16, "bold"))

        self.logo_label.pack(pady=20, padx=10)

        # Menu de navigation
        self.menu_label = ctk.CTkLabel(
            self.sidebar, text="MENU", font=self.ui["font_h2"]
        )
        self.menu_label.pack(pady=(20, 10), padx=10, anchor="w")

        # Boutons de navigation
        nav_buttons = [
            ("Articles", "articles"),
            ("Clients", "clients"),
            ("Devis", "devis"),
            ("Bon de livraison", "bl"),
            ("Factures", "factures"),
            ("Bon de réception", "reception"),
            ("Paiements / Impayés", "paiements"),
        ]

        if self.user.get("is_admin"):
            nav_buttons.append(("Administration", "admin"))
        # Add About as a menu item
        nav_buttons.append(("À propos", "about"))

        self.nav_buttons = {}
        for text, command in nav_buttons:
            # Administration should open a separate settings window instead of a frame
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

        # Espacement
        ctk.CTkLabel(self.sidebar, text="").pack(fill="x", pady=20)

        # Informations utilisateur
        user_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        user_frame.pack(side="bottom", fill="x", padx=10, pady=10)

        ctk.CTkLabel(
            user_frame, text=f"Connecté en tant que :", font=("Arial", 10)
        ).pack(anchor="w")

        ctk.CTkLabel(
            user_frame, text=f"{self.user['username']}", font=("Arial", 12, "bold")
        ).pack(anchor="w")

        # Bouton de déconnexion
        self.logout_btn = ctk.CTkButton(
            user_frame,
            text="Déconnexion",
            command=self.logout,
            fg_color=self.ui["danger_fg"],
            hover_color=self.ui["danger_hover"],
            width=120,
        )
        self.logout_btn.pack(pady=(10, 0), fill="x")

        # Small separator before admin/settings area
        ctk.CTkLabel(self.sidebar, text="").pack(fill="x", pady=(6, 0))

        # About link in sidebar (small, clickable)
        try:
            about_lbl = ctk.CTkLabel(
                self.sidebar,
                text="À propos",
                text_color="blue",
                cursor="hand2",
                font=("Arial", 10, "underline"),
            )
            about_lbl.pack(fill="x", padx=10, pady=(6, 6), anchor="w")
            about_lbl.bind("<Button-1>", lambda e: self.open_about_dialog())
        except Exception:
            pass

        # Contenu principal
        self.main_frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.main_frame.pack(
            side="right",
            fill="both",
            expand=True,
            padx=self.ui["content_padx"],
            pady=self.ui["content_pady"],
        )

        # Dictionnaire pour stocker les frames
        self.frames = {}

        # Création des différentes vues (nouvelle structure)
        self.create_articles_frame()
        self.create_clients_frame()
        self.create_documents_frame("devis", "Devis")
        self.create_documents_frame("bl", "Bon de livraison")
        self.create_documents_frame("factures", "Factures")
        self.create_documents_frame("reception", "Bon de réception")
        self.create_paiements_frame()
        # Exportation removed from sidebar by request.
        # Administration will open in a separate settings window instead of a frame.

    def show_frame(self, frame_name):
        # Cacher le frame actuel s'il existe
        if self.current_frame:
            self.current_frame.pack_forget()

        # Afficher le nouveau frame
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

    def _setup_ttk_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # Base treeview style
        style.configure(
            "Treeview",
            rowheight=self.ui["tree_rowheight"],
            font=self.ui["font_body"],
            background=self.ui.get("tree_odd", "#F6F8FB"),
            foreground=self.ui.get("tree_text", "#000000"),
            fieldbackground=self.ui.get("tree_even", "#FFFFFF"),
        )

        # Heading style: bolder, padded, with subtle background to separate from rows
        style.configure(
            "Treeview.Heading",
            font=(self.ui["font_family"], 11, "bold"),
            padding=(8, 8),
            background="#F0F3F7",
            foreground="#1f2d3d",
            relief="flat",
            borderwidth=0,
        )

        # Map hover/pressed appearance for headings (if supported by theme)
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
        for idx, item in enumerate(tree.get_children()):
            tree.item(item, tags=("even" if idx % 2 == 0 else "odd",))

    def open_admin_settings(self):
        """Open administration settings in a separate French-titled window."""
        try:
            admin_win = ctk.CTkToplevel(self)
            # store reference so we can refresh/reopen it after destructive actions
            try:
                self._admin_win = admin_win
            except Exception:
                pass
            admin_win.title("Paramètres d'administration")
            admin_win.geometry("900x600")
            admin_win.transient(self)
            admin_win.grab_set()

            # ensure we clear stored ref when admin window is closed
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

            # Main layout: header + content area with left sidebar and right detail
            header = ctk.CTkFrame(admin_win, fg_color="transparent")
            header.pack(fill="x", padx=16, pady=(12, 6))
            ctk.CTkLabel(
                header,
                text="Paramètres d'administration",
                font=self.ui.get("font_h1", ("Arial", 24, "bold")),
            ).pack(side="left")

            content = ctk.CTkFrame(admin_win, fg_color="transparent")
            content.pack(fill="both", expand=True, padx=16, pady=8)

            # Left sidebar
            sidebar = ctk.CTkFrame(content, width=240, fg_color="transparent")
            sidebar.pack(side="left", fill="y", padx=(0, 12), pady=6)

            # Detail area
            detail = ctk.CTkFrame(content, fg_color="transparent")
            detail.pack(side="right", fill="both", expand=True, pady=6)

            # Sidebar buttons
            sections = [
                ("Utilisateurs", "users"),
                ("Paramètres système", "system"),
            ]

            btns = {}

            def clear_detail():
                for w in detail.winfo_children():
                    w.destroy()

            def show_users_section():
                clear_detail()
                ctk.CTkLabel(detail, text="Utilisateurs", font=self.ui.get("font_h2")).pack(anchor="w", pady=(6, 8))
                # Users list (read-only)
                tree_frame = ctk.CTkFrame(detail, fg_color="transparent")
                tree_frame.pack(fill="both", expand=True)
                cols = ("ID", "Nom d'utilisateur", "Email", "Nom complet", "Admin")
                users_tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
                for c in cols:
                    users_tree.heading(c, text=c)
                    users_tree.column(c, width=120, anchor="center")
                users_tree.column("Email", width=200, anchor="w")
                scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=users_tree.yview)
                scrollbar.pack(side="right", fill="y")
                users_tree.configure(yscrollcommand=scrollbar.set)
                users_tree.pack(fill="both", expand=True)

                # Load users from DB
                try:
                    conn = sqlite3.connect(self.db_path)
                    cur = conn.cursor()
                    cur.execute("SELECT id, username, email, full_name, is_admin FROM users ORDER BY username")
                    for r in cur.fetchall():
                        users_tree.insert("", "end", values=(r[0], r[1], r[2] or "", r[3] or "", "Oui" if r[4] else "Non"))
                except Exception as e:
                    print(f"Failed to load users for admin panel: {e}")
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass

                footer = ctk.CTkFrame(detail, fg_color="transparent")
                footer.pack(fill="x", pady=(8, 0))
                ctk.CTkButton(footer, text="Créer un utilisateur", state="disabled").pack(side="left")
                ctk.CTkButton(footer, text="Modifier (sélection)", state="disabled").pack(side="left", padx=8)

            def show_system_section():
                clear_detail()
                ctk.CTkLabel(detail, text="Paramètres système", font=self.ui.get("font_h2")).pack(anchor="w", pady=(6, 8))
                sys_frame = ctk.CTkFrame(detail, fg_color="transparent")
                sys_frame.pack(fill="both", expand=True)

                ctk.CTkLabel(sys_frame, text="Sauvegarde / Restauration", font=self.ui.get("font_body")).pack(anchor="w", pady=(4, 2))
                ctk.CTkButton(sys_frame, text="Sauvegarder la base de données", command=self._admin_backup_db).pack(anchor="w")
                ctk.CTkButton(sys_frame, text="Restaurer la base de données", command=self._admin_restore_db).pack(anchor="w", pady=(6, 0))

                # Wipe DB (danger) — backup first, then recreate schema
                ctk.CTkLabel(sys_frame, text="", font=self.ui.get("font_body")).pack(pady=(8, 0))
                def _wipe_confirm():
                    if not messagebox.askokcancel(
                        "Effacer la base de données",
                        "Ceci va sauvegarder la base actuelle puis la supprimer définitivement. Continuer ?",
                    ):
                        return
                    self._admin_wipe_db()

                ctk.CTkButton(
                    sys_frame,
                    text="Effacer la base de données",
                    command=_wipe_confirm,
                    fg_color=self.ui.get("danger_fg", "#FF4B4B"),
                    hover_color=self.ui.get("danger_hover", "#FF0000"),
                ).pack(anchor="w", pady=(10, 0))

                ctk.CTkLabel(sys_frame, text="Options", font=self.ui.get("font_body")).pack(anchor="w", pady=(12, 2))
                # Example toggle: reservation behaviour (placeholder)
                def _toggle_reserve_var():
                    # placeholder: no persistent behavior implemented yet
                    pass

                reserve_frame = ctk.CTkFrame(sys_frame, fg_color="transparent")
                reserve_frame.pack(anchor="w", pady=(4, 2))
                ctk.CTkLabel(reserve_frame, text="Activer réservation automatique pour Devis", font=self.ui.get("font_body")).pack(side="left")
                ctk.CTkSwitch(reserve_frame, command=_toggle_reserve_var).pack(side="left", padx=8)

            # Create sidebar buttons and wire them
            for text, key in sections:
                b = ctk.CTkButton(sidebar, text=text, anchor="w", width=220, corner_radius=8)
                b.pack(fill="x", pady=6)
                btns[key] = b

            # Assign commands after creation to avoid late-binding issues
            btns["users"].configure(command=show_users_section)
            btns["system"].configure(command=show_system_section)

            # Default view
            show_users_section()

            # Bottom actions
            bottom = ctk.CTkFrame(admin_win, fg_color="transparent")
            bottom.pack(fill="x", padx=16, pady=10)
            ctk.CTkButton(bottom, text="Fermer", command=admin_win.destroy, fg_color="gray", hover_color="darkgray").pack(side="right")
        except Exception as e:
            print(f"Failed to open admin settings: {e}")

    def open_about_dialog(self):
        try:
            dialog = ctk.CTkToplevel(self)
            dialog.title("À propos - GCS")
            dialog.geometry("420x220")
            dialog.transient(self)
            dialog.grab_set()

            ctk.CTkLabel(dialog, text="GCS", font=("Arial", 18, "bold")).pack(pady=(16, 4))
            ctk.CTkLabel(dialog, text="Version: 0.1.0 beta", font=("Arial", 12)).pack(pady=(0, 8))

            email_lbl = ctk.CTkLabel(dialog, text="sedikdarragi@gmail.com", text_color="blue", cursor="hand2", font=("Arial", 11, "underline"))
            email_lbl.pack(pady=(4, 2))
            email_lbl.bind("<Button-1>", lambda e: webbrowser.open("mailto:sedikdarragi@gmail.com"))

            gh_lbl = ctk.CTkLabel(dialog, text="https://github.com/SedikDarragi", text_color="blue", cursor="hand2", font=("Arial", 11, "underline"))
            gh_lbl.pack(pady=(2, 8))
            gh_lbl.bind("<Button-1>", lambda e: webbrowser.open("https://github.com/SedikDarragi"))

            ctk.CTkButton(dialog, text="Fermer", command=dialog.destroy, fg_color="gray", hover_color="darkgray").pack(pady=8)
        except Exception:
            messagebox.showinfo("À propos", "GCS\nVersion: 0.1.0 beta\nContact: sedikdarragi@gmail.com")

    def _admin_wipe_db(self):
        """Optionally backup current DB, delete it, and reinitialize schema, then refresh UI in real time."""
        # Ask the user whether to create a backup before wiping
        choice = messagebox.askyesnocancel(
            "Effacer la base de données",
            "Voulez-vous créer une sauvegarde avant d'effacer la base ?\n\n"
            "Oui = sauvegarder puis effacer, Non = effacer sans sauvegarde, Annuler = annuler.",
        )

        if choice is None:
            # Cancel
            return

        backup_path = None
        if choice is True:
            # Ask where to save the backup
            path = filedialog.asksaveasfilename(
                defaultextension=".db",
                filetypes=[("SQLite DB", "*.db"), ("All files", "*.*")],
                initialfile=f"{os.path.basename(self.db_path)}.backup",
            )
            if not path:
                # user cancelled save dialog -> abort wipe
                return
            try:
                shutil.copyfile(self.db_path, path)
                backup_path = path
            except Exception as e:
                messagebox.showerror("Sauvegarde", f"Échec de la sauvegarde: {e}")
                return

        # proceed to remove database file
        try:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
        except Exception as e:
            messagebox.showerror("Effacer DB", f"Impossible de supprimer la base: {e}")
            return

        # reinitialize schema
        try:
            self.init_db()
        except Exception as e:
            messagebox.showerror("Effacer DB", f"Erreur lors de la réinitialisation: {e}")
            return

        # Refresh main UI frames in real time
        try:
            # reload articles
            try:
                self.load_articles()
            except Exception:
                pass
            # reload clients
            try:
                if hasattr(self, "load_clients"):
                    self.load_clients()
            except Exception:
                pass
            # reload documents for common types
            for t in ("devis", "bl", "factures", "reception"):
                try:
                    self.load_documents(t)
                except Exception:
                    pass
            # reload paiements/impayés view
            try:
                self.load_paiements()
            except Exception:
                pass
        except Exception:
            pass

        # If admin settings window was open, reopen it so it reflects new DB
        try:
            if getattr(self, "_admin_win", None) is not None:
                try:
                    # close existing admin window
                    self._admin_win.destroy()
                except Exception:
                    pass
                # clear reference and reopen fresh
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

        msg = "Base réinitialisée avec succès."
        if backup_path:
            msg += f" Une sauvegarde a été créée: {backup_path}"
        else:
            msg += " Aucune sauvegarde n'a été créée."

        messagebox.showinfo("Effacer DB", msg)

    def _admin_backup_db(self):
        try:
            path = filedialog.asksaveasfilename(
                defaultextension=".db",
                filetypes=[("SQLite DB", "*.db"), ("All files", "*.*")],
                initialfile=os.path.basename(self.db_path),
            )
            if not path:
                return
            shutil.copyfile(self.db_path, path)
            messagebox.showinfo("Sauvegarde", f"Sauvegarde effectuée:\n{path}")
        except Exception as e:
            messagebox.showerror("Sauvegarde", f"Échec de la sauvegarde: {e}")

    def _admin_restore_db(self):
        try:
            src = filedialog.askopenfilename(
                filetypes=[("SQLite DB", "*.db"), ("All files", "*.*")]
            )
            if not src:
                return
            if not messagebox.askokcancel(
                "Restaurer", "Restaurer la base de données remplacera la base actuelle. Continuer?"
            ):
                return
            # Overwrite current DB file
            shutil.copyfile(src, self.db_path)
            messagebox.showinfo("Restaurer", "Restauration effectuée. Redémarrez l'application pour recharger la base.")
        except Exception as e:
            messagebox.showerror("Restaurer", f"Échec de la restauration: {e}")

    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT,
                    designation TEXT NOT NULL,
                    prix_achat REAL NOT NULL DEFAULT 0,
                    prix_vente REAL NOT NULL DEFAULT 0,
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
                    adresse TEXT
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
                    total_ht REAL NOT NULL DEFAULT 0,
                    total_tva REAL NOT NULL DEFAULT 0,
                    total_ttc REAL NOT NULL DEFAULT 0,
                    paid_amount REAL NOT NULL DEFAULT 0,
                    payment_mode TEXT NOT NULL DEFAULT 'none',
                    echeance TEXT,
                    source_bl_id INTEGER,
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
                    prix_unitaire REAL NOT NULL DEFAULT 0,
                    tva REAL NOT NULL DEFAULT 0,
                    total_ht REAL NOT NULL DEFAULT 0,
                    total_tva REAL NOT NULL DEFAULT 0,
                    total_ttc REAL NOT NULL DEFAULT 0,
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
                    amount REAL NOT NULL,
                    method TEXT,
                    note TEXT,
                    FOREIGN KEY(document_id) REFERENCES documents(id)
                )
                """
            )

            # Migrations légères (si DB existante)
            for ddl in [
                "ALTER TABLE document_lignes ADD COLUMN article_id INTEGER",
                "ALTER TABLE articles ADD COLUMN prix_achat REAL NOT NULL DEFAULT 0",
                "ALTER TABLE articles ADD COLUMN prix_vente REAL NOT NULL DEFAULT 0",
                "ALTER TABLE articles ADD COLUMN tva REAL NOT NULL DEFAULT 0",
                "ALTER TABLE documents ADD COLUMN total_ht REAL NOT NULL DEFAULT 0",
                "ALTER TABLE documents ADD COLUMN total_tva REAL NOT NULL DEFAULT 0",
                "ALTER TABLE documents ADD COLUMN total_ttc REAL NOT NULL DEFAULT 0",
                "ALTER TABLE documents ADD COLUMN paid_amount REAL NOT NULL DEFAULT 0",
                "ALTER TABLE documents ADD COLUMN payment_mode TEXT NOT NULL DEFAULT 'none'",
                "ALTER TABLE documents ADD COLUMN echeance TEXT",
                "ALTER TABLE document_lignes ADD COLUMN tva REAL NOT NULL DEFAULT 0",
                "ALTER TABLE document_lignes ADD COLUMN total_ht REAL NOT NULL DEFAULT 0",
                "ALTER TABLE document_lignes ADD COLUMN total_tva REAL NOT NULL DEFAULT 0",
                "ALTER TABLE document_lignes ADD COLUMN total_ttc REAL NOT NULL DEFAULT 0",
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

            conn.commit()
        finally:
            conn.close()

    def get_next_numero(self, doc_type):
        annee = datetime.now().year
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT annee, compteur FROM sequences WHERE type = ?", (doc_type,)
            )
            row = cursor.fetchone()
            if not row:
                compteur = 1
                cursor.execute(
                    "INSERT INTO sequences (type, annee, compteur) VALUES (?, ?, ?)",
                    (doc_type, annee, compteur),
                )
            else:
                row_annee, row_compteur = row
                if row_annee != annee:
                    compteur = 1
                    cursor.execute(
                        "UPDATE sequences SET annee = ?, compteur = ? WHERE type = ?",
                        (annee, compteur, doc_type),
                    )
                else:
                    compteur = row_compteur + 1
                    cursor.execute(
                        "UPDATE sequences SET compteur = ? WHERE type = ?",
                        (compteur, doc_type),
                    )

            conn.commit()
        finally:
            conn.close()

        prefix = {
            "devis": "DEV",
            "bl": "BL",
            "factures": "FAC",
            "reception": "BR",
        }.get(doc_type, doc_type.upper())

        return f"{prefix}-{annee}-{compteur:04d}"

    def create_articles_frame(self):
        frame = ctk.CTkFrame(self.main_frame, corner_radius=0, fg_color="transparent")

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 20))

        ctk.CTkLabel(header, text="Articles", font=self.ui["font_h1"]).pack(side="left")

        ctk.CTkButton(
            header, text="+ Ajouter un article", command=self.show_add_article_dialog
        ).pack(side="right")

        search_frame = ctk.CTkFrame(frame, fg_color="transparent")
        search_frame.pack(fill="x", pady=(0, 20))

        self.article_search_entry = ttk.Combobox(
            search_frame, values=[], width=48, state="normal"
        )
        self.article_search_entry.pack(side="left", padx=(0, 10))
        self.article_search_entry.bind("<Return>", self.search_articles)
        self.article_search_entry.bind(
            "<KeyRelease>", self._update_article_search_suggestions
        )
        self.article_search_entry.bind("<<ComboboxSelected>>", self.search_articles)

        ctk.CTkButton(
            search_frame, text="Rechercher", command=self.search_articles
        ).pack(side="left")

        self._update_article_search_suggestions()

        columns = (
            "ID",
            "Référence",
            "Désignation",
            "PA",
            "PV",
            "TVA%",
            "Marge",
            "Stock",
        )
        self.articles_tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse"
        )

        self.articles_tree.heading("ID", text="ID", anchor="center")
        self.articles_tree.heading("Référence", text="Référence", anchor="w")
        self.articles_tree.heading("Désignation", text="Désignation", anchor="w")
        self.articles_tree.heading("PA", text="Prix Achat", anchor="e")
        self.articles_tree.heading("PV", text="Prix Vente", anchor="e")
        self.articles_tree.heading("TVA%", text="TVA%", anchor="center")
        self.articles_tree.heading("Marge", text="Marge", anchor="e")
        self.articles_tree.heading("Stock", text="Stock", anchor="center")

        self.articles_tree.column("ID", width=60, anchor="center")
        self.articles_tree.column("Référence", width=140, anchor="w")
        self.articles_tree.column("Désignation", width=340, anchor="w")
        self.articles_tree.column("PA", width=120, anchor="e")
        self.articles_tree.column("PV", width=120, anchor="e")
        self.articles_tree.column("TVA%", width=80, anchor="center")
        self.articles_tree.column("Marge", width=120, anchor="e")
        self.articles_tree.column("Stock", width=100, anchor="center")

        scrollbar = ttk.Scrollbar(
            frame, orient="vertical", command=self.articles_tree.yview
        )
        scrollbar.pack(side="right", fill="y")
        self.articles_tree.configure(yscrollcommand=scrollbar.set)
        self.articles_tree.pack(fill="both", expand=True)

        self.articles_tree.bind("<Double-1>", self.on_article_double_click)
        self.load_articles()
        self.frames["articles"] = frame

    def load_articles(self, search_query=None):
        for item in self.articles_tree.get_children():
            self.articles_tree.delete(item)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            if search_query:
                q = str(search_query).strip()
                if " - " in q:
                    left, right = q.split(" - ", 1)
                    left = left.strip()
                    right = right.strip()
                    cursor.execute(
                        """
                        SELECT id, reference, designation, prix_achat, prix_vente, tva, stock
                        FROM articles
                        WHERE reference LIKE ? OR designation LIKE ?
                        ORDER BY designation
                        """,
                        (f"%{left}%", f"%{right}%"),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT id, reference, designation, prix_achat, prix_vente, tva, stock
                        FROM articles
                        WHERE reference LIKE ? OR designation LIKE ?
                        ORDER BY designation
                        """,
                        (f"%{q}%", f"%{q}%"),
                    )
            else:
                cursor.execute(
                    "SELECT id, reference, designation, prix_achat, prix_vente, tva, stock FROM articles ORDER BY designation"
                )

            for row in cursor.fetchall():
                pa = float(row[3] or 0)
                pv = float(row[4] or 0)
                tva = float(row[5] or 0)
                marge = pv - pa
                self.articles_tree.insert(
                    "",
                    "end",
                    values=(
                        row[0],
                        row[1] or "",
                        row[2] or "",
                        f"{pa:.2f}",
                        f"{pv:.2f}",
                        f"{tva:.2f}",
                        f"{marge:.2f}",
                        row[6] if row[6] is not None else 0,
                    ),
                )
        finally:
            conn.close()

        self._apply_zebra(self.articles_tree)

    def search_articles(self, event=None):
        query = self.article_search_entry.get().strip()
        if not query:
            values = getattr(self.article_search_entry, "cget", lambda _k: [])("values")
            if values:
                self.article_search_entry.set(values[0])
                query = str(values[0]).strip()
        self.load_articles(query if query else None)

    def show_add_article_dialog(self, article_id=None):
        dialog = ctk.CTkToplevel(self)
        dialog.title(
            "Ajouter un article" if article_id is None else "Modifier un article"
        )
        dialog.geometry("980x700")
        dialog.transient(self)
        dialog.grab_set()

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
                    pa_value = str(row[2] if row[2] is not None else 0)
                    pv_value = str(row[3] if row[3] is not None else 0)
                    tva_value = str(row[4] if row[4] is not None else 0)
                    stock_value = str(row[5] if row[5] is not None else 0)
            finally:
                conn.close()

        # 1. Header Frame (Title + Actions placeholder)
        header = ctk.CTkFrame(dialog, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 10))

        ctk.CTkLabel(
            header,
            text="Fiche Article",
            font=self.ui["font_h1"],
        ).pack(side="left")

        actions = ctk.CTkFrame(header, fg_color="transparent")
        actions.pack(side="right")
        # Dummy button to match "Exporter / Imprimer" position
        ctk.CTkButton(
            actions,
            text="Historique",
            state="disabled",
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "gray90"),
        ).pack(side="right")

        # 2. Top Info Frame (Ref, Stock, Designation)
        top = ctk.CTkFrame(dialog, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(0, 10))

        left = ctk.CTkFrame(top, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        right = ctk.CTkFrame(top, fg_color="transparent")
        right.pack(side="right")

        # Row 0: Reference and Stock Labels
        ctk.CTkLabel(left, text="Référence", font=self.ui["font_h2"]).grid(
            row=0, column=0, sticky="w"
        )
        ctk.CTkLabel(left, text="Stock", font=self.ui["font_h2"]).grid(
            row=0, column=1, sticky="w"
        )

        # Row 1: Entries
        ref_entry = ctk.CTkEntry(left, width=220)
        ref_entry.insert(0, ref_value)
        ref_entry.grid(row=1, column=0, sticky="w", pady=(0, 8), padx=(0, 16))

        stock_entry = ctk.CTkEntry(left, width=220)
        stock_entry.insert(0, stock_value)
        stock_entry.grid(row=1, column=1, sticky="w", pady=(0, 8))

        # Row 2: Designation Label
        ctk.CTkLabel(left, text="Désignation", font=self.ui["font_h2"]).grid(
            row=2, column=0, sticky="w", pady=(8, 0)
        )

        # Row 3: Designation Entry (wide)
        des_entry = ctk.CTkEntry(left, width=460)
        des_entry.insert(0, des_value)
        des_entry.grid(row=3, column=0, columnspan=2, sticky="w")

        # Right side status (dummy)
        statut_label = ctk.CTkLabel(
            right, text="Statut: Actif", font=self.ui["font_h2"]
        )
        statut_label.pack(anchor="e")

        # 3. Content Area (Treeview)
        content = ctk.CTkFrame(dialog, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=20, pady=(10, 10))

        columns = ("Date", "Mouvement", "Qté", "Utilisateur")
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

        self._apply_zebra(lines_tree)

        # 4. Totals Bar (Pricing)
        totals_bar = ctk.CTkFrame(dialog, fg_color="transparent")
        totals_bar.pack(fill="x", padx=20, pady=(0, 10))

        totals_right = ctk.CTkFrame(totals_bar, fg_color="transparent")
        totals_right.pack(side="right")

        # Prix Vente (mimics TTC)
        pv_box = ctk.CTkFrame(totals_right, fg_color="transparent")
        pv_box.pack(side="right", padx=(0, 12))
        ctk.CTkLabel(
            pv_box,
            text="Prix Vente",
            font=self.ui["font_body"],
            text_color=("gray35", "gray70"),
        ).pack(anchor="e")
        pv_entry = ctk.CTkEntry(
            pv_box, width=150, font=self.ui["font_h2"], justify="right"
        )
        pv_entry.insert(0, pv_value)
        pv_entry.pack(anchor="e")

        # TVA (mimics TVA)
        tva_box = ctk.CTkFrame(totals_right, fg_color="transparent")
        tva_box.pack(side="right", padx=(0, 12))
        ctk.CTkLabel(
            tva_box,
            text="TVA %",
            font=self.ui["font_body"],
            text_color=("gray35", "gray70"),
        ).pack(anchor="e")
        tva_entry = ctk.CTkEntry(
            tva_box, width=100, font=self.ui["font_h2"], justify="right"
        )
        tva_entry.insert(0, tva_value)
        tva_entry.pack(anchor="e")

        # Prix Achat (mimics HT)
        pa_box = ctk.CTkFrame(totals_right, fg_color="transparent")
        pa_box.pack(side="right")
        ctk.CTkLabel(
            pa_box,
            text="Prix Achat",
            font=self.ui["font_body"],
            text_color=("gray35", "gray70"),
        ).pack(anchor="e")
        pa_entry = ctk.CTkEntry(
            pa_box, width=150, font=self.ui["font_h2"], justify="right"
        )
        pa_entry.insert(0, pa_value)
        pa_entry.pack(anchor="e")

        # 5. Toolbar (Bottom buttons in invoice details: Add/Edit/Delete line)
        toolbar = ctk.CTkFrame(dialog, fg_color="transparent")
        toolbar.pack(fill="x", padx=20, pady=(0, 10))

        add_btn = ctk.CTkButton(
            toolbar,
            text="+ Mouvement Stock",
            state="disabled",
            fg_color="transparent",
            border_width=1,
            text_color=("gray", "gray"),
        )
        add_btn.pack(side="left")

        # 6. Footer (Close/Save)
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
                messagebox.showerror("Erreur", "La désignation est obligatoire")
                return

            try:
                pa_f = float(pa) if pa else 0.0
                pv_f = float(pv) if pv else 0.0
                tva_f = float(tva) if tva else 0.0
                stock_i = int(stock) if stock else 0
            except ValueError:
                messagebox.showerror("Erreur", "Prix/Stock invalides")
                return

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            try:
                if article_id is None:
                    cursor.execute(
                        "INSERT INTO articles (reference, designation, prix_achat, prix_vente, tva, stock) VALUES (?, ?, ?, ?, ?, ?)",
                        (reference or None, designation, pa_f, pv_f, tva_f, stock_i),
                    )
                else:
                    cursor.execute(
                        "UPDATE articles SET reference = ?, designation = ?, prix_achat = ?, prix_vente = ?, tva = ?, stock = ? WHERE id = ?",
                        (
                            reference or None,
                            designation,
                            pa_f,
                            pv_f,
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
            text="Fermer",
            command=dialog.destroy,
            fg_color="gray",
            hover_color="darkgray",
        ).pack(side="right", padx=10)
        ctk.CTkButton(footer, text="Enregistrer", command=save_article).pack(
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

        ctk.CTkLabel(header, text=title, font=self.ui["font_h1"]).pack(side="left")

        actions_left = ctk.CTkFrame(header, fg_color="transparent")
        actions_left.pack(side="right")

        # Boutons spécifiques au workflow
        if key == "devis":
            ctk.CTkButton(
                actions_left,
                text="Convertir → BL",
                command=lambda k=key: self.convert_selected_document(k, "bl"),
            ).pack(side="right", padx=(10, 0))
        elif key == "bl":
            ctk.CTkButton(
                actions_left,
                text="Convertir → Facture (BLV)",
                command=lambda k=key: self.convert_selected_document(k, "factures"),
            ).pack(side="right", padx=(10, 0))
            ctk.CTkButton(
                actions_left,
                text="Valider (sortie stock)",
                command=lambda k=key: self.validate_selected_document(k),
            ).pack(side="right", padx=(10, 0))
        elif key == "reception":
            ctk.CTkButton(
                actions_left,
                text="Valider (entrée stock)",
                command=lambda k=key: self.validate_selected_document(k),
            ).pack(side="right", padx=(10, 0))

        ctk.CTkButton(
            actions_left,
            text=f"+ Nouveau {title}",
            command=lambda k=key, t=title: self.show_new_document_dialog(k, t),
        ).pack(side="right")

        columns = (
            "ID",
            "Numéro",
            "Date",
            "Client",
            "Statut",
            "HT",
            "TVA",
            "TTC",
            "Payé",
            "Reste",
        )
        tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse"
        )
        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=140, anchor="center")

        tree.column("Client", width=240, anchor="w")
        tree.column("HT", width=110, anchor="e")
        tree.column("TVA", width=110, anchor="e")
        tree.column("TTC", width=110, anchor="e")
        tree.column("Payé", width=110, anchor="e")
        tree.column("Reste", width=110, anchor="e")

        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        scrollbar.pack(side="right", fill="y")
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(fill="both", expand=True)

        tree.bind(
            "<Double-1>",
            lambda e, k=key, tr=tree: self.on_document_double_click(e, k, tr),
        )

        self.documents_trees = getattr(self, "documents_trees", {})
        self.documents_trees[key] = tree
        self.load_documents(key)
        self.frames[key] = frame

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
                SELECT d.id, d.numero, d.date, COALESCE(c.nom, ''), d.statut,
                       d.total_ht, d.total_tva, d.total_ttc, COALESCE(d.paid_amount, 0)
                FROM documents d
                LEFT JOIN clients c ON c.id = d.client_id
                WHERE d.type = ?
                ORDER BY d.date DESC, d.id DESC
                """,
                (doc_type,),
            )
            for row in cursor.fetchall():
                total_ttc = float(row[7] or 0)
                paid = float(row[8] or 0)
                reste = max(0.0, total_ttc - paid)
                tree.insert(
                    "",
                    "end",
                    values=(
                        row[0],
                        row[1],
                        row[2],
                        row[3],
                        row[4],
                        f"{(row[5] or 0):.2f}",
                        f"{(row[6] or 0):.2f}",
                        f"{total_ttc:.2f}",
                        f"{paid:.2f}",
                        f"{reste:.2f}",
                    ),
                )
        finally:
            conn.close()

        self._apply_zebra(tree)

    def show_new_document_dialog(self, doc_type, title):
        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Nouveau {title}")
        dialog.geometry("800x600")
        dialog.transient(self)
        dialog.grab_set()

        top = ctk.CTkFrame(dialog, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=15)

        numero = self.get_next_numero(doc_type)
        ctk.CTkLabel(top, text=f"Numéro: {numero}", font=("Arial", 14, "bold")).pack(
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
        ctk.CTkLabel(client_frame, text="Client").pack(side="left", padx=(0, 10))
        client_combo = ttk.Combobox(
            client_frame, values=client_names, state="readonly", width=40
        )
        client_combo.pack(side="left")

        lines_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        lines_frame.pack(fill="both", expand=True, padx=20, pady=15)

        columns = ("ArticleId", "Article", "Quantité", "PU", "TVA%")
        lines_tree = ttk.Treeview(
            lines_frame, columns=columns, show="headings", selectmode="browse"
        )
        for col in columns:
            lines_tree.heading(col, text=col)
            lines_tree.column(col, width=220, anchor="center")
        lines_tree.column("ArticleId", width=0, stretch=False)
        lines_tree.heading("ArticleId", text="")
        lines_tree.pack(fill="both", expand=True)

        def add_line():
            dlg = ctk.CTkToplevel(dialog)
            dlg.title("Ajouter une ligne")
            dlg.geometry("560x420")
            dlg.transient(dialog)
            dlg.grab_set()

            header = ctk.CTkFrame(dlg, fg_color="transparent")
            header.pack(fill="x", padx=20, pady=(15, 10))
            ctk.CTkLabel(
                header, text="Ajouter une ligne", font=self.ui["font_h2"]
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
            price_map = {a[1]: float(a[2] or 0) for a in articles}
            tva_map = {a[1]: float(a[3] or 0) for a in articles}

            ctk.CTkLabel(form, text="Article", font=self.ui["font_body"]).grid(
                row=0, column=0, columnspan=2, sticky="w"
            )
            art_combo = ttk.Combobox(form, values=names, state="readonly", width=58)
            art_combo.grid(row=1, column=0, columnspan=2, sticky="we", pady=(0, 10))

            ctk.CTkLabel(form, text="Quantité", font=self.ui["font_body"]).grid(
                row=2, column=0, sticky="w"
            )
            qty_var = ctk.StringVar(value="1")
            qty_entry = ctk.CTkEntry(form, textvariable=qty_var)
            qty_entry.grid(row=3, column=0, sticky="we", pady=(0, 10), padx=(0, 10))

            ctk.CTkLabel(form, text="Prix unitaire", font=self.ui["font_body"]).grid(
                row=2, column=1, sticky="w"
            )
            pu_var = ctk.StringVar(value="0")
            pu_entry = ctk.CTkEntry(form, textvariable=pu_var)
            pu_entry.grid(row=3, column=1, sticky="we", pady=(0, 10))

            ctk.CTkLabel(form, text="TVA %", font=self.ui["font_body"]).grid(
                row=4, column=0, sticky="w"
            )
            tva_var = ctk.StringVar(value="0")
            tva_entry = ctk.CTkEntry(form, textvariable=tva_var)
            tva_entry.grid(row=5, column=0, sticky="we", pady=(0, 10), padx=(0, 10))

            preview = ctk.CTkFrame(form, fg_color="transparent")
            preview.grid(row=5, column=1, sticky="we", pady=(0, 10))
            ctk.CTkLabel(
                preview,
                text="Aperçu",
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
                    qty_f = float(qty_entry.get().strip() or 0)
                    pu_f = float(pu_entry.get().strip() or 0)
                    tva_f = float(tva_entry.get().strip() or 0)
                except Exception:
                    prev_ht_label.configure(text="HT: -")
                    prev_tva_label.configure(text="TVA: -")
                    prev_ttc_label.configure(text="TTC: -")
                    return
                line_ht = qty_f * pu_f
                line_tva = line_ht * (tva_f / 100.0)
                line_ttc = line_ht + line_tva
                prev_ht_label.configure(text=f"HT: {line_ht:.2f}")
                prev_tva_label.configure(text=f"TVA: {line_tva:.2f}")
                prev_ttc_label.configure(text=f"TTC: {line_ttc:.2f}")

            def on_select(event=None):
                name = art_combo.get()
                if name in price_map:
                    pu_var.set(str(price_map[name]))
                if name in tva_map:
                    tva_var.set(str(tva_map[name]))

                update_preview()

            art_combo.bind("<<ComboboxSelected>>", on_select)
            qty_var.trace_add("write", lambda *_: update_preview())
            pu_var.trace_add("write", lambda *_: update_preview())
            tva_var.trace_add("write", lambda *_: update_preview())
            qty_entry.bind("<FocusOut>", update_preview)
            pu_entry.bind("<FocusOut>", update_preview)
            tva_entry.bind("<FocusOut>", update_preview)
            update_preview()

            def save_line():
                art = art_combo.get().strip()
                if not art:
                    messagebox.showerror("Erreur", "Choisis un article")
                    return
                try:
                    qty = float(qty_entry.get().strip())
                    pu = float(pu_entry.get().strip())
                    tva = float(tva_entry.get().strip())
                except ValueError:
                    messagebox.showerror("Erreur", "Quantité / PU invalides")
                    return
                article_id = id_map.get(art)
                lines_tree.insert("", "end", values=(article_id, art, qty, pu, tva))
                recalc_total()
                dlg.destroy()

            btns = ctk.CTkFrame(dlg, fg_color="transparent")
            btns.pack(fill="x", padx=20, pady=(0, 15))
            ctk.CTkButton(
                btns,
                text="Annuler",
                command=dlg.destroy,
                fg_color="gray",
                hover_color="darkgray",
            ).pack(side="right", padx=10)
            ctk.CTkButton(btns, text="Ajouter", command=save_line).pack(side="right")

        actions = ctk.CTkFrame(dialog, fg_color="transparent")
        actions.pack(fill="x", padx=20, pady=(0, 10))
        add_line_btn = ctk.CTkButton(
            actions, text="+ Ajouter une ligne", command=add_line
        )
        add_line_btn.pack(side="left")
        add_line_btn.configure(text="+ Ajouter une ligne")

        totals = ctk.CTkFrame(dialog, fg_color="transparent")
        totals.pack(fill="x", padx=20, pady=(0, 5))

        ctk.CTkLabel(totals, text="HT:").pack(side="right", padx=(10, 5))
        lbl_ht_val = ctk.CTkLabel(totals, text="0.00", font=("Arial", 14, "bold"))
        lbl_ht_val.pack(side="right")

        ctk.CTkLabel(totals, text="TVA:").pack(side="right", padx=(10, 5))
        lbl_tva_val = ctk.CTkLabel(totals, text="0.00", font=("Arial", 14, "bold"))
        lbl_tva_val.pack(side="right")

        ctk.CTkLabel(totals, text="TTC:").pack(side="right", padx=(10, 5))
        lbl_ttc_val = ctk.CTkLabel(totals, text="0.00", font=("Arial", 14, "bold"))
        lbl_ttc_val.pack(side="right")

        def recalc_total():
            ht = 0.0
            tva_sum = 0.0
            for item in lines_tree.get_children():
                _article_id, art, qty, pu, tva = lines_tree.item(item, "values")
                line_ht = float(qty) * float(pu)
                line_tva = line_ht * (float(tva) / 100.0)
                ht += line_ht
                tva_sum += line_tva

            ttc = ht + tva_sum
            lbl_ht_val.configure(text=f"{ht:.2f}")
            lbl_tva_val.configure(text=f"{tva_sum:.2f}")
            lbl_ttc_val.configure(text=f"{ttc:.2f}")

        recalc_total()

        def save_document():
            client_name = client_combo.get().strip()
            client_id = client_map.get(client_name) if client_name else None

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            try:
                doc_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                total_ht = float(lbl_ht_val.cget("text"))
                total_tva = float(lbl_tva_val.cget("text"))
                total_ttc = float(lbl_ttc_val.cget("text"))
                cursor.execute(
                    """
                    INSERT INTO documents (type, numero, date, client_id, statut, total_ht, total_tva, total_ttc, paid_amount, payment_mode)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        doc_type,
                        numero,
                        doc_date,
                        client_id,
                        "brouillon",
                        total_ht,
                        total_tva,
                        total_ttc,
                        0.0,
                        "none",
                    ),
                )
                doc_id = cursor.lastrowid

                for item in lines_tree.get_children():
                    article_id, art, qty, pu, tva = lines_tree.item(item, "values")
                    qty_f = float(qty)
                    pu_f = float(pu)
                    tva_f = float(tva)
                    line_ht = qty_f * pu_f
                    line_tva = line_ht * (tva_f / 100.0)
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
                            pu_f,
                            tva_f,
                            line_ht,
                            line_tva,
                            line_ttc,
                        ),
                    )

                conn.commit()
            finally:
                conn.close()

            self.load_documents(doc_type)
            dialog.destroy()

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=10)
        ctk.CTkButton(
            btn_frame,
            text="Annuler",
            command=dialog.destroy,
            fg_color="gray",
            hover_color="darkgray",
        ).pack(side="right", padx=10)
        ctk.CTkButton(btn_frame, text="Enregistrer", command=save_document).pack(
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
                       COALESCE(c.nom, ''), COALESCE(c.email, ''), COALESCE(c.telephone, ''), COALESCE(c.adresse, '')
                FROM documents d
                LEFT JOIN clients c ON c.id = d.client_id
                WHERE d.id = ?
                """,
                (document_id,),
            )
            doc = cursor.fetchone()
            if not doc:
                messagebox.showerror("Erreur", "Document introuvable")
                return

            cursor.execute("SELECT id, nom FROM clients ORDER BY nom")
            clients = cursor.fetchall()

            cursor.execute(
                """
                SELECT id, article_id, designation, quantite, prix_unitaire, tva
                FROM document_lignes
                WHERE document_id = ?
                ORDER BY id
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
        ) = doc

        title = {
            "devis": "Devis",
            "bl": "Bon de livraison",
            "factures": "Facture",
            "reception": "Bon de réception",
        }.get(doc_type, doc_type)

        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Détails {title} - {numero}")
        dialog.geometry("980x700")
        dialog.transient(self)
        dialog.grab_set()

        is_locked = statut == "valide"

        header = ctk.CTkFrame(dialog, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 10))

        ctk.CTkLabel(header, text=f"{title}", font=self.ui["font_h1"]).pack(side="left")

        actions = ctk.CTkFrame(header, fg_color="transparent")
        actions.pack(side="right")
        ctk.CTkButton(
            actions,
            text="Exporter / Imprimer",
            command=lambda: self.show_export_dialog_for_document(document_id),
        ).pack(side="right")

        top = ctk.CTkFrame(dialog, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(0, 10))

        left = ctk.CTkFrame(top, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        right = ctk.CTkFrame(top, fg_color="transparent")
        right.pack(side="right")

        ctk.CTkLabel(left, text="Numéro", font=self.ui["font_h2"]).grid(
            row=0, column=0, sticky="w"
        )
        numero_entry = ctk.CTkEntry(left, width=220)
        numero_entry.insert(0, str(numero or ""))
        numero_entry.grid(row=1, column=0, sticky="w", pady=(0, 8), padx=(0, 16))

        ctk.CTkLabel(left, text="Date", font=self.ui["font_h2"]).grid(
            row=0, column=1, sticky="w"
        )
        date_entry = ctk.CTkEntry(left, width=220)
        date_entry.insert(0, str(date_str or ""))
        date_entry.grid(row=1, column=1, sticky="w", pady=(0, 8))

        ctk.CTkLabel(left, text="Client", font=self.ui["font_h2"]).grid(
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
                text="(Document validé: modification verrouillée)",
                font=self.ui["font_body"],
                text_color=("#B91C1C", "#EF4444"),
            ).pack(anchor="e", pady=(2, 0))

        content = ctk.CTkFrame(dialog, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=20, pady=(10, 10))

        columns = ("LineId", "ArticleId", "Désignation", "Qté", "PU", "TVA%")
        lines_tree = ttk.Treeview(
            content, columns=columns, show="headings", selectmode="browse"
        )
        for col in columns:
            lines_tree.heading(col, text=col)
            lines_tree.column(col, width=140, anchor="center")
        lines_tree.column("LineId", width=0, stretch=False)
        lines_tree.heading("LineId", text="")
        lines_tree.column("ArticleId", width=0, stretch=False)
        lines_tree.heading("ArticleId", text="")
        lines_tree.column("Désignation", width=420, anchor="w")
        lines_tree.column("Qté", width=90, anchor="e")
        lines_tree.column("PU", width=120, anchor="e")
        lines_tree.column("TVA%", width=90, anchor="e")

        scrollbar = ttk.Scrollbar(content, orient="vertical", command=lines_tree.yview)
        scrollbar.pack(side="right", fill="y")
        lines_tree.configure(yscrollcommand=scrollbar.set)
        lines_tree.pack(fill="both", expand=True)

        for line_id, article_id, des, qty, pu, tva in lines:
            lines_tree.insert(
                "",
                "end",
                values=(
                    int(line_id),
                    int(article_id) if article_id is not None else "",
                    des or "",
                    float(qty or 0),
                    float(pu or 0),
                    float(tva or 0),
                ),
            )
        self._apply_zebra(lines_tree)

        totals_bar = ctk.CTkFrame(dialog, fg_color="transparent")
        totals_bar.pack(fill="x", padx=20, pady=(0, 10))
        total_ht_value = f"{float(total_ht or 0):.2f}"
        total_tva_value = f"{float(total_tva or 0):.2f}"
        total_ttc_value = f"{float(total_ttc or 0):.2f}"

        totals_right = ctk.CTkFrame(totals_bar, fg_color="transparent")
        totals_right.pack(side="right")

        ttc_box = ctk.CTkFrame(totals_right, fg_color="transparent")
        ttc_box.pack(side="right", padx=(0, 12))
        ctk.CTkLabel(
            ttc_box,
            text="TTC",
            font=self.ui["font_body"],
            text_color=("gray35", "gray70"),
        ).pack(anchor="e")
        ttc_value_label = ctk.CTkLabel(
            ttc_box, text=total_ttc_value, font=self.ui["font_h2"]
        )
        ttc_value_label.pack(anchor="e")

        # Payment input area (allow registering a payment for this document)
        payment_frame = ctk.CTkFrame(totals_bar, fg_color="transparent")
        payment_frame.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(payment_frame, text="Enregistrer paiement", font=self.ui.get("font_body")).pack(anchor="w")
        pay_entry = ctk.CTkEntry(payment_frame, width=140, placeholder_text="Montant")
        pay_entry.pack(anchor="w", pady=(4, 6))
        pay_method = ctk.CTkEntry(payment_frame, width=140, placeholder_text="Méthode (opt)")
        pay_method.pack(anchor="w")

        def _save_payment_for_current():
            try:
                amount = float(pay_entry.get().strip())
            except Exception:
                messagebox.showerror("Erreur", "Montant invalide")
                return
            method = pay_method.get().strip() or None
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT total_ttc, paid_amount FROM documents WHERE id = ?",
                    (document_id,),
                )
                row = cursor.fetchone()
                if not row:
                    messagebox.showerror("Erreur", "Document introuvable")
                    return
                total_ttc = float(row[0] or 0)
                paid = float(row[1] or 0)
                new_paid = min(total_ttc, paid + amount)

                date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute(
                    "INSERT INTO payments (document_id, date, amount, method, note) VALUES (?, ?, ?, ?, ?)",
                    (document_id, date_now, amount, method, "Paiement"),
                )
                mode = "partial" if new_paid < total_ttc else "full"
                cursor.execute(
                    "UPDATE documents SET paid_amount = ?, payment_mode = ? WHERE id = ?",
                    (new_paid, mode, document_id),
                )
                conn.commit()
            finally:
                conn.close()

            # refresh views
            pay_entry.delete(0, "end")
            pay_method.delete(0, "end")
            ht_value_label.configure(text=f"{float(total_ht or 0):.2f}")
            tva_value_label.configure(text=f"{float(total_tva or 0):.2f}")
            ttc_value_label.configure(text=f"{float(total_ttc or 0):.2f}")
            self.load_documents(doc_type)

        ctk.CTkButton(payment_frame, text="Enregistrer", command=_save_payment_for_current).pack(pady=(6, 0))

        tva_box = ctk.CTkFrame(totals_right, fg_color="transparent")
        tva_box.pack(side="right", padx=(0, 12))
        ctk.CTkLabel(
            tva_box,
            text="TVA",
            font=self.ui["font_body"],
            text_color=("gray35", "gray70"),
        ).pack(anchor="e")
        tva_value_label = ctk.CTkLabel(
            tva_box, text=total_tva_value, font=self.ui["font_h2"]
        )
        tva_value_label.pack(anchor="e")

        ht_box = ctk.CTkFrame(totals_right, fg_color="transparent")
        ht_box.pack(side="right")
        ctk.CTkLabel(
            ht_box,
            text="HT",
            font=self.ui["font_body"],
            text_color=("gray35", "gray70"),
        ).pack(anchor="e")
        ht_value_label = ctk.CTkLabel(
            ht_box, text=total_ht_value, font=self.ui["font_h2"]
        )
        ht_value_label.pack(anchor="e")

        def recalc_totals_once():
            ht = 0.0
            tva_sum = 0.0
            for item in lines_tree.get_children():
                _line_id, _article_id, _des, qty, pu, tva = lines_tree.item(
                    item, "values"
                )
                try:
                    qty_f = float(qty)
                    pu_f = float(pu)
                    tva_f = float(tva)
                except Exception:
                    continue
                line_ht = qty_f * pu_f
                line_tva = line_ht * (tva_f / 100.0)
                ht += line_ht
                tva_sum += line_tva
            ttc = ht + tva_sum
            ht_value_label.configure(text=f"{ht:.2f}")
            tva_value_label.configure(text=f"{tva_sum:.2f}")
            ttc_value_label.configure(text=f"{ttc:.2f}")

        def _get_selected_line_item():
            sel = lines_tree.selection()
            if not sel:
                return None
            return sel[0]

        # --- Helpers for payments / impayés pane
        def _on_client_selected(self):
            if not hasattr(self, "clients_tree"):
                return
            sel = self.clients_tree.selection()
            if not sel:
                return
            vals = self.clients_tree.item(sel[0], "values")
            if not vals:
                return
            client_id = vals[0]
            try:
                client_id = int(client_id)
            except Exception:
                return
            # load unpaid documents for client
            for item in getattr(self, "client_docs_tree", []).get_children():
                self.client_docs_tree.delete(item)
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    SELECT id, numero, type, total_ttc, COALESCE(paid_amount,0) FROM documents
                    WHERE client_id = ? AND (COALESCE(total_ttc,0) - COALESCE(paid_amount,0)) > 0
                    ORDER BY date DESC, id DESC
                    """,
                    (client_id,),
                )
                for r in cur.fetchall():
                    total_ttc = float(r[3] or 0)
                    paid = float(r[4] or 0)
                    reste = max(0.0, total_ttc - paid)
                    self.client_docs_tree.insert("", "end", values=(r[0], r[1], r[2], f"{total_ttc:.2f}", f"{paid:.2f}", f"{reste:.2f}"))
            finally:
                conn.close()

        def show_add_payment_dialog_for_selected(self):
            # pick selected doc from client_docs_tree if present
            if not hasattr(self, "client_docs_tree"):
                messagebox.showerror("Erreur", "Aucun document sélectionné")
                return
            sel = self.client_docs_tree.selection()
            if not sel:
                messagebox.showerror("Erreur", "Sélectionne un document")
                return
            vals = self.client_docs_tree.item(sel[0], "values")
            if not vals:
                messagebox.showerror("Erreur", "Sélectionne un document")
                return
            doc_id = int(vals[0])
            self.show_add_payment_dialog_for(doc_id)

        def mark_selected_document_paid_full(self):
            if not hasattr(self, "client_docs_tree"):
                messagebox.showerror("Erreur", "Aucun document sélectionné")
                return
            sel = self.client_docs_tree.selection()
            if not sel:
                messagebox.showerror("Erreur", "Sélectionne un document")
                return
            vals = self.client_docs_tree.item(sel[0], "values")
            if not vals:
                messagebox.showerror("Erreur", "Sélectionne un document")
                return
            doc_id = int(vals[0])
            # reuse mark_invoice_paid_full behavior by setting selection
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            try:
                cursor.execute("SELECT total_ttc, paid_amount FROM documents WHERE id = ?", (doc_id,))
                row = cursor.fetchone()
                if not row:
                    messagebox.showerror("Erreur", "Document introuvable")
                    return
                total_ttc = float(row[0] or 0)
                paid = float(row[1] or 0)
                delta = max(0.0, total_ttc - paid)
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
            dlg.geometry("520x380")
            dlg.transient(dialog)
            dlg.grab_set()

            header = ctk.CTkFrame(dlg, fg_color="transparent")
            header.pack(fill="x", padx=20, pady=(15, 10))
            ctk.CTkLabel(
                header,
                text="Modifier une ligne"
                if item_id is not None
                else "Ajouter une ligne",
                font=self.ui["font_h2"],
            ).pack(side="left")

            form = ctk.CTkFrame(dlg, fg_color="transparent")
            form.pack(fill="both", expand=True, padx=20, pady=(0, 10))

            form.grid_columnconfigure(0, weight=1)
            form.grid_columnconfigure(1, weight=1)

            current = {
                "article_id": "",
                "designation": "",
                "qty": "1",
                "pu": "0",
                "tva": "0",
            }
            if item_id is not None:
                vals = lines_tree.item(item_id, "values")
                if vals:
                    current["article_id"] = vals[1]
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
            price_map = {a[1]: float(a[2] or 0) for a in articles}
            tva_map = {a[1]: float(a[3] or 0) for a in articles}

            ctk.CTkLabel(
                form, text="Article (optionnel)", font=self.ui["font_body"]
            ).grid(row=0, column=0, columnspan=2, sticky="w")
            art_combo = ttk.Combobox(form, values=names, state="readonly", width=54)
            art_combo.grid(row=1, column=0, columnspan=2, sticky="we", pady=(0, 10))

            ctk.CTkLabel(form, text="Désignation", font=self.ui["font_body"]).grid(
                row=2, column=0, columnspan=2, sticky="w"
            )
            des_entry = ctk.CTkEntry(form)
            des_entry.insert(0, current["designation"])
            des_entry.grid(row=3, column=0, columnspan=2, sticky="we", pady=(0, 10))

            ctk.CTkLabel(form, text="Quantité", font=self.ui["font_body"]).grid(
                row=4, column=0, sticky="w"
            )
            qty_var = ctk.StringVar(value=current["qty"])
            qty_entry = ctk.CTkEntry(form, textvariable=qty_var)
            qty_entry.grid(row=5, column=0, sticky="we", pady=(0, 10), padx=(0, 10))

            ctk.CTkLabel(form, text="Prix unitaire", font=self.ui["font_body"]).grid(
                row=4, column=1, sticky="w"
            )
            pu_var = ctk.StringVar(value=current["pu"])
            pu_entry = ctk.CTkEntry(form, textvariable=pu_var)
            pu_entry.grid(row=5, column=1, sticky="we", pady=(0, 10))

            ctk.CTkLabel(form, text="TVA %", font=self.ui["font_body"]).grid(
                row=6, column=0, sticky="w"
            )
            tva_var = ctk.StringVar(value=current["tva"])
            tva_entry = ctk.CTkEntry(form, textvariable=tva_var)
            tva_entry.grid(row=7, column=0, sticky="we", pady=(0, 10), padx=(0, 10))

            preview = ctk.CTkFrame(form, fg_color="transparent")
            preview.grid(row=7, column=1, sticky="we", pady=(0, 10))
            ctk.CTkLabel(
                preview,
                text="Aperçu",
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
                    qty_f = float(qty_entry.get().strip() or 0)
                    pu_f = float(pu_entry.get().strip() or 0)
                    tva_f = float(tva_entry.get().strip() or 0)
                except Exception:
                    prev_ht_label.configure(text="HT: -")
                    prev_tva_label.configure(text="TVA: -")
                    prev_ttc_label.configure(text="TTC: -")
                    return
                line_ht = qty_f * pu_f
                line_tva = line_ht * (tva_f / 100.0)
                line_ttc = line_ht + line_tva
                prev_ht_label.configure(text=f"HT: {line_ht:.2f}")
                prev_tva_label.configure(text=f"TVA: {line_tva:.2f}")
                prev_ttc_label.configure(text=f"TTC: {line_ttc:.2f}")

            def on_select(event=None):
                name = art_combo.get().strip()
                if not name:
                    return
                des_entry.delete(0, "end")
                des_entry.insert(0, name)
                pu_var.set(str(price_map.get(name, 0.0)))
                tva_var.set(str(tva_map.get(name, 0.0)))

                update_preview()

            art_combo.bind("<<ComboboxSelected>>", on_select)

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
                    messagebox.showerror("Erreur", "La désignation est obligatoire")
                    return
                try:
                    qty_f = float(qty_entry.get().strip())
                    pu_f = float(pu_entry.get().strip())
                    tva_f = float(tva_entry.get().strip())
                except ValueError:
                    messagebox.showerror("Erreur", "Quantité / PU / TVA invalides")
                    return

                article_id_val = ""
                if art_combo.get().strip() in id_map:
                    article_id_val = id_map[art_combo.get().strip()]

                if item_id is None:
                    lines_tree.insert(
                        "", "end", values=("", article_id_val, des, qty_f, pu_f, tva_f)
                    )
                else:
                    line_id_val = lines_tree.item(item_id, "values")[0]
                    lines_tree.item(
                        item_id,
                        values=(line_id_val, article_id_val, des, qty_f, pu_f, tva_f),
                    )

                self._apply_zebra(lines_tree)
                recalc_totals_once()
                dlg.destroy()

            btns = ctk.CTkFrame(dlg, fg_color="transparent")
            btns.pack(fill="x", padx=20, pady=(0, 15))
            ctk.CTkButton(
                btns,
                text="Annuler",
                command=dlg.destroy,
                fg_color="gray",
                hover_color="darkgray",
            ).pack(side="right", padx=10)
            ctk.CTkButton(btns, text="Enregistrer", command=save_line).pack(
                side="right"
            )

            des_entry.focus_set()

        def add_line():
            open_line_editor(None)

        def edit_line():
            item_id = _get_selected_line_item()
            if not item_id:
                messagebox.showerror("Erreur", "Sélectionne une ligne")
                return
            open_line_editor(item_id)

        def delete_line():
            if is_locked:
                return
            item_id = _get_selected_line_item()
            if not item_id:
                messagebox.showerror("Erreur", "Sélectionne une ligne")
                return
            if not messagebox.askyesno("Supprimer", "Supprimer cette ligne ?"):
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
                messagebox.showerror("Erreur", "Le numéro est obligatoire")
                return

            client_name = client_combo.get().strip()
            new_client_id = client_map.get(client_name) if client_name else None

            # Totals
            try:
                new_total_ht = float((ht_value_label.cget("text") or "0").strip())
                new_total_tva = float((tva_value_label.cget("text") or "0").strip())
                new_total_ttc = float((ttc_value_label.cget("text") or "0").strip())
            except Exception:
                messagebox.showerror("Erreur", "Totaux invalides")
                return

            conn3 = sqlite3.connect(self.db_path)
            cur3 = conn3.cursor()
            try:
                cur3.execute(
                    "SELECT statut FROM documents WHERE id = ?", (document_id,)
                )
                row = cur3.fetchone()
                if row and row[0] == "valide":
                    messagebox.showerror(
                        "Erreur", "Document déjà validé. Modification impossible."
                    )
                    return

                cur3.execute(
                    """
                    UPDATE documents
                    SET numero = ?, date = ?, client_id = ?,
                        total_ht = ?, total_tva = ?, total_ttc = ?
                    WHERE id = ?
                    """,
                    (
                        new_numero,
                        new_date or date_str,
                        new_client_id,
                        new_total_ht,
                        new_total_tva,
                        new_total_ttc,
                        document_id,
                    ),
                )

                cur3.execute(
                    "DELETE FROM document_lignes WHERE document_id = ?", (document_id,)
                )
                for item in lines_tree.get_children():
                    _line_id, article_id_val, des, qty, pu, tva = lines_tree.item(
                        item, "values"
                    )
                    try:
                        qty_f = float(qty)
                        pu_f = float(pu)
                        tva_f = float(tva)
                    except Exception:
                        continue

                    line_ht = qty_f * pu_f
                    line_tva = line_ht * (tva_f / 100.0)
                    line_ttc = line_ht + line_tva

                    art_id = None
                    if article_id_val not in (None, ""):
                        try:
                            art_id = int(float(article_id_val))
                        except Exception:
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
                            pu_f,
                            tva_f,
                            line_ht,
                            line_tva,
                            line_ttc,
                        ),
                    )

                conn3.commit()
            finally:
                conn3.close()

            self.load_documents(doc_type)
            self.load_paiements()
            messagebox.showinfo("Enregistré", "Document mis à jour")
            dialog.title(f"Détails {title} - {new_numero}")

        toolbar = ctk.CTkFrame(dialog, fg_color="transparent")
        toolbar.pack(fill="x", padx=20, pady=(0, 10))
        add_btn = ctk.CTkButton(toolbar, text="+ Ajouter ligne", command=add_line)
        add_btn.pack(side="left")
        edit_btn = ctk.CTkButton(toolbar, text="Modifier ligne", command=edit_line)
        edit_btn.pack(side="left", padx=10)
        add_btn.configure(text="+ Ajouter ligne")
        edit_btn.configure(text="Modifier ligne")
        del_btn = ctk.CTkButton(
            toolbar,
            text="Supprimer ligne",
            command=delete_line,
            fg_color=self.ui["danger_fg"],
            hover_color=self.ui["danger_hover"],
        )
        del_btn.pack(side="left")

        footer = ctk.CTkFrame(dialog, fg_color="transparent")
        footer.pack(fill="x", padx=20, pady=(0, 15))
        ctk.CTkButton(
            footer,
            text="Fermer",
            command=dialog.destroy,
            fg_color="gray",
            hover_color="darkgray",
        ).pack(side="right", padx=10)
        save_btn = ctk.CTkButton(
            footer, text="Enregistrer", command=save_document_changes
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
            messagebox.showerror("Erreur", "Sélectionne un document")
            return

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT id, type, numero, date, client_id, statut,
                       total_ht, total_tva, total_ttc
                FROM documents
                WHERE id = ?
                """,
                (doc_id,),
            )
            doc = cursor.fetchone()
            if not doc:
                return

            if to_type == "bl" and doc[1] != "devis":
                messagebox.showerror("Erreur", "Conversion uniquement Devis → BL")
                return
            if to_type == "factures" and doc[1] != "bl":
                messagebox.showerror("Erreur", "Conversion uniquement BL → Facture")
                return

            new_numero = self.get_next_numero(to_type)
            new_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            source_bl_id = doc_id if to_type == "factures" else None

            cursor.execute(
                """
                INSERT INTO documents (type, numero, date, client_id, statut,
                                       total_ht, total_tva, total_ttc,
                                       paid_amount, payment_mode, source_bl_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    0.0,
                    "none",
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

            conn.commit()
        finally:
            conn.close()

        self.load_documents(to_type)
        messagebox.showinfo("Conversion", f"Document converti: {new_numero}")

    def validate_selected_document(self, doc_type):
        doc_id = self._get_selected_doc_id_from_type(doc_type)
        if not doc_id:
            messagebox.showerror("Erreur", "Sélectionne un document")
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
                messagebox.showinfo("Validation", "Ce document est déjà validé")
                return

            # Stock movements
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
                # check stock first
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
                            f"Stock insuffisant pour '{designation}': stock={stock}, demandé={needed}"
                        )

                # apply stock decrement
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
            messagebox.showerror("Stock", str(e))
            return
        finally:
            conn.close()

        # Refresh UI
        self.load_documents(doc_type)
        self.load_articles()
        messagebox.showinfo("Validation", "Document validé")

    def create_paiements_frame(self):
        frame = ctk.CTkFrame(self.main_frame, corner_radius=0, fg_color="transparent")
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(header, text="Paiements / Impayés", font=self.ui["font_h1"]).pack(side="left")

        # Two-pane layout: left = clients with unpaid, right = their unpaid documents
        container = ctk.CTkFrame(frame, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=6, pady=(8, 0))

        left = ctk.CTkFrame(container, width=320, fg_color="transparent")
        left.pack(side="left", fill="y", padx=(0, 8), pady=6)

        right = ctk.CTkFrame(container, fg_color="transparent")
        right.pack(side="right", fill="both", expand=True, pady=6)

        # Clients tree (left)
        ctk.CTkLabel(left, text="Clients (impayés)", font=self.ui.get("font_h2")).pack(anchor="w", pady=(0, 8))
        client_cols = ("ClientID", "Client", "Impayé")
        self.clients_tree = ttk.Treeview(left, columns=client_cols, show="headings", selectmode="browse", height=20)
        for c in client_cols:
            self.clients_tree.heading(c, text=c if c != "Impayé" else "Impayé (€)")
            self.clients_tree.column(c, width=100, anchor="center")
        self.clients_tree.column("Client", width=180, anchor="w")
        self.clients_tree.pack(fill="y", expand=True)
        client_scroll = ttk.Scrollbar(left, orient="vertical", command=self.clients_tree.yview)
        client_scroll.pack(side="right", fill="y")
        self.clients_tree.configure(yscrollcommand=client_scroll.set)

        # Actions under clients
        clients_actions = ctk.CTkFrame(left, fg_color="transparent")
        clients_actions.pack(fill="x", pady=(8, 0))
        ctk.CTkButton(clients_actions, text="Rafraîchir", command=self.load_paiements).pack(side="left")

        # Right: documents for selected client
        ctk.CTkLabel(right, text="Documents impayés", font=self.ui.get("font_h2")).pack(anchor="w", pady=(0, 8))
        doc_cols = ("ID", "Numéro", "Type", "TTC", "Payé", "Reste")
        self.client_docs_tree = ttk.Treeview(right, columns=doc_cols, show="headings", selectmode="browse")
        for c in doc_cols:
            self.client_docs_tree.heading(c, text=c)
            self.client_docs_tree.column(c, width=120, anchor="center")
        self.client_docs_tree.column("Numéro", width=140, anchor="w")
        self.client_docs_tree.column("Type", width=100, anchor="center")
        self.client_docs_tree.pack(fill="both", expand=True)
        doc_scroll = ttk.Scrollbar(right, orient="vertical", command=self.client_docs_tree.yview)
        doc_scroll.pack(side="right", fill="y")
        self.client_docs_tree.configure(yscrollcommand=doc_scroll.set)

        # Actions for selected document
        doc_actions = ctk.CTkFrame(right, fg_color="transparent")
        doc_actions.pack(fill="x", pady=(8, 0))
        ctk.CTkButton(doc_actions, text="Enregistrer un paiement", command=lambda: self.show_add_payment_dialog_for_selected()).pack(side="left")
        ctk.CTkButton(doc_actions, text="Marquer payé (document)", command=lambda: self.mark_selected_document_paid_full()).pack(side="left", padx=8)

        # Bind selection to load documents for a client
        self.clients_tree.bind("<<TreeviewSelect>>", lambda e: self._on_client_selected())

        self.load_paiements()
        self.frames["paiements"] = frame

    def load_paiements(self):
        # Populate clients_tree with clients having unpaid amounts
        if not hasattr(self, "clients_tree"):
            return

        for item in self.clients_tree.get_children():
            self.clients_tree.delete(item)
        for item in self.client_docs_tree.get_children():
            self.client_docs_tree.delete(item)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT c.id, COALESCE(c.nom,''), SUM(COALESCE(d.total_ttc,0) - COALESCE(d.paid_amount,0)) as unpaid
                FROM documents d
                JOIN clients c ON c.id = d.client_id
                WHERE d.type IN ('devis', 'bl', 'factures')
                GROUP BY c.id
                HAVING unpaid > 0
                ORDER BY unpaid DESC
                """
            )
            for row in cursor.fetchall():
                client_id = row[0]
                name = row[1] or ""
                unpaid = float(row[2] or 0)
                self.clients_tree.insert("", "end", values=(client_id, name, f"{unpaid:.2f}"))
        finally:
            conn.close()

        self._apply_zebra(self.clients_tree)

    def _on_client_selected(self):
        # Handler for selecting a client in the impayés clients tree
        if not hasattr(self, "clients_tree"):
            return
        sel = self.clients_tree.selection()
        if not sel:
            return
        vals = self.clients_tree.item(sel[0], "values")
        if not vals:
            return
        client_id = vals[0]
        try:
            client_id = int(client_id)
        except Exception:
            return

        # clear previous docs
        if hasattr(self, "client_docs_tree"):
            for item in self.client_docs_tree.get_children():
                self.client_docs_tree.delete(item)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT id, numero, type, total_ttc, COALESCE(paid_amount,0) FROM documents
                WHERE client_id = ? AND (COALESCE(total_ttc,0) - COALESCE(paid_amount,0)) > 0
                ORDER BY date DESC, id DESC
                """,
                (client_id,),
            )
            for r in cur.fetchall():
                total_ttc = float(r[3] or 0)
                paid = float(r[4] or 0)
                reste = max(0.0, total_ttc - paid)
                self.client_docs_tree.insert(
                    "",
                    "end",
                    values=(r[0], r[1], r[2], f"{total_ttc:.2f}", f"{paid:.2f}", f"{reste:.2f}"),
                )
        finally:
            conn.close()

        # apply zebra styling if available
        try:
            self._apply_zebra(self.client_docs_tree)
        except Exception:
            pass

    def show_add_payment_dialog_for_selected(self):
        # Wrapper: open payment dialog for currently selected document in client_docs_tree
        if not hasattr(self, "client_docs_tree"):
            messagebox.showerror("Erreur", "Aucun document sélectionné")
            return
        sel = self.client_docs_tree.selection()
        if not sel:
            messagebox.showerror("Erreur", "Sélectionne un document")
            return
        vals = self.client_docs_tree.item(sel[0], "values")
        if not vals:
            messagebox.showerror("Erreur", "Sélectionne un document")
            return
        try:
            doc_id = int(vals[0])
        except Exception:
            messagebox.showerror("Erreur", "ID de document invalide")
            return
        self.show_add_payment_dialog_for(doc_id)

    def mark_selected_document_paid_full(self):
        # Mark the selected document (from client_docs_tree) as fully paid
        if not hasattr(self, "client_docs_tree"):
            messagebox.showerror("Erreur", "Aucun document sélectionné")
            return
        sel = self.client_docs_tree.selection()
        if not sel:
            messagebox.showerror("Erreur", "Sélectionne un document")
            return
        vals = self.client_docs_tree.item(sel[0], "values")
        if not vals:
            messagebox.showerror("Erreur", "Sélectionne un document")
            return
        try:
            doc_id = int(vals[0])
        except Exception:
            messagebox.showerror("Erreur", "ID de document invalide")
            return

        # preserve selected client so we can restore selection after reload
        preserved_client_id = None
        try:
            if hasattr(self, "clients_tree"):
                csel = self.clients_tree.selection()
                if csel:
                    cvals = self.clients_tree.item(csel[0], "values")
                    if cvals:
                        preserved_client_id = int(cvals[0])

        except Exception:
            preserved_client_id = None

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT total_ttc, paid_amount FROM documents WHERE id = ?", (doc_id,))
            row = cursor.fetchone()
            if not row:
                messagebox.showerror("Erreur", "Document introuvable")
                return
            total_ttc = float(row[0] or 0)
            paid = float(row[1] or 0)
            delta = max(0.0, total_ttc - paid)
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
        # try to restore previously selected client (if any)
        if preserved_client_id is not None and hasattr(self, "clients_tree"):
            for iid in self.clients_tree.get_children():
                try:
                    vals = self.clients_tree.item(iid, "values")
                except Exception:
                    continue
                if vals and int(vals[0]) == preserved_client_id:
                    self.clients_tree.selection_set(iid)
                    try:
                        self.clients_tree.focus(iid)
                    except Exception:
                        pass
                    # reload the client's documents into the right pane
                    try:
                        self._on_client_selected()
                    except Exception:
                        pass
                    break

        for t in ("devis", "bl", "factures"):
            try:
                self.load_documents(t)
            except Exception:
                pass

    def _get_selected_invoice_id(self):
        # legacy helper: select first selected id from paiements_tree if present
        if hasattr(self, "paiements_tree"):
            sel = self.paiements_tree.selection()
            if not sel:
                return None
            vals = self.paiements_tree.item(sel, "values")
            if not vals:
                return None
            return int(vals[0])
        # fallback: if using clients/doc view, return selected doc id from client_docs_tree
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
            messagebox.showerror("Erreur", "Sélectionne un document")
            return

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT total_ttc, paid_amount FROM documents WHERE id = ?", (doc_id,))
            row = cursor.fetchone()
            if not row:
                return
            total_ttc = float(row[0] or 0)
            paid = float(row[1] or 0)
            delta = max(0.0, total_ttc - paid)

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
        # legacy single-invoice dialog; prefer show_add_payment_dialog_for(document_id)
        invoice_id = self._get_selected_invoice_id()
        if not invoice_id:
            messagebox.showerror("Erreur", "Sélectionne une facture")
            return
        self.show_add_payment_dialog_for(invoice_id)

    def show_add_payment_dialog_for(self, document_id):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Enregistrer un paiement")
        dialog.geometry("420x280")
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(
            dialog, text=f"Document ID: {document_id}", font=("Arial", 14, "bold")
        ).pack(pady=(15, 5))
        ctk.CTkLabel(dialog, text="Montant").pack(pady=(10, 0))
        amount_entry = ctk.CTkEntry(dialog, width=220)
        amount_entry.pack(pady=(0, 10))

        ctk.CTkLabel(dialog, text="Méthode (optionnel)").pack(pady=(5, 0))
        method_entry = ctk.CTkEntry(dialog, width=220)
        method_entry.pack(pady=(0, 10))

        def save_payment():
            try:
                amount = float(amount_entry.get().strip())
            except ValueError:
                messagebox.showerror("Erreur", "Montant invalide")
                return
            method = method_entry.get().strip() or None

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT total_ttc, paid_amount FROM documents WHERE id = ?",
                    (document_id,),
                )
                row = cursor.fetchone()
                if not row:
                    messagebox.showerror("Erreur", "Document introuvable")
                    return
                total_ttc = float(row[0] or 0)
                paid = float(row[1] or 0)
                new_paid = min(total_ttc, paid + amount)

                date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute(
                    "INSERT INTO payments (document_id, date, amount, method, note) VALUES (?, ?, ?, ?, ?)",
                    (document_id, date_now, amount, method, "Paiement"),
                )

                mode = "partial" if new_paid < total_ttc else "full"
                cursor.execute(
                    "UPDATE documents SET paid_amount = ?, payment_mode = ? WHERE id = ?",
                    (new_paid, mode, document_id),
                )
                conn.commit()
            finally:
                conn.close()

            dialog.destroy()
            self.load_paiements()
            # reload documents for relevant type(s)
            for t in ("devis", "bl", "factures"):
                try:
                    self.load_documents(t)
                except Exception:
                    pass

        btns = ctk.CTkFrame(dialog, fg_color="transparent")
        btns.pack(fill="x", padx=20, pady=10)
        ctk.CTkButton(
            btns,
            text="Annuler",
            command=dialog.destroy,
            fg_color="gray",
            hover_color="darkgray",
        ).pack(side="right", padx=10)
        ctk.CTkButton(btns, text="Enregistrer", command=save_payment).pack(side="right")

        amount_entry.focus_set()

    def create_export_frame(self):
        frame = ctk.CTkFrame(self.main_frame, corner_radius=0, fg_color="transparent")

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 20))
        ctk.CTkLabel(header, text="Exportation", font=self.ui["font_h1"]).pack(
            side="left"
        )

        ctk.CTkLabel(
            frame,
            text="Sélectionne un document (Devis / BL / Facture / Bon de réception) et double-clique dessus pour exporter/imprimer.",
            font=("Arial", 12),
        ).pack(pady=10)

        self.frames["export"] = frame

    def show_export_dialog_for_document(self, document_id):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Export / Impression")
        dialog.geometry("420x220")
        dialog.transient(self)
        dialog.grab_set()

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
            messagebox.showinfo("Export", f"Export HTML terminé:\n{path}")

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
                    "PDF", "ReportLab n'est pas installé. Installe les dépendances."
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
            messagebox.showinfo("Export", f"Export PDF terminé:\n{path}")

        def print_pdf_windows():
            if canvas is None or A4 is None:
                messagebox.showerror(
                    "PDF", "ReportLab n'est pas installé. Installe les dépendances."
                )
                return
            tmp = os.path.join(tempfile.gettempdir(), f"document_{document_id}.pdf")
            self.export_document_pdf(document_id, tmp)
            try:
                os.startfile(tmp, "print")
            except Exception:
                os.startfile(tmp)

        ctk.CTkButton(dialog, text="Exporter (HTML)", command=export_html).pack(pady=6)
        ctk.CTkButton(
            dialog, text="Imprimer HTML (Windows)", command=print_direct
        ).pack(pady=6)
        ctk.CTkButton(dialog, text="Exporter (PDF)", command=export_pdf).pack(pady=6)
        ctk.CTkButton(
            dialog, text="Imprimer PDF (Windows)", command=print_pdf_windows
        ).pack(pady=6)
        ctk.CTkButton(
            dialog,
            text="Fermer",
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
                       COALESCE(c.nom, ''), COALESCE(c.adresse, ''), COALESCE(c.telephone, ''), COALESCE(c.email, '')
                FROM documents d
                LEFT JOIN clients c ON c.id = d.client_id
                WHERE d.id = ?
                """,
                (document_id,),
            )
            doc = cursor.fetchone()
            if not doc:
                raise ValueError("Document introuvable")

            cursor.execute(
                "SELECT designation, quantite, prix_unitaire, tva, total_ht, total_tva, total_ttc FROM document_lignes WHERE document_id = ?",
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
        ) = doc
        title = {
            "devis": "Devis",
            "bl": "Bon de livraison",
            "factures": "Facture",
            "reception": "Bon de réception",
        }.get(doc_type, doc_type)

        rows_html = "".join(
            f"<tr><td>{d}</td><td style='text-align:right'>{q}</td><td style='text-align:right'>{pu:.2f}</td><td style='text-align:right'>{tva:.2f}</td><td style='text-align:right'>{ht:.2f}</td><td style='text-align:right'>{tv:.2f}</td><td style='text-align:right'>{ttc:.2f}</td></tr>"
            for (d, q, pu, tva, ht, tv, ttc) in lines
        )

        html = f"""<!doctype html>
<html lang='fr'>
<head>
  <meta charset='utf-8'>
  <title>{title} {numero}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; }}
    .header {{ display:flex; justify-content:space-between; align-items:flex-start; }}
    .box {{ border: 1px solid #ddd; padding: 12px; border-radius: 8px; }}
    table {{ width:100%; border-collapse: collapse; margin-top: 16px; }}
    th, td {{ border: 1px solid #ddd; padding: 8px; }}
    th {{ background:#f5f5f5; }}
    .total {{ text-align:right; font-size: 18px; font-weight: bold; margin-top: 12px; }}
    .muted {{ color:#666; font-size: 12px; }}
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
    </div>
    <div class='box' style='min-width:320px'>
      <div><b>Client</b></div>
      <div>{client_nom}</div>
      <div class='muted'>{client_adresse}</div>
      <div class='muted'>{client_tel} {client_email}</div>
    </div>
  </div>

  <table>
    <thead>
      <tr>
        <th>Désignation</th>
        <th style='text-align:right'>Qté</th>
        <th style='text-align:right'>PU</th>
        <th style='text-align:right'>TVA%</th>
        <th style='text-align:right'>HT</th>
        <th style='text-align:right'>TVA</th>
        <th style='text-align:right'>TTC</th>
      </tr>
    </thead>
    <tbody>
      {rows_html if rows_html else "<tr><td colspan='7' style='text-align:center;color:#666'>Aucune ligne</td></tr>"}
    </tbody>
  </table>

    <div class='total'>HT: {float(total_ht or 0):.2f} | TVA: {float(total_tva or 0):.2f} | TTC: {float(total_ttc or 0):.2f}</div>

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
                       COALESCE(c.nom, ''), COALESCE(c.adresse, ''), COALESCE(c.telephone, ''), COALESCE(c.email, '')
                FROM documents d
                LEFT JOIN clients c ON c.id = d.client_id
                WHERE d.id = ?
                """,
                (document_id,),
            )
            doc = cursor.fetchone()
            if not doc:
                raise ValueError("Document introuvable")

            cursor.execute(
                "SELECT designation, quantite, prix_unitaire, tva, total_ht, total_tva, total_ttc FROM document_lignes WHERE document_id = ?",
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
        ) = doc

        title = {
            "devis": "Devis",
            "bl": "Bon de livraison",
            "factures": "Facture",
            "reception": "Bon de réception",
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
                "Gestion Commerciale",
                ParagraphStyle(name="Brand", parent=bold, fontSize=12, spaceAfter=2),
            )
        )
        story.append(Paragraph(f"{title} <b>{numero}</b>", h1))

        meta_left = f"<b>Date</b> : {date_str}<br/><b>Statut</b> : {statut}"
        meta_right = f"<b>Client</b> : {client_nom or ''}"
        if client_adresse:
            meta_right += f"<br/>{client_adresse}"
        contact = " ".join([p for p in [client_tel, client_email] if p])
        if contact:
            meta_right += f"<br/>{contact}"

        meta_table = Table(
            [[Paragraph(meta_left, normal), Paragraph(meta_right, normal)]],
            colWidths=[88 * mm, 88 * mm],
        )
        meta_table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#E6E9EF")),
                    ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#E6E9EF")),
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FAFBFD")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.append(meta_table)
        story.append(Spacer(1, 10))

        table_header = [
            Paragraph("Désignation", bold),
            Paragraph("Qté", bold),
            Paragraph("PU", bold),
            Paragraph("TVA%", bold),
            Paragraph("HT", bold),
            Paragraph("TVA", bold),
            Paragraph("TTC", bold),
        ]
        table_rows = [table_header]

        for des, qty, pu, tva, tht, ttva, tttc in lines:
            des_txt = str(des or "")
            table_rows.append(
                [
                    Paragraph(des_txt.replace("\n", "<br/>") or "-", normal),
                    Paragraph(f"{float(qty or 0):.2f}", normal),
                    Paragraph(f"{float(pu or 0):.2f}", normal),
                    Paragraph(f"{float(tva or 0):.2f}", normal),
                    Paragraph(f"{float(tht or 0):.2f}", normal),
                    Paragraph(f"{float(ttva or 0):.2f}", normal),
                    Paragraph(f"{float(tttc or 0):.2f}", normal),
                ]
            )

        if len(table_rows) == 1:
            table_rows.append(
                [Paragraph("Aucune ligne", normal), "", "", "", "", "", ""]
            )

        lines_table = Table(
            table_rows,
            colWidths=[70 * mm, 14 * mm, 18 * mm, 14 * mm, 18 * mm, 18 * mm, 18 * mm],
            repeatRows=1,
        )
        lines_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F2937")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, 0), 9),
                    ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                    ("ALIGN", (0, 0), (0, -1), "LEFT"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E6E9EF")),
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

        totals_rows = [
            [
                Paragraph("Total HT", bold),
                Paragraph(f"{float(total_ht or 0):.2f}", bold),
            ],
            [
                Paragraph("Total TVA", bold),
                Paragraph(f"{float(total_tva or 0):.2f}", bold),
            ],
            [
                Paragraph("Total TTC", bold),
                Paragraph(f"{float(total_ttc or 0):.2f}", bold),
            ],
        ]
        # 'Payé' / 'Reste' removed from document PDF totals per settings

        totals_table = Table(totals_rows, colWidths=[35 * mm, 35 * mm])
        totals_table.setStyle(
            TableStyle(
                [
                    ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#E6E9EF")),
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FAFBFD")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        wrapper = Table([["", totals_table]], colWidths=[90 * mm, 70 * mm])
        wrapper.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story.append(wrapper)
        story.append(Spacer(1, 8))
        story.append(Paragraph("Merci pour votre confiance.", small))

        def on_page(c, _doc):
            c.setFont("Helvetica", 8)
            c.setFillColor(colors.HexColor("#6B7280"))
            c.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {_doc.page}")

        doc.build(story, onFirstPage=on_page, onLaterPages=on_page)

    def create_dashboard_frame(self):
        frame = ctk.CTkFrame(self.main_frame, corner_radius=0, fg_color="transparent")

        # Titre
        title = ctk.CTkLabel(frame, text="Tableau de bord", font=("Arial", 24, "bold"))
        title.pack(pady=20)

        # Cartes d'information
        cards_frame = ctk.CTkFrame(frame, fg_color="transparent")
        cards_frame.pack(fill="x", pady=10)

        # Fonction pour créer une carte
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

        # Récupérer les données
        conn = sqlite3.connect("commercial.db")
        cursor = conn.cursor()

        # Nombre de produits
        cursor.execute("SELECT COUNT(*) FROM products")
        product_count = cursor.fetchone()[0]

        # Ventes du jour
        cursor.execute("""
            SELECT COUNT(*), COALESCE(SUM(total_price), 0)
            FROM sales
            WHERE date(sale_date) = date('now')
        """)
        today_sales = cursor.fetchone()

        # Ventes du mois
        cursor.execute("""
            SELECT COUNT(*), COALESCE(SUM(total_price), 0)
            FROM sales
            WHERE strftime('%Y-%m', sale_date) = strftime('%Y-%m', 'now')
        """)
        month_sales = cursor.fetchone()

        conn.close()

        # Création des cartes
        card1 = create_card(cards_frame, "Produits en stock", str(product_count), "📦")
        card1.pack(side="left", fill="both", expand=True, padx=10)

        card2 = create_card(
            cards_frame,
            "Ventes du jour",
            f"{today_sales[0]}\n{today_sales[1]:.2f} €",
            "💰",
        )
        card2.pack(side="left", fill="both", expand=True, padx=10)

        card3 = create_card(
            cards_frame,
            "Ventes du mois",
            f"{month_sales[0]}\n{month_sales[1]:.2f} €",
            "📊",
        )
        card3.pack(side="left", fill="both", expand=True, padx=10)

        # Dernières ventes
        sales_frame = ctk.CTkFrame(frame, corner_radius=10)
        sales_frame.pack(fill="both", expand=True, pady=20, padx=10)

        ctk.CTkLabel(
            sales_frame, text="Dernières ventes", font=("Arial", 16, "bold")
        ).pack(pady=10)

        # Tableau des ventes récentes
        columns = ("ID", "Produit", "Quantité", "Prix total", "Date")
        self.sales_tree = ttk.Treeview(
            sales_frame, columns=columns, show="headings", selectmode="browse"
        )

        # Configuration des colonnes
        for col in columns:
            self.sales_tree.heading(col, text=col)
            self.sales_tree.column(col, width=100, anchor="center")

        # Configuration du style
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

        # Barre de défilement
        scrollbar = ttk.Scrollbar(
            sales_frame, orient="vertical", command=self.sales_tree.yview
        )
        scrollbar.pack(side="right", fill="y")
        self.sales_tree.configure(yscrollcommand=scrollbar.set)

        self.sales_tree.pack(fill="both", expand=True, padx=10, pady=10)

        # Charger les données
        self.load_recent_sales()

        self.frames["dashboard"] = frame

    def load_recent_sales(self, limit=10):
        # Effacer les données existantes
        for item in self.sales_tree.get_children():
            self.sales_tree.delete(item)

        conn = sqlite3.connect("commercial.db")
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
            # Formater la date
            sale_date = datetime.strptime(row[4], "%Y-%m-%d %H:%M:%S")
            formatted_date = sale_date.strftime("%d/%m/%Y %H:%M")

            self.sales_tree.insert(
                "",
                "end",
                values=(
                    row[0],  # ID
                    row[1],  # Produit
                    row[2],  # Quantité
                    f"{row[3]:.2f} €",  # Prix total
                    formatted_date,  # Date formatée
                ),
            )

        conn.close()

    def create_sales_frame(self):
        """Créer le cadre pour la gestion des ventes"""
        frame = ctk.CTkFrame(self.main_frame)

        # Titre
        title = ctk.CTkLabel(
            frame, text="Gestion des Ventes", font=("Arial", 20, "bold")
        )
        title.pack(pady=20)

        # Barre d'outils
        toolbar = ctk.CTkFrame(frame, fg_color="transparent")
        toolbar.pack(fill="x", padx=10, pady=5)

        # Bouton Nouvelle Vente
        new_sale_btn = ctk.CTkButton(
            toolbar, text="+ Nouvelle Vente", command=self.show_new_sale_dialog
        )
        new_sale_btn.pack(side="left", padx=5)

        # Barre de recherche
        search_frame = ctk.CTkFrame(toolbar, fg_color="transparent")
        search_frame.pack(side="right")

        self.sale_search_entry = ctk.CTkEntry(
            search_frame, placeholder_text="Rechercher une vente...", width=250
        )
        self.sale_search_entry.pack(side="left", padx=5)
        self.sale_search_entry.bind("<Return>", self.search_sales)

        search_btn = ctk.CTkButton(
            search_frame, text="Rechercher", command=self.search_sales
        )
        search_btn.pack(side="left")

        # Tableau des ventes
        columns = ("id", "date", "client", "total", "statut", "actions")
        self.sales_tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse"
        )

        # Configuration des colonnes
        self.sales_tree.heading("id", text="ID")
        self.sales_tree.heading("date", text="Date")
        self.sales_tree.heading("client", text="Client")
        self.sales_tree.heading("total", text="Total")
        self.sales_tree.heading("statut", text="Statut")
        self.sales_tree.heading("actions", text="Actions")

        self.sales_tree.column("id", width=50, anchor="center")
        self.sales_tree.column("date", width=150, anchor="center")
        self.sales_tree.column("client", width=200, anchor="w")
        self.sales_tree.column("total", width=100, anchor="e")
        self.sales_tree.column("statut", width=100, anchor="center")
        self.sales_tree.column("actions", width=150, anchor="center")

        # Barre de défilement
        scrollbar = ttk.Scrollbar(
            frame, orient="vertical", command=self.sales_tree.yview
        )
        scrollbar.pack(side="right", fill="y")
        self.sales_tree.configure(yscrollcommand=scrollbar.set)

        self.sales_tree.pack(fill="both", expand=True, padx=10, pady=10)

        # Charger les ventes
        self.load_sales()

        # Détection du double-clic pour voir les détails
        self.sales_tree.bind("<Double-1>", self.on_sale_double_click)

        self.frames["sales"] = frame

    def create_products_frame(self):
        frame = ctk.CTkFrame(self.main_frame, corner_radius=0, fg_color="transparent")

        # En-tête avec titre et bouton d'ajout
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 20))

        title = ctk.CTkLabel(
            header, text="Gestion des produits", font=("Arial", 24, "bold")
        )
        title.pack(side="left")

        add_button = ctk.CTkButton(
            header, text="+ Ajouter un produit", command=self.show_add_product_dialog
        )
        add_button.pack(side="right")

        # Barre de recherche
        search_frame = ctk.CTkFrame(frame, fg_color="transparent")
        search_frame.pack(fill="x", pady=(0, 20))

        self.search_entry = ctk.CTkEntry(
            search_frame, placeholder_text="Rechercher un produit...", width=400
        )
        self.search_entry.pack(side="left", padx=(0, 10))

        search_button = ctk.CTkButton(
            search_frame, text="Rechercher", command=self.search_products
        )
        search_button.pack(side="left")

        # Tableau des produits
        columns = ("ID", "Nom", "Description", "Prix", "Stock", "Actions")
        self.products_tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse"
        )

        # Configuration des colonnes
        col_widths = [50, 150, 250, 80, 80, 150]
        for idx, col in enumerate(columns):
            self.products_tree.heading(col, text=col)
            self.products_tree.column(col, width=col_widths[idx], anchor="center")

        # Configuration du style
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

        # Barre de défilement
        scrollbar = ttk.Scrollbar(
            frame, orient="vertical", command=self.products_tree.yview
        )
        scrollbar.pack(side="right", fill="y")
        self.products_tree.configure(yscrollcommand=scrollbar.set)

        self.products_tree.pack(fill="both", expand=True)

        # Charger les produits
        self.load_products()

        # Détection du double-clic pour la modification
        self.products_tree.bind("<Double-1>", self.on_product_double_click)

        self.frames["products"] = frame

    def load_products(self, search_query=None):
        # Effacer les données existantes
        for item in self.products_tree.get_children():
            self.products_tree.delete(item)

        conn = sqlite3.connect("commercial.db")
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

        # Afficher les produits dans le tableau
        for product in products:
            product_id = product[0]
            name = product[1]
            description = product[2] if product[2] else ""
            price = f"{product[3]:.2f} €" if product[3] is not None else "0.00 €"
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
        dialog.transient(self)
        dialog.grab_set()

        # Fonction de validation
        def save_product():
            name = name_entry.get()
            description = desc_entry.get("1.0", "end-1c")
            price = price_entry.get()
            stock = stock_entry.get()

            if not all([name, price, stock]):
                messagebox.showerror(
                    "Erreur", "Veuillez remplir tous les champs obligatoires"
                )
                return

            try:
                price = float(price)
                stock = int(stock)

                conn = sqlite3.connect("commercial.db")
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

                messagebox.showinfo("Succès", "Produit ajouté avec succès!")
                self.load_products()  # Rafraîchir la liste
                dialog.destroy()

            except ValueError:
                messagebox.showerror(
                    "Erreur",
                    "Veuillez entrer des valeurs numériques valides pour le prix et le stock",
                )

        # Formulaire
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

        # Boutons
        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkButton(
            btn_frame,
            text="Annuler",
            command=dialog.destroy,
            fg_color="gray",
            hover_color="darkgray",
        ).pack(side="right", padx=10)

        ctk.CTkButton(btn_frame, text="Enregistrer", command=save_product).pack(
            side="right"
        )

        # Mettre le focus sur le premier champ
        name_entry.focus_set()

    def search_products(self, event=None):
        """Rechercher des produits en fonction du texte saisi"""
        search_query = self.search_entry.get().strip()
        if search_query:
            self.load_products(search_query)
        else:
            self.load_products()

    def load_sales(self, search_query=None):
        """Charger les ventes depuis la base de données"""
        # Effacer les données existantes
        for item in self.sales_tree.get_children():
            self.sales_tree.delete(item)

        # Ici, vous devriez implémenter la logique pour charger les ventes
        # depuis votre base de données
        # Ceci est un exemple basique
        example_sales = [
            (
                1,
                "2025-01-01 10:30:00",
                "Client 1",
                150.50,
                "Terminée",
                "Voir | Supprimer",
            ),
            (
                2,
                "2025-01-02 14:45:00",
                "Client 2",
                230.75,
                "En attente",
                "Voir | Supprimer",
            ),
        ]

        for sale in example_sales:
            self.sales_tree.insert("", "end", values=sale)

    def search_sales(self, event=None):
        """Rechercher des ventes"""
        search_query = self.sale_search_entry.get().strip()
        # Implémentez la logique de recherche ici
        self.load_sales(search_query)

    def show_new_sale_dialog(self):
        """Afficher la boîte de dialogue de nouvelle vente"""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Nouvelle Vente")
        dialog.geometry("800x600")
        dialog.transient(self)
        dialog.grab_set()

        # Implémentez ici le formulaire de nouvelle vente
        # avec sélection de client, ajout de produits, calcul du total, etc.

        # Exemple basique
        label = ctk.CTkLabel(dialog, text="Formulaire de nouvelle vente")
        label.pack(pady=20)

        # Ajoutez ici les champs nécessaires (client, produits, quantité, etc.)

    def on_sale_double_click(self, event):
        """Gérer le double-clic sur une vente pour voir les détails"""
        selected_item = self.sales_tree.selection()
        if not selected_item:
            return

        # Récupérer les détails de la vente
        sale_id = self.sales_tree.item(selected_item, "values")[0]

        # Afficher une boîte de dialogue avec les détails de la vente
        # Implémentez cette partie selon vos besoins
        messagebox.showinfo("Détails de la vente", f"Détails de la vente #{sale_id}")

    def on_product_double_click(self, event):
        """Gérer le double-clic sur un produit pour le modifier"""
        # Récupérer l'élément sélectionné
        selected_item = self.products_tree.selection()
        if not selected_item:
            return

        # Récupérer les valeurs du produit
        item_values = self.products_tree.item(selected_item, "values")
        product_id = item_values[0]

        # Récupérer les détails complets du produit depuis la base de données
        conn = sqlite3.connect("commercial.db")
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM products WHERE id = ?", (product_id,))
        product = cursor.fetchone()
        conn.close()

        if not product:
            messagebox.showerror("Erreur", "Produit introuvable")
            return

        # Créer la boîte de dialogue de modification
        dialog = ctk.CTkToplevel(self)
        dialog.title("Modifier le produit")
        dialog.geometry("400x400")
        dialog.transient(self)
        dialog.grab_set()

        # Créer la boîte de dialogue de modification
        dialog = ctk.CTkToplevel(self)
        dialog.title("Modifier le produit")
        dialog.geometry("500x550")  # Adjusted geometry for better layout
        dialog.transient(self)
        dialog.grab_set()

        # Header
        header = ctk.CTkFrame(dialog, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 10))
        ctk.CTkLabel(header, text="Modifier le produit", font=self.ui["font_h1"]).pack(
            side="left"
        )

        # Form content - using a scrollable frame for consistency with other dialogs and future expandability
        form_frame = ctk.CTkScrollableFrame(dialog, fg_color="transparent")
        form_frame.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        form_frame.grid_columnconfigure(
            0, weight=1
        )  # Configure for a single column for entries

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

        # Sub-frame for Price and Stock to keep them horizontally aligned if desired
        price_stock_inner_frame = ctk.CTkFrame(form_frame, fg_color="transparent")
        price_stock_inner_frame.grid(row=4, column=0, sticky="we", pady=(5, 0))
        price_stock_inner_frame.grid_columnconfigure(0, weight=1)
        price_stock_inner_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            price_stock_inner_frame, text="Prix*", font=self.ui["font_body"]
        ).grid(row=0, column=0, sticky="w")
        price_entry = ctk.CTkEntry(price_stock_inner_frame)
        price_entry.insert(0, str(product[3] if product[3] is not None else "0.00"))
        price_entry.grid(row=1, column=0, sticky="we", padx=(0, 10), pady=(0, 10))

        ctk.CTkLabel(
            price_stock_inner_frame, text="Stock*", font=self.ui["font_body"]
        ).grid(row=0, column=1, sticky="w")
        stock_entry = ctk.CTkEntry(price_stock_inner_frame)
        stock_entry.insert(0, str(product[4] if product[4] is not None else "0"))
        stock_entry.grid(row=1, column=1, sticky="we", pady=(0, 10))

        # Define save_changes function here, after all entry widgets are created
        def save_changes():
            name = name_entry.get()
            description = desc_entry.get("1.0", "end-1c")
            price = price_entry.get()
            stock = stock_entry.get()

            if not all([name, price, stock]):
                messagebox.showerror(
                    "Erreur", "Veuillez remplir tous les champs obligatoires"
                )
                return

            try:
                price = float(price)
                stock = int(stock)

                conn = sqlite3.connect("commercial.db")
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

                messagebox.showinfo("Succès", "Produit mis à jour avec succès!")
                self.load_products()  # Rafraîchir la liste
                dialog.destroy()

            except ValueError:
                messagebox.showerror(
                    "Erreur",
                    "Veuillez entrer des valeurs numériques valides pour le prix et le stock",
                )

        # Footer
        footer = ctk.CTkFrame(dialog, fg_color="transparent")
        footer.pack(fill="x", padx=20, pady=(0, 15))

        ctk.CTkButton(
            footer,
            text="Annuler",
            command=dialog.destroy,
            fg_color="gray",
            hover_color="darkgray",
        ).pack(side="right", padx=10)

        ctk.CTkButton(footer, text="Enregistrer", command=save_changes).pack(
            side="right"
        )

        # Set focus after all widgets are configured
        name_entry.focus_set()

    def create_clients_frame(self):
        frame = ctk.CTkFrame(self.main_frame, corner_radius=0, fg_color="transparent")

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 20))

        ctk.CTkLabel(header, text="Gestion des clients", font=self.ui["font_h1"]).pack(
            side="left"
        )

        ctk.CTkButton(
            header,
            text="+ Ajouter un client",
            command=lambda: self.show_client_dialog(None),
        ).pack(side="right")

        search_frame = ctk.CTkFrame(frame, fg_color="transparent")
        search_frame.pack(fill="x", pady=(0, 20))

        self.client_search_entry = ttk.Combobox(
            search_frame, values=[], width=48, state="normal"
        )
        self.client_search_entry.pack(side="left", padx=(0, 10))
        self.client_search_entry.bind("<Return>", self.search_clients)
        self.client_search_entry.bind(
            "<KeyRelease>", self._update_client_search_suggestions
        )
        self.client_search_entry.bind("<<ComboboxSelected>>", self.search_clients)

        ctk.CTkButton(
            search_frame, text="Rechercher", command=self.search_clients
        ).pack(side="left")

        self._update_client_search_suggestions()

        columns = ("ID", "Nom", "Email", "Téléphone", "Adresse")
        self.clients_tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse"
        )

        self.clients_tree.heading("ID", text="ID")
        self.clients_tree.heading("Nom", text="Nom")
        self.clients_tree.heading("Email", text="Email")
        self.clients_tree.heading("Téléphone", text="Téléphone")
        self.clients_tree.heading("Adresse", text="Adresse")

        self.clients_tree.column("ID", width=60, anchor="center")
        self.clients_tree.column("Nom", width=180, anchor="w")
        self.clients_tree.column("Email", width=220, anchor="w")
        self.clients_tree.column("Téléphone", width=140, anchor="center")
        self.clients_tree.column("Adresse", width=260, anchor="w")

        scrollbar = ttk.Scrollbar(
            frame, orient="vertical", command=self.clients_tree.yview
        )
        scrollbar.pack(side="right", fill="y")
        self.clients_tree.configure(yscrollcommand=scrollbar.set)
        self.clients_tree.pack(fill="both", expand=True)

        self.clients_tree.bind("<Double-1>", self.on_client_double_click)
        self.load_clients()

        self.frames["clients"] = frame

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
                if " | " in q:
                    parts = [p.strip() for p in q.split("|")]
                    nom = parts[0] if len(parts) > 0 else ""
                    email = parts[1] if len(parts) > 1 else ""
                    tel = parts[2] if len(parts) > 2 else ""
                    cursor.execute(
                        """
                        SELECT id, nom, email, telephone, adresse
                        FROM clients
                        WHERE nom LIKE ? OR email LIKE ? OR telephone LIKE ?
                        ORDER BY nom
                        """,
                        (f"%{nom}%", f"%{email}%", f"%{tel}%"),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT id, nom, email, telephone, adresse
                        FROM clients
                        WHERE nom LIKE ? OR email LIKE ? OR telephone LIKE ?
                        ORDER BY nom
                        """,
                        (f"%{q}%", f"%{q}%", f"%{q}%"),
                    )
            else:
                cursor.execute(
                    "SELECT id, nom, email, telephone, adresse FROM clients ORDER BY nom"
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
                    ),
                )
        finally:
            conn.close()

        self._apply_zebra(self.clients_tree)

    def search_clients(self, event=None):
        query = ""
        if hasattr(self, "client_search_entry"):
            query = self.client_search_entry.get().strip()
        if not query:
            values = getattr(self.client_search_entry, "cget", lambda _k: [])("values")
            if values:
                self.client_search_entry.set(values[0])
                query = str(values[0]).strip()
        self.load_clients(query if query else None)

    def show_client_dialog(self, client_id=None):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Fiche Client" if client_id else "Nouveau Client")
        dialog.geometry("980x700")
        dialog.transient(self)
        dialog.grab_set()

        nom_value = ""
        email_value = ""
        tel_value = ""
        addr_value = ""

        if client_id:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT nom, email, telephone, adresse FROM clients WHERE id = ?",
                    (client_id,),
                )
                row = cursor.fetchone()
                if row:
                    nom_value = row[0] or ""
                    email_value = row[1] or ""
                    tel_value = row[2] or ""
                    addr_value = row[3] or ""
            finally:
                conn.close()

        # Header Frame
        header = ctk.CTkFrame(dialog, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 10))

        ctk.CTkLabel(
            header,
            text="Fiche Client",
            font=self.ui["font_h1"],
        ).pack(side="left")

        actions = ctk.CTkFrame(header, fg_color="transparent")
        actions.pack(side="right")
        ctk.CTkButton(
            actions,
            text="Documents",
            state="disabled",
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "gray90"),
        ).pack(side="right")

        # Top Info Frame
        top = ctk.CTkFrame(dialog, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(0, 10))

        left = ctk.CTkFrame(top, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        right = ctk.CTkFrame(top, fg_color="transparent")
        right.pack(side="right")

        # Row 0: Nom and Telephone Labels
        ctk.CTkLabel(left, text="Nom*", font=self.ui["font_h2"]).grid(
            row=0, column=0, sticky="w"
        )
        ctk.CTkLabel(left, text="Téléphone", font=self.ui["font_h2"]).grid(
            row=0, column=1, sticky="w", padx=(10, 0)
        )

        # Row 1: Entries
        nom_entry = ctk.CTkEntry(left, width=300)
        nom_entry.insert(0, nom_value)
        nom_entry.grid(row=1, column=0, sticky="w", pady=(0, 8))

        tel_entry = ctk.CTkEntry(left, width=200)
        tel_entry.insert(0, tel_value)
        tel_entry.grid(row=1, column=1, sticky="w", pady=(0, 8), padx=(10, 0))

        # Row 2: Email Label
        ctk.CTkLabel(left, text="Email", font=self.ui["font_h2"]).grid(
            row=2, column=0, sticky="w"
        )

        # Row 3: Email Entry
        email_entry = ctk.CTkEntry(left, width=300)
        email_entry.insert(0, email_value)
        email_entry.grid(row=3, column=0, sticky="w", pady=(0, 8))

        # Row 4: Adresse Label
        ctk.CTkLabel(left, text="Adresse", font=self.ui["font_h2"]).grid(
            row=4, column=0, sticky="w"
        )

        # Row 5: Adresse Entry (wide)
        addr_entry = ctk.CTkEntry(left, width=510)
        addr_entry.insert(0, addr_value)
        addr_entry.grid(row=5, column=0, columnspan=2, sticky="w")

        # Right side status (dummy)
        statut_label = ctk.CTkLabel(
            right,
            text=f"Client #{client_id if client_id else 'New'}",
            font=self.ui["font_h2"],
        )
        statut_label.pack(anchor="e")

        # Content Area (Treeview for History)
        content = ctk.CTkFrame(dialog, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=20, pady=(10, 10))

        ctk.CTkLabel(
            content, text="Historique des documents", font=self.ui["font_h2"]
        ).pack(anchor="w", pady=(0, 5))

        columns = ("Date", "Type", "Numéro", "Total TTC", "Statut")
        history_tree = ttk.Treeview(
            content, columns=columns, show="headings", selectmode="browse"
        )
        for col in columns:
            history_tree.heading(col, text=col)
            history_tree.column(col, anchor="center")

        history_tree.column("Type", width=100)
        history_tree.column("Numéro", width=120)
        history_tree.column("Total TTC", width=100, anchor="e")

        scrollbar = ttk.Scrollbar(
            content, orient="vertical", command=history_tree.yview
        )
        scrollbar.pack(side="right", fill="y")
        history_tree.configure(yscrollcommand=scrollbar.set)
        history_tree.pack(fill="both", expand=True)

        total_spent = 0.0
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
                    # d: date, type, numero, total_ttc, statut
                    dt = d[0]
                    typ = d[1]
                    num = d[2]
                    ttc = d[3] if d[3] else 0.0
                    st = d[4]
                    history_tree.insert(
                        "", "end", values=(dt, typ, num, f"{ttc:.2f}", st)
                    )
                    if typ in ("factures", "reception"):
                        total_spent += ttc
            finally:
                conn.close()

            self._apply_zebra(history_tree)

        # Totals Bar
        totals_bar = ctk.CTkFrame(dialog, fg_color="transparent")
        totals_bar.pack(fill="x", padx=20, pady=(0, 10))

        totals_right = ctk.CTkFrame(totals_bar, fg_color="transparent")
        totals_right.pack(side="right")

        # Total Depensé
        spent_box = ctk.CTkFrame(totals_right, fg_color="transparent")
        spent_box.pack(side="right")
        ctk.CTkLabel(
            spent_box,
            text="Total Dépensé (TTC)",
            font=self.ui["font_body"],
            text_color=("gray35", "gray70"),
        ).pack(anchor="e")
        spent_label = ctk.CTkLabel(
            spent_box, text=f"{total_spent:.2f}", font=self.ui["font_h2"]
        )
        spent_label.pack(anchor="e")

        # Footer
        footer = ctk.CTkFrame(dialog, fg_color="transparent")
        footer.pack(fill="x", padx=20, pady=(0, 15))

        def save_client():
            nom = nom_entry.get().strip()
            email = email_entry.get().strip()
            telephone = tel_entry.get().strip()
            adresse = addr_entry.get().strip()

            if not nom:
                messagebox.showerror("Erreur", "Le nom est obligatoire")
                return

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            try:
                if client_id:
                    cursor.execute(
                        "UPDATE clients SET nom=?, email=?, telephone=?, adresse=? WHERE id=?",
                        (nom, email, telephone, adresse, client_id),
                    )
                else:
                    cursor.execute(
                        "INSERT INTO clients (nom, email, telephone, adresse) VALUES (?, ?, ?, ?)",
                        (nom, email, telephone, adresse),
                    )
                conn.commit()
            finally:
                conn.close()

            self.load_clients()
            dialog.destroy()

        ctk.CTkButton(
            footer,
            text="Fermer",
            command=dialog.destroy,
            fg_color="gray",
            hover_color="darkgray",
        ).pack(side="right", padx=10)

        ctk.CTkButton(footer, text="Enregistrer", command=save_client).pack(
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

    def create_reports_frame(self):
        frame = ctk.CTkFrame(self.main_frame, corner_radius=0, fg_color="transparent")

        ctk.CTkLabel(frame, text="Rapports", font=("Arial", 24, "bold")).pack(pady=20)

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
        frame = ctk.CTkFrame(self.main_frame, corner_radius=0, fg_color="transparent")

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 20))

        ctk.CTkLabel(header, text="Administration", font=("Arial", 24, "bold")).pack(
            side="left"
        )

        tabview = ctk.CTkTabview(frame)
        tabview.pack(fill="both", expand=True)
        tabview.add("Utilisateurs")
        tabview.add("Maintenance")

        # --- Users Tab ---
        users_frame = tabview.tab("Utilisateurs")

        columns = ("ID", "Username", "Email", "Nom", "Admin")
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

        # --- Maintenance Tab ---
        maint_frame = tabview.tab("Maintenance")

        ctk.CTkLabel(
            maint_frame,
            text="Zone de Danger",
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
            text="Réinitialiser TOUTE la base de données",
            command=self.wipe_database,
            fg_color="#FF4B4B",
            hover_color="#FF0000",
            height=40,
        ).pack(anchor="w", padx=20)

        self.frames["admin"] = frame

    def wipe_database(self):
        if not messagebox.askyesno(
            "Confirmation CRITIQUE",
            "Êtes-vous SÛR de vouloir effacer TOUTE la base de données ?\n\nCette action est irréversible !",
            icon="warning",
        ):
            return

        dialog = ctk.CTkInputDialog(
            text="Tapez 'SUPPRIMER' pour confirmer :", title="Dernière confirmation"
        )
        text = dialog.get_input()

        if text != "SUPPRIMER":
            messagebox.showinfo("Annulé", "Opération annulée.")
            return

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # Get all tables
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = cursor.fetchall()

            for table in tables:
                if table[0] != "sqlite_sequence" and table[0] != "users":
                    cursor.execute(f"DROP TABLE IF EXISTS {table[0]}")

            conn.commit()
            conn.close()

            # Re-initialize the database schema
            self.init_db()

            # Refresh all data views to reflect empty database
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

            # Refresh the UI
            messagebox.showinfo("Succès", "Base de données réinitialisée.")
            self.show_frame("admin")

        except Exception as e:
            messagebox.showerror("Erreur", f"Erreur lors de la réinitialisation : {e}")

    def load_users(self):
        for item in self.users_tree.get_children():
            self.users_tree.delete(item)

        conn = sqlite3.connect("commercial.db")
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
                        "Oui" if row[4] else "Non",
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
            "Quitter", "Voulez-vous vraiment quitter l'application ?"
        ):
            self.quit()

import hashlib
import os
import secrets
import sqlite3
import webbrowser
from tkinter import messagebox

import customtkinter as ctk
from PIL import Image, ImageTk

from i18n import get_language, set_language, t

_PBKDF2_ALGO = "pbkdf2"
_PBKDF2_ITERATIONS = 600000

DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "commercial.db"
)


def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return "{}${}${}${}".format(
        _PBKDF2_ALGO, _PBKDF2_ITERATIONS, salt.hex(), dk.hex()
    )


def verify_password(password, stored):
    if not isinstance(stored, str) or not stored:
        return False

    if not stored.startswith(_PBKDF2_ALGO + "$"):
        return secrets.compare_digest(password, stored)

    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$", 3)
        if algo != _PBKDF2_ALGO:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return secrets.compare_digest(dk, expected)
    except Exception:
        return False


def is_password_hashed(stored):
    return isinstance(stored, str) and stored.startswith(_PBKDF2_ALGO + "$")


class LoginWindow(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.geometry("400x560")
        self.resizable(False, False)

        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        base_dir = os.path.dirname(os.path.abspath(__file__))
        assets_dir = os.path.join(base_dir, "assets")
        os.makedirs(assets_dir, exist_ok=True)

        # --- Language switcher (top bar) ---
        self.lang_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.lang_frame.pack(fill="x", padx=10, pady=(8, 0))

        ctk.CTkLabel(self.lang_frame, text=t("login.language_label") + ":", font=("Arial", 11)).pack(side="left", padx=(10, 6))

        self.lang_var = ctk.StringVar(value="EN" if get_language() == "en" else "FR")
        # Use segmented button for EN/FR
        try:
            self.lang_switch = ctk.CTkSegmentedButton(
                self.lang_frame,
                values=["EN", "FR"],
                command=self.on_language_change,
                width=140,
            )
            self.lang_switch.set(self.lang_var.get())
            self.lang_switch.pack(side="right", padx=10)
        except Exception:
            # Fallback to option menu
            self.lang_switch = ctk.CTkOptionMenu(
                self.lang_frame,
                values=["EN", "FR"],
                variable=self.lang_var,
                command=self.on_language_change,
                width=100,
            )
            self.lang_switch.pack(side="right", padx=10)

        logo_path = os.path.join(assets_dir, "logo.png")
        if os.path.exists(logo_path):
            try:
                self.logo_image = ctk.CTkImage(
                    light_image=Image.open(logo_path),
                    size=(150, 150),
                )
                self.logo_label = ctk.CTkLabel(self, image=self.logo_image, text="")
            except Exception as e:
                print(f"Error loading logo: {e}")
                self.logo_label = ctk.CTkLabel(
                    self, text="LOGO", font=("Arial", 24, "bold")
                )
        else:
            print(f"Logo not found at {logo_path}")
            self.logo_label = ctk.CTkLabel(
                self, text="LOGO", font=("Arial", 24, "bold")
            )

        self.logo_label.pack(pady=12)

        self.frame = ctk.CTkFrame(self)
        self.frame.pack(pady=12, padx=40, fill="both", expand=True)

        self.title_label = ctk.CTkLabel(
            self.frame, text=t("login.header_title"), font=("Arial", 20, "bold")
        )
        self.title_label.pack(pady=12, padx=10)

        self.username_entry = ctk.CTkEntry(
            self.frame, placeholder_text=t("login.username_placeholder"), width=250
        )
        self.username_entry.pack(pady=12, padx=10)

        self.password_entry = ctk.CTkEntry(
            self.frame, placeholder_text=t("login.password_placeholder"), show="*", width=250
        )
        self.password_entry.pack(pady=12, padx=10)

        self.login_button = ctk.CTkButton(
            self.frame, text=t("login.button_login"), command=self.login, width=250
        )
        self.login_button.pack(pady=12, padx=10)

        try:
            self.username_entry.bind("<Return>", lambda e: self.password_entry.focus_set())
            self.password_entry.bind("<Return>", lambda e: self.login())

            self.username_entry.focus_set()
        except Exception:
            pass

        try:
            self.about_label = ctk.CTkLabel(
                self.frame,
                text=t("login.about_link"),
                text_color="blue",
                cursor="hand2",
                font=("Arial", 10, "underline"),
            )
            self.about_label.pack(pady=(8, 0))
            self.about_label.bind("<Button-1>", lambda e: self.open_about_dialog())
        except Exception:
            pass

        # Apply initial language to window title
        self.title(t("app.window_title_login"))

        self.init_database()
        self.create_default_admin()

    def on_language_change(self, value):
        # SegmentedButton passes selected value directly
        lang_code = "en" if str(value).upper().startswith("EN") else "fr"
        set_language(lang_code)
        # Update var for option menu path
        try:
            self.lang_var.set("EN" if lang_code == "en" else "FR")
        except Exception:
            pass
        self.apply_language()
        # Also notify any open register window if exists
        try:
            if hasattr(self, "_register_win") and self._register_win and self._register_win.winfo_exists():
                self._register_win.apply_language()
        except Exception:
            pass

    def apply_language(self):
        try:
            self.title(t("app.window_title_login"))
        except Exception:
            pass
        try:
            # lang frame label (first child)
            for child in self.lang_frame.winfo_children():
                if isinstance(child, ctk.CTkLabel):
                    child.configure(text=t("login.language_label") + ":")
                    break
        except Exception:
            pass
        try:
            self.title_label.configure(text=t("login.header_title"))
        except Exception:
            pass
        try:
            self.username_entry.configure(placeholder_text=t("login.username_placeholder"))
        except Exception:
            pass
        try:
            self.password_entry.configure(placeholder_text=t("login.password_placeholder"))
        except Exception:
            pass
        try:
            self.login_button.configure(text=t("login.button_login"))
        except Exception:
            pass
        try:
            self.about_label.configure(text=t("login.about_link"))
        except Exception:
            pass

    def init_database(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            is_admin BOOLEAN DEFAULT 0
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            price REAL NOT NULL,
            stock INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            quantity INTEGER NOT NULL,
            total_price REAL NOT NULL,
            sale_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_id INTEGER,
            FOREIGN KEY (product_id) REFERENCES products (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
        """)

        conn.commit()
        conn.close()

    def create_default_admin(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM users")
        count = cursor.fetchone()[0]

        if count == 0:

            cursor.execute(
            """
                INSERT INTO users (username, password, email, full_name, is_admin)
                VALUES (?, ?, ?, ?, ?)
            """,
            ("admin", hash_password("admin123"), "admin@example.com", "Administrateur", 1),
            )
            conn.commit()

        conn.close()

    def login(self):
        try:
            username = self.username_entry.get()
            password = self.password_entry.get()

            if not username or not password:
                messagebox.showerror(t("common.error"), t("login.error_fill_fields"))
                return

            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
            user = cursor.fetchone()

            if user:
                stored = user[2]
                if verify_password(password, stored):
                    if not is_password_hashed(stored):
                        cursor.execute(
                        "UPDATE users SET password = ? WHERE id = ?",
                        (hash_password(password), user[0]),
                        )
                        conn.commit()
                    conn.close()
                    print(f"User logged in: {username}")
                    self.withdraw()

                    from main_app import MainApp

                    app = MainApp(user, parent=self)

                    app.protocol("WM_DELETE_WINDOW", lambda: self.on_main_app_close(app))
                    return

            conn.close()
            messagebox.showerror(
            t("common.error"), t("login.error_invalid_credentials")
            )

        except Exception as e:
            messagebox.showerror(t("common.error"), t("login.error_generic", error=str(e)))
            print(f"Login error: {str(e)}")

    def on_main_app_close(self, main_app):
        """Handle main app closing"""
        if messagebox.askokcancel(
        t("login.quit_title"), t("login.quit_confirm")
        ):
            main_app.destroy()
            self.destroy()

    def open_about_dialog(self):
        try:
            dialog = ctk.CTkToplevel(self)
            dialog.title(t("about.dialog_title"))
            dialog.geometry("420x220")
            dialog.transient(self)
            dialog.grab_set()

            ctk.CTkLabel(dialog, text=t("about.title"), font=("Arial", 18, "bold")).pack(pady=(16, 4))
            ctk.CTkLabel(dialog, text=t("about.version"), font=("Arial", 12)).pack(pady=(0, 8))

            email_lbl = ctk.CTkLabel(dialog, text="sedikdarragi@gmail.com", text_color="blue", cursor="hand2", font=("Arial", 11, "underline"))
            email_lbl.pack(pady=(4, 2))
            email_lbl.bind("<Button-1>", lambda e: webbrowser.open("mailto:sedikdarragi@gmail.com"))

            gh_lbl = ctk.CTkLabel(dialog, text="https://github.com/SedikDarragi", text_color="blue", cursor="hand2", font=("Arial", 11, "underline"))
            gh_lbl.pack(pady=(2, 8))
            gh_lbl.bind("<Button-1>", lambda e: webbrowser.open("https://github.com/SedikDarragi"))

            ctk.CTkButton(dialog, text=t("common.close"), command=dialog.destroy, fg_color="gray", hover_color="darkgray").pack(pady=8)
        except Exception:
            messagebox.showinfo(t("about.dialog_title"), t("about.fallback_body"))

    def show_register(self):
        win = RegisterWindow(self)
        # keep ref for live language updates
        try:
            self._register_win = win
        except Exception:
            pass


class RegisterWindow(ctk.CTkToplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent

        self.geometry("400x600")
        self.resizable(False, False)

        # Language switcher
        self.lang_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.lang_frame.pack(fill="x", padx=10, pady=(8, 4))
        ctk.CTkLabel(self.lang_frame, text=t("login.language_label") + ":", font=("Arial", 11)).pack(side="left", padx=(10, 6))
        self.lang_var = ctk.StringVar(value="EN" if get_language() == "en" else "FR")
        try:
            self.lang_switch = ctk.CTkSegmentedButton(
                self.lang_frame,
                values=["EN", "FR"],
                command=self.on_language_change,
                width=140,
            )
            self.lang_switch.set(self.lang_var.get())
            self.lang_switch.pack(side="right", padx=10)
        except Exception:
            self.lang_switch = ctk.CTkOptionMenu(
                self.lang_frame,
                values=["EN", "FR"],
                variable=self.lang_var,
                command=self.on_language_change,
                width=100,
            )
            self.lang_switch.pack(side="right", padx=10)

        self.title_label = ctk.CTkLabel(
        self, text=t("register.header_title"), font=("Arial", 20, "bold")
        )
        self.title_label.pack(pady=12, padx=10)

        self.scrollable_frame = ctk.CTkScrollableFrame(self)
        self.scrollable_frame.pack(fill="both", expand=True, padx=20, pady=10)

        self.fullname_entry = ctk.CTkEntry(
        self.scrollable_frame, placeholder_text=t("register.fullname_placeholder"), width=300
        )
        self.fullname_entry.pack(pady=6, padx=10)

        self.email_entry = ctk.CTkEntry(
        self.scrollable_frame, placeholder_text=t("register.email_placeholder"), width=300
        )
        self.email_entry.pack(pady=6, padx=10)

        self.username_entry = ctk.CTkEntry(
        self.scrollable_frame, placeholder_text=t("register.username_placeholder"), width=300
        )
        self.username_entry.pack(pady=6, padx=10)

        self.password_entry = ctk.CTkEntry(
        self.scrollable_frame, placeholder_text=t("register.password_placeholder"), show="*", width=300
        )
        self.password_entry.pack(pady=6, padx=10)

        self.confirm_password_entry = ctk.CTkEntry(
        self.scrollable_frame,
        placeholder_text=t("register.confirm_password_placeholder"),
        show="*",
        width=300,
        )
        self.confirm_password_entry.pack(pady=6, padx=10)

        self.register_button = ctk.CTkButton(
        self.scrollable_frame, text=t("register.button_submit"), command=self.register, width=300
        )
        self.register_button.pack(pady=12, padx=10)

        self.login_label = ctk.CTkLabel(
        self.scrollable_frame,
        text=t("register.already_have_account"),
        text_color="blue",
        cursor="hand2",
        )
        self.login_label.pack(pady=6, padx=10)
        self.login_label.bind("<Button-1>", lambda e: self.destroy())

        self.transient(parent)
        self.grab_set()
        self.title(t("register.window_title"))

    def on_language_change(self, value):
        lang_code = "en" if str(value).upper().startswith("EN") else "fr"
        set_language(lang_code)
        try:
            self.lang_var.set("EN" if lang_code == "en" else "FR")
        except Exception:
            pass
        self.apply_language()
        # also update parent
        try:
            if hasattr(self.parent, "apply_language"):
                self.parent.apply_language()
                # sync parent switch
                try:
                    self.parent.lang_var.set(self.lang_var.get())
                    self.parent.lang_switch.set(self.lang_var.get())
                except Exception:
                    pass
        except Exception:
            pass

    def apply_language(self):
        try:
            self.title(t("register.window_title"))
        except Exception:
            pass
        try:
            for child in self.lang_frame.winfo_children():
                if isinstance(child, ctk.CTkLabel):
                    child.configure(text=t("login.language_label") + ":")
                    break
        except Exception:
            pass
        try:
            self.title_label.configure(text=t("register.header_title"))
        except Exception:
            pass
        try:
            self.fullname_entry.configure(placeholder_text=t("register.fullname_placeholder"))
        except Exception:
            pass
        try:
            self.email_entry.configure(placeholder_text=t("register.email_placeholder"))
        except Exception:
            pass
        try:
            self.username_entry.configure(placeholder_text=t("register.username_placeholder"))
        except Exception:
            pass
        try:
            self.password_entry.configure(placeholder_text=t("register.password_placeholder"))
        except Exception:
            pass
        try:
            self.confirm_password_entry.configure(placeholder_text=t("register.confirm_password_placeholder"))
        except Exception:
            pass
        try:
            self.register_button.configure(text=t("register.button_submit"))
        except Exception:
            pass
        try:
            self.login_label.configure(text=t("register.already_have_account"))
        except Exception:
            pass

    def register(self):
        fullname = self.fullname_entry.get().strip()
        email = self.email_entry.get().strip()
        username = self.username_entry.get().strip()
        password = self.password_entry.get()
        confirm_password = self.confirm_password_entry.get()

        if not all([fullname, email, username, password, confirm_password]):
            messagebox.showerror(t("common.error"), t("register.error_fill_fields"))
            return

        if "@" not in email or "." not in email:
            messagebox.showerror(t("common.error"), t("register.error_invalid_email"))
            return

        if len(password) < 6:
            messagebox.showerror(
            t("common.error"), t("register.error_password_short")
            )
            return

        if password != confirm_password:
            messagebox.showerror(t("common.error"), t("register.error_password_mismatch"))
            return

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        try:
            cursor.execute(
            "INSERT INTO users (username, password, email, full_name, is_admin) VALUES (?, ?, ?, ?, ?)",
            (
            username,
            hash_password(password),
            email,
            fullname,
            0,
            ),
            )
            conn.commit()
            messagebox.showinfo(t("register.success_title"), t("register.success_body"))
            self.destroy()
        except sqlite3.IntegrityError as e:
            if "username" in str(e).lower():
                messagebox.showerror(t("common.error"), t("register.error_username_taken"))
            elif "email" in str(e).lower():
                messagebox.showerror(t("common.error"), t("register.error_email_taken"))
            else:
                messagebox.showerror(t("common.error"), t("register.error_generic", error=str(e)))
        except Exception as e:
            messagebox.showerror(
            t("common.error"), t("register.error_unexpected", error=str(e))
            )
        finally:
            conn.close()


if __name__ == "__main__":
    app = LoginWindow()
    app.mainloop()

import hashlib 
import os 
import secrets 
import sqlite3 
import webbrowser 
from tkinter import messagebox 

import customtkinter as ctk 
from PIL import Image ,ImageTk 


_PBKDF2_ALGO ="pbkdf2"
_PBKDF2_ITERATIONS =600000

DB_PATH =os .path .join (
os .path .dirname (os .path .abspath (__file__ )),"commercial.db"
)


def hash_password (password ):
    salt =secrets .token_bytes (16 )
    dk =hashlib .pbkdf2_hmac (
    "sha256",password .encode ("utf-8"),salt ,_PBKDF2_ITERATIONS
    )
    return "{}${}${}${}".format (
    _PBKDF2_ALGO ,_PBKDF2_ITERATIONS ,salt .hex (),dk .hex ()
    )


def verify_password (password ,stored ):
    if not isinstance (stored ,str ) or not stored :
        return False

    if not stored .startswith (_PBKDF2_ALGO +"$"):
        return secrets .compare_digest (password ,stored )

    try :
        algo ,iterations ,salt_hex ,hash_hex =stored .split ("$",3 )
        if algo !=_PBKDF2_ALGO :
            return False
        salt =bytes .fromhex (salt_hex )
        expected =bytes .fromhex (hash_hex )
        dk =hashlib .pbkdf2_hmac (
        "sha256",password .encode ("utf-8"),salt ,int (iterations )
        )
        return secrets .compare_digest (dk ,expected )
    except Exception :
        return False


def is_password_hashed (stored ):
    return isinstance (stored ,str ) and stored .startswith (_PBKDF2_ALGO +"$")


class LoginWindow (ctk .CTk ):
    def __init__ (self ):
        super ().__init__ ()


        self .title ("Gestion Commerciale - Connexion")
        self .geometry ("400x500")
        self .resizable (False ,False )


        ctk .set_appearance_mode ("light")
        ctk .set_default_color_theme ("blue")


        base_dir =os .path .dirname (os .path .abspath (__file__ ))
        assets_dir =os .path .join (base_dir ,"assets")
        os .makedirs (assets_dir ,exist_ok =True )


        logo_path =os .path .join (assets_dir ,"logo.png")
        if os .path .exists (logo_path ):
            try :
                self .logo_image =ctk .CTkImage (
                light_image =Image .open (logo_path ),
                size =(150 ,150 ),
                )
                self .logo_label =ctk .CTkLabel (self ,image =self .logo_image ,text ="")
            except Exception as e :
                print (f"Error loading logo: {e }")
                self .logo_label =ctk .CTkLabel (
                self ,text ="LOGO",font =("Arial",24 ,"bold")
                )
        else :
            print (f"Logo not found at {logo_path }")
            self .logo_label =ctk .CTkLabel (
            self ,text ="LOGO",font =("Arial",24 ,"bold")
            )

        self .logo_label .pack (pady =20 )


        self .frame =ctk .CTkFrame (self )
        self .frame .pack (pady =20 ,padx =40 ,fill ="both",expand =True )


        self .title_label =ctk .CTkLabel (
        self .frame ,text ="Gestion Commerciale",font =("Arial",20 ,"bold")
        )
        self .title_label .pack (pady =12 ,padx =10 )


        self .username_entry =ctk .CTkEntry (
        self .frame ,placeholder_text ="Nom d'utilisateur",width =250 
        )
        self .username_entry .pack (pady =12 ,padx =10 )

        self .password_entry =ctk .CTkEntry (
        self .frame ,placeholder_text ="Mot de passe",show ="*",width =250 
        )
        self .password_entry .pack (pady =12 ,padx =10 )


        self .login_button =ctk .CTkButton (
        self .frame ,text ="Se connecter",command =self .login ,width =250 
        )
        self .login_button .pack (pady =12 ,padx =10 )




        try :
            self .username_entry .bind ("<Return>",lambda e :self .password_entry .focus_set ())
            self .password_entry .bind ("<Return>",lambda e :self .login ())

            self .username_entry .focus_set ()
        except Exception :
            pass 


        try :
            self .about_label =ctk .CTkLabel (
            self .frame ,
            text ="À propos",
            text_color ="blue",
            cursor ="hand2",
            font =("Arial",10 ,"underline"),
            )
            self .about_label .pack (pady =(8 ,0 ))
            self .about_label .bind ("<Button-1>",lambda e :self .open_about_dialog ())
        except Exception :
            pass 


        self .init_database ()


        self .create_default_admin ()

    def init_database (self ):
        conn =sqlite3 .connect (DB_PATH )
        cursor =conn .cursor ()


        cursor .execute ("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            is_admin BOOLEAN DEFAULT 0
        )
        """)


        cursor .execute ("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            price REAL NOT NULL,
            stock INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)


        cursor .execute ("""
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

        conn .commit ()
        conn .close ()

    def create_default_admin (self ):
        conn =sqlite3 .connect (DB_PATH )
        cursor =conn .cursor ()


        cursor .execute ("SELECT COUNT(*) FROM users")
        count =cursor .fetchone ()[0 ]

        if count ==0 :

            cursor .execute (
            """
                INSERT INTO users (username, password, email, full_name, is_admin)
                VALUES (?, ?, ?, ?, ?)
            """,
            ("admin",hash_password ("admin123"),"admin@example.com","Administrateur",1 ),
            )
            conn .commit ()

        conn .close ()

    def login (self ):
        try :
            username =self .username_entry .get ()
            password =self .password_entry .get ()

            if not username or not password :
                messagebox .showerror ("Erreur","Veuillez remplir tous les champs")
                return

            conn =sqlite3 .connect (DB_PATH )
            cursor =conn .cursor ()

            cursor .execute ("SELECT * FROM users WHERE username = ?",(username ,))
            user =cursor .fetchone ()

            if user :
                stored =user[2]
                if verify_password (password ,stored ):
                    if not is_password_hashed (stored ):
                        cursor .execute (
                        "UPDATE users SET password = ? WHERE id = ?",
                        (hash_password (password ),user[0]),
                        )
                        conn .commit ()
                    conn .close ()
                    print (f"Utilisateur connecté: {username }")
                    self .withdraw ()


                    from main_app import MainApp


                    app =MainApp (user ,parent =self )


                    app .protocol ("WM_DELETE_WINDOW",lambda :self .on_main_app_close (app ))
                    return

            conn .close ()
            messagebox .showerror (
            "Erreur","Nom d'utilisateur ou mot de passe incorrect"
            )

        except Exception as e :
            messagebox .showerror ("Erreur",f"Une erreur est survenue: {str (e )}")
            print (f"Erreur lors de la connexion: {str (e )}")

    def on_main_app_close (self ,main_app ):
        """Gérer la fermeture de l'application principale"""
        if messagebox .askokcancel (
        "Quitter","Voulez-vous vraiment quitter l'application ?"
        ):
            main_app .destroy ()
            self .destroy ()

    def open_about_dialog (self ):
        try :
            dialog =ctk .CTkToplevel (self )
            dialog .title ("À propos - GCS")
            dialog .geometry ("420x220")
            dialog .transient (self )
            dialog .grab_set ()

            ctk .CTkLabel (dialog ,text ="GCS",font =("Arial",18 ,"bold")).pack (pady =(16 ,4 ))
            ctk .CTkLabel (dialog ,text ="Version: 0.1.0 beta",font =("Arial",12 )).pack (pady =(0 ,8 ))


            email_lbl =ctk .CTkLabel (dialog ,text ="sedikdarragi@gmail.com",text_color ="blue",cursor ="hand2",font =("Arial",11 ,"underline"))
            email_lbl .pack (pady =(4 ,2 ))
            email_lbl .bind ("<Button-1>",lambda e :webbrowser .open ("mailto:sedikdarragi@gmail.com"))


            gh_lbl =ctk .CTkLabel (dialog ,text ="https://github.com/SedikDarragi",text_color ="blue",cursor ="hand2",font =("Arial",11 ,"underline"))
            gh_lbl .pack (pady =(2 ,8 ))
            gh_lbl .bind ("<Button-1>",lambda e :webbrowser .open ("https://github.com/SedikDarragi"))

            ctk .CTkButton (dialog ,text ="Fermer",command =dialog .destroy ,fg_color ="gray",hover_color ="darkgray").pack (pady =8 )
        except Exception :
            messagebox .showinfo ("À propos","GCS\nVersion: 0.1.0 beta\nContact: sedikdarragi@gmail.com")

    def show_register (self ):
        RegisterWindow (self )


class RegisterWindow (ctk .CTkToplevel ):
    def __init__ (self ,parent ):
        super ().__init__ (parent )
        self .parent =parent 

        self .title ("Créer un compte")
        self .geometry ("400x550")
        self .resizable (False ,False )


        self .title_label =ctk .CTkLabel (
        self ,text ="Créer un compte",font =("Arial",20 ,"bold")
        )
        self .title_label .pack (pady =12 ,padx =10 )


        self .scrollable_frame =ctk .CTkScrollableFrame (self )
        self .scrollable_frame .pack (fill ="both",expand =True ,padx =20 ,pady =10 )


        self .fullname_entry =ctk .CTkEntry (
        self .scrollable_frame ,placeholder_text ="Nom complet",width =300 
        )
        self .fullname_entry .pack (pady =6 ,padx =10 )

        self .email_entry =ctk .CTkEntry (
        self .scrollable_frame ,placeholder_text ="Email",width =300 
        )
        self .email_entry .pack (pady =6 ,padx =10 )

        self .username_entry =ctk .CTkEntry (
        self .scrollable_frame ,placeholder_text ="Nom d'utilisateur",width =300 
        )
        self .username_entry .pack (pady =6 ,padx =10 )

        self .password_entry =ctk .CTkEntry (
        self .scrollable_frame ,placeholder_text ="Mot de passe",show ="*",width =300 
        )
        self .password_entry .pack (pady =6 ,padx =10 )

        self .confirm_password_entry =ctk .CTkEntry (
        self .scrollable_frame ,
        placeholder_text ="Confirmer le mot de passe",
        show ="*",
        width =300 ,
        )
        self .confirm_password_entry .pack (pady =6 ,padx =10 )


        self .register_button =ctk .CTkButton (
        self .scrollable_frame ,text ="S'inscrire",command =self .register ,width =300 
        )
        self .register_button .pack (pady =12 ,padx =10 )


        self .login_label =ctk .CTkLabel (
        self .scrollable_frame ,
        text ="Déjà un compte ? Se connecter",
        text_color ="blue",
        cursor ="hand2",
        )
        self .login_label .pack (pady =6 ,padx =10 )
        self .login_label .bind ("<Button-1>",lambda e :self .destroy ())

        self .transient (parent )
        self .grab_set ()

    def register (self ):
        fullname =self .fullname_entry .get ().strip ()
        email =self .email_entry .get ().strip ()
        username =self .username_entry .get ().strip ()
        password =self .password_entry .get ()
        confirm_password =self .confirm_password_entry .get ()


        if not all ([fullname ,email ,username ,password ,confirm_password ]):
            messagebox .showerror ("Erreur","Veuillez remplir tous les champs")
            return 

        if "@"not in email or "."not in email :
            messagebox .showerror ("Erreur","Veuillez entrer une adresse email valide")
            return 

        if len (password )<6 :
            messagebox .showerror (
            "Erreur","Le mot de passe doit contenir au moins 6 caractères"
            )
            return 

        if password !=confirm_password :
            messagebox .showerror ("Erreur","Les mots de passe ne correspondent pas")
            return 

        conn =sqlite3 .connect (DB_PATH )
        cursor =conn .cursor ()

        try :
            cursor .execute (
            "INSERT INTO users (username, password, email, full_name, is_admin) VALUES (?, ?, ?, ?, ?)",
            (
            username ,
            hash_password (password ),
            email ,
            fullname ,
            0 ,
            ),
            )
            conn .commit ()
            messagebox .showinfo ("Succès","Compte créé avec succès !")
            self .destroy ()
        except sqlite3 .IntegrityError as e :
            if "username"in str (e ).lower ():
                messagebox .showerror ("Erreur","Ce nom d'utilisateur est déjà utilisé")
            elif "email"in str (e ).lower ():
                messagebox .showerror ("Erreur","Cet email est déjà utilisé")
            else :
                messagebox .showerror ("Erreur",f"Une erreur est survenue : {str (e )}")
        except Exception as e :
            messagebox .showerror (
            "Erreur",f"Une erreur inattendue est survenue : {str (e )}"
            )
        finally :
            conn .close ()


if __name__ =="__main__":
    app =LoginWindow ()
    app .mainloop ()

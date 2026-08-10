import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _CTk:
    class CTk:
        pass

    class CTkToplevel:
        pass

    class CTkFrame:
        pass

    class CTkLabel:
        pass

    class CTkEntry:
        pass

    class CTkButton:
        pass

    class CTkScrollableFrame:
        pass

    class CTkImage:
        pass

    class CTkSwitch:
        pass

    @staticmethod
    def set_appearance_mode(mode):
        return None

    @staticmethod
    def set_default_color_theme(theme):
        return None


sys.modules.setdefault("customtkinter", _CTk())

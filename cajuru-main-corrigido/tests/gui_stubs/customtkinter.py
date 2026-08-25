"""customtkinter falso — a API que o painel do Cajuru A1 usa."""
from __future__ import annotations

from tests.gui_stubs._fake import Fake
from tests.gui_stubs.tkinter import BooleanVar, IntVar, StringVar  # noqa: F401 (reexport)


def set_appearance_mode(_mode):
    return None


def set_default_color_theme(_theme):
    return None


class CTkFont:
    def __init__(self, family=None, size=None, weight=None, **kw):
        self.family, self.size, self.weight = family, size, weight


class CTkFrame(Fake):
    pass


class CTkScrollableFrame(Fake):
    pass


class CTkLabel(Fake):
    pass


class CTkButton(Fake):
    def invoke(self):
        command = self._kw.get("command")
        return command() if callable(command) else None


class CTkEntry(Fake):
    pass


class CTkTextbox(Fake):
    pass


class CTkCheckBox(Fake):
    pass


class CTkSwitch(Fake):
    pass


class CTkProgressBar(Fake):
    pass


class CTkOptionMenu(Fake):
    pass


class CTkComboBox(Fake):
    pass


class CTkSegmentedButton(Fake):
    pass


class CTkTabview(Fake):
    pass


class CTkToplevel(Fake):
    pass


class CTk(Fake):
    """Janela raiz: executa ``after`` na hora, para o teste ser síncrono."""

    def __init__(self, *a, **kw):
        super().__init__(None, **kw)

    def title(self, *a):
        return None

    def geometry(self, *a):
        return None

    def minsize(self, *a):
        return None

    def protocol(self, *a):
        return None

    def after(self, _ms, func=None, *args):
        if callable(func):
            func(*args)
        return "timer"

    def after_cancel(self, *a):
        return None

    def mainloop(self):
        return None

    def update(self):
        return None

    def update_idletasks(self):
        return None

"""tkinter falso — só o que o gui.py do Cajuru A1 importa."""
from tests.gui_stubs._fake import Fake


class Canvas(Fake):
    pass


class Tk(Fake):
    pass


class StringVar:
    def __init__(self, master=None, value=""):
        self._v = value
        self._callbacks = []

    def get(self):
        return self._v

    def set(self, value):
        self._v = value
        for callback in self._callbacks:
            callback()

    def trace_add(self, _mode, callback):
        self._callbacks.append(lambda *a: callback())
        return "trace"

    def trace_remove(self, *a):
        return None


class BooleanVar(StringVar):
    def __init__(self, master=None, value=False):
        super().__init__(master, value)


class IntVar(StringVar):
    def __init__(self, master=None, value=0):
        super().__init__(master, value)


END = "end"

from tests.gui_stubs.tkinter import filedialog, messagebox, ttk  # noqa: E402,F401

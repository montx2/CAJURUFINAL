"""Widget falso usado só nos testes.

Aceita qualquer construtor e qualquer método, guarda os ``configure`` e a
árvore de filhos. Serve para montar a janela do Cajuru A1 sem display,
sem Tcl/Tk instalado e sem abrir nada na tela.
"""
from __future__ import annotations


class Fake:
    def __init__(self, master=None, **kw):
        object.__setattr__(self, "_kw", dict(kw))
        object.__setattr__(self, "_children", [])
        object.__setattr__(self, "_master", master)
        if isinstance(master, Fake):
            master._children.append(self)

    # -- geometria -----------------------------------------------------
    def pack(self, **kw): return None
    def grid(self, **kw): return None
    def place(self, **kw): return None
    def pack_forget(self): return None
    def grid_forget(self): return None
    def pack_propagate(self, *a): return None
    def grid_propagate(self, *a): return None
    def grid_columnconfigure(self, *a, **kw): return None
    def grid_rowconfigure(self, *a, **kw): return None

    # -- estado --------------------------------------------------------
    def configure(self, *a, **kw):
        # ttk.Style.configure recebe o nome do estilo posicionalmente.
        self._kw.update(kw)
    def cget(self, key): return self._kw.get(key)
    def bind(self, *a, **kw): return None
    def winfo_children(self): return list(self._children)
    def winfo_width(self): return 900
    def winfo_height(self): return 400
    def winfo_exists(self): return True

    def destroy(self):
        master = self._master
        if isinstance(master, Fake) and self in master._children:
            master._children.remove(self)

    # -- canvas --------------------------------------------------------
    def delete(self, *a):
        # canvas.delete("all") realmente limpa o que foi desenhado.
        self._kw["_rects"] = []

    def create_rectangle(self, *a, **kw):
        self._kw.setdefault("_rects", []).append((a, kw))
        return len(self._kw["_rects"])

    def rects(self):
        return list(self._kw.get("_rects", []))

    # -- widgets diversos ---------------------------------------------
    def insert(self, *a, **kw): return None
    def see(self, *a): return None
    def get(self, *a, **kw): return ""
    def set(self, *a, **kw): return None
    def start(self): return None
    def stop(self): return None

    def __getattr__(self, name):
        def anything(*a, **kw):
            return None
        return anything

    # -- utilidades de teste -------------------------------------------
    def walk(self):
        """Todos os descendentes, em profundidade."""
        for child in list(self._children):
            yield child
            yield from child.walk()

    def texts(self):
        """Todo texto visível na subárvore — usado nas asserções."""
        out = []
        for widget in self.walk():
            value = widget._kw.get("text")
            if isinstance(value, str) and value.strip():
                out.append(value)
        return out

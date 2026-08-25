"""ttk falso: Treeview, Scrollbar e Style."""
from tests.gui_stubs._fake import Fake


class Treeview(Fake):
    def get_children(self):
        return list(self._kw.get("_rows", []))

    def insert(self, parent, index, **kw):
        self._kw.setdefault("_rows", []).append(kw)
        return f"item{len(self._kw['_rows'])}"

    def delete(self, *items):
        self._kw["_rows"] = []

    def rows(self):
        return list(self._kw.get("_rows", []))


class Scrollbar(Fake):
    pass


class Style(Fake):
    def theme_use(self, *a):
        return None

    def theme_names(self):
        return ["clam", "default"]

    def map(self, *a, **kw):
        return None

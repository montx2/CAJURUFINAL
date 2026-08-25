"""messagebox falso: grava as chamadas em ``calls`` para os testes lerem."""
calls: list[tuple[str, str, str]] = []


def _record(kind, title, message):
    calls.append((kind, title, str(message)))


def showinfo(title, message, **kw):
    _record("info", title, message)
    return "ok"


def showwarning(title, message, **kw):
    _record("warn", title, message)
    return "ok"


def showerror(title, message, **kw):
    _record("error", title, message)
    return "ok"


def askyesno(title, message, **kw):
    _record("yesno", title, message)
    return True

"""filedialog falso: nunca abre janela, devolve o que o teste mandar."""
resposta = ""


def askdirectory(**kw):
    return resposta


def askopenfilename(**kw):
    return resposta


def asksaveasfilename(**kw):
    return resposta

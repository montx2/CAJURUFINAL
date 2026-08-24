from cajuru_a1.cnpjutil import (
    best_doc_from_filename,
    extract_docs_from_text,
    format_cnpj,
    is_valid_cnpj,
    is_valid_cpf,
    pad_cnpj,
)


def test_cnpj_valid():
    assert is_valid_cnpj("50590299000150")
    assert is_valid_cnpj("11.444.777/0001-61")


def test_cnpj_invalid():
    assert not is_valid_cnpj("00000000000000")
    assert not is_valid_cnpj("12345678000100")  # dígitos verificadores errados


def test_cpf_valid():
    assert is_valid_cpf("52998224725")


def test_pad_cnpj_missing_leading_zero():
    assert pad_cnpj("9796329000100") == "09796329000100"  # 13 -> 14


def test_format_cnpj():
    assert format_cnpj("50590299000150") == "50.590.299/0001-50"


def test_extract_from_dirty_filename():
    name = "WM GESTAO EMPRESARIAL LTDA_68620019000174"
    docs = extract_docs_from_text(name)
    assert "68620019000174" in docs


def test_best_doc_ambiguous_returns_none():
    # Dois documentos válidos no mesmo nome -> ambíguo, não escolhe.
    name = "empresa_50590299000150_e_68620019000174"
    assert best_doc_from_filename(name) is None


def test_no_cnpj_in_text_like_matheus_dornas():
    # "MATHEUS DORNAS DINIZ - VENC 27-01-2027" não tem CNPJ.
    assert best_doc_from_filename("MATHEUS DORNAS DINIZ - VENC 27-01-2027") is None

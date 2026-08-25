from cajuru_a1.passwords import PasswordVault, candidate_passwords, PasswordEntry


def test_vault_lookup_by_exact_document():
    vault = PasswordVault()
    vault.add(PasswordEntry("Empresa X", "senha123", "50590299000150"))
    cands = candidate_passwords(vault=vault, cnpj="50590299000150")
    assert any(pwd == "senha123" for pwd, _src in cands)


def test_vault_lookup_by_name():
    vault = PasswordVault()
    vault.add(PasswordEntry("WM GESTAO EMPRESARIAL LTDA", "minhasenha"))
    cands = candidate_passwords(vault=vault, empresa="WM GESTAO EMPRESARIAL LTDA")
    assert any(pwd == "minhasenha" for pwd, _src in cands)


def test_pattern_passwords():
    vault = PasswordVault()
    cands = candidate_passwords(vault=vault, empresa="CAJURU PROCESSAMENTO", years=["26", "2026"])
    assert ("CAJURU26", "padrão:CAJURU+ano") in cands
    assert ("CAJURU2026", "padrão:CAJURU+ano") in cands


def test_empty_password_candidate_always_included():
    vault = PasswordVault()
    cands = candidate_passwords(vault=vault, empresa="Qualquer")
    assert any(pwd == "" for pwd, _src in cands)

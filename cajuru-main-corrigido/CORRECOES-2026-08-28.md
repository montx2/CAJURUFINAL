# Correções de 28/08/2026 — Jettax e interface (v3.2.3)

## 1. ZIP da extração expressa aceito pelo Jettax

O botão **Pegar Certificados + Senhas** usava o nome original que estava no
Dropbox dentro de `todos_certificados_a1.zip`. Por exemplo:

```text
CENTRO DE RADIOLOGIA ODONTOLOGICA LT LTDA_55033273000124.pfx
```

O importador do Jettax exige o nome exato do arquivo no formato abaixo:

```text
55033273000124.pfx
```

Agora o programa lê o **CNPJ validado de dentro do certificado X.509** e cria
o ZIP principal com o nome exato `CNPJ.pfx` (14 dígitos, extensão `.pfx`),
mesmo quando o arquivo original era `.p12` ou tinha razão social no nome.
A planilha oficial e o CSV de senhas usam o mesmo conjunto de CNPJs do ZIP.

### Itens que não podem entrar no Jettax

Para não fazer o Jettax recusar o lote inteiro, certificados abertos que tenham
CPF, CNPJ interno inválido/ausente ou CNPJ duplicado não entram no ZIP principal.
Quando houver algum deles, a pasta da exportação terá:

- `certificados_para_revisao.zip` — cópia para consulta manual; **não importe
  esse ZIP no Jettax**;
- `nao_exportados.csv` — motivo de cada item que ficou fora do ZIP principal.

Quando houver dois certificados para o mesmo CNPJ, o ZIP do Jettax fica com o
mais recente (maior validade; depois início de validade e data do arquivo). O
outro é preservado no ZIP de revisão.

### Certificados vencidos ficam fora do ZIP

Um A1 vencido (ou com início de validade no futuro) não é importável no Jettax,
mas entrava no ZIP e na planilha. Agora ele é tratado como os demais itens fora
do padrão: fica em `certificados_para_revisao.zip` e ganha o motivo em
`nao_exportados.csv` ("O certificado venceu em DD/MM/AAAA…"). A escolha do
certificado mais recente por CNPJ continua igual, só que entre os válidos.

### Onde a pasta da exportação é gravada

```text
Windows: %LOCALAPPDATA%\CajuruA1\output\exportacoes\todos_certificados_<data_hora>\
Linux:   ~/.local/state/cajuru_a1/output/exportacoes/todos_certificados_<data_hora>/
```

Se `armazenamento.saida` estiver preenchido no `config.yaml`, essa pasta entra no
lugar de `%LOCALAPPDATA%\CajuruA1\output`. Nunca é gravado dentro do Dropbox nem
dentro da pasta do programa. A janela abre a pasta sozinha e mostra o caminho
completo no log e no aviso final.

## 2. Janela pequena / conteúdo ocupando só parte da tela

A coluna principal da janela não tinha `weight=1` no grid raiz do Tk. Assim,
quando a janela era maximizada, o painel ficava preso à largura inicial e
sobrava uma área vazia à direita.

A interface agora:

- abre maximizada, com uma geometria inicial segura caso o sistema não permita
  maximizar;
- dá todo o espaço restante à área de conteúdo, enquanto mantém a barra lateral
  fixa;
- ativa DPI awareness no Windows antes de criar a janela, corrigindo a escala
  pequena ou borrada em monitores configurados em 125% ou 150%; e
- corrige a sobreposição de título e subtítulo no cabeçalho e aumenta os alvos
  de clique do menu lateral e da extração expressa.

## Como usar a versão corrigida

1. Instale esta versão fora do Dropbox com `INSTALAR.bat`.
2. Abra `INICIAR.bat`; a janela deve preencher a área de trabalho.
3. Clique **Pegar Certificados + Senhas**.
4. Na pasta aberta ao final, escolha para o Jettax somente:
   - `todos_certificados_a1.zip`; e
   - `planilha_importacao_jettax.xlsx`.
5. Se existirem, resolva antes os itens registrados em `nao_exportados.csv`.
   Nunca envie `certificados_para_revisao.zip` para o Jettax.

> Esta distribuição não inclui certificados reais nem senhas. Por segurança, o
> ZIP de importação é gerado no computador do escritório, a partir da pasta
> Dropbox configurada, que continua sendo usada somente para leitura.

## Validação

```text
30 passed
ruff check cajuru_a1 tests: All checks passed
```

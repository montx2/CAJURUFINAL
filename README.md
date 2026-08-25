# Cajuru A1 v4

Preparação **off-line** de certificados digitais A1 (PFX/P12) para importação em
lote no **Jettax 360** (`Clientes > Importar`).

O programa lê a pasta de certificados e a planilha de senhas, abre cada PFX,
extrai o CNPJ de **dentro** do certificado X.509, escolhe o certificado mais
novo por CNPJ e gera, **fora do Dropbox**:

1. `certificados_jettax.zip` — cada arquivo nomeado **exatamente**
   `<CNPJ>.pfx` (14 dígitos, sem nome de empresa);
2. `planilha_importacao_jettax.xlsx` — modelo oficial do Jettax preenchido com
   **uma linha por CNPJ, sem duplicatas**;
3. `senhas_para_conferencia.csv` — senhas validadas para você conferir/digitar;
4. `rejeitados.csv` — arquivos que **não** entraram no lote e o motivo;
5. `relatorio.html` e `relatorio.xlsx` — visão geral do processamento.

> A pasta do Dropbox é usada **somente para leitura**. Nada é apagado, movido,
> renomeado ou alterado nela. O lote e os relatórios são gerados na pasta de
> saída configurada.

## Correções desta reescrita (v4)

Os erros que o Jettax devolvia — `"CNPJ duplicado na planilha"` e
`"Nome do arquivo não é um CNPJ válido"` — eram causados pela versão antiga:

- o ZIP era montado com o **nome original** do arquivo
  (ex.: `WM GESTAO..._68620019000174.pfx`), que o Jettax não aceita;
- quando havia mais de um PFX para o mesmo CNPJ, entravam **linhas repetidas**
  na planilha.

Agora:

- cada PFX no ZIP chama-se **somente** `<CNPJ>.pfx`;
- há **exatamente uma linha por CNPJ** na planilha (o certificado mais novo
  vence; os outros vão para `rejeitados.csv` como `SUBSTITUIDO`/`DUPLICADO`);
- só entra no lote o certificado cujo **CNPJ interno do X.509** é válido;
- PFX que não abre, está vencido, tem identidade ambígua ou é cópia idêntica
  (mesmo SHA-256) é **rejeitado com motivo**, sem derrubar o lote inteiro;
- a coluna **Senha** da planilha oficial fica em branco por padrão (você digita
  a senha na tela de importação); o CSV de apoio tem as senhas validadas.

## Instalação (Windows)

1. Instale o **Python 3.11+** marcando *Add Python to PATH*.
2. Copie esta pasta para **fora do Dropbox** (ex.: `C:\CajuruA1`).
3. Execute `INSTALAR.bat`.
4. Copie `config.example.yaml` para `config.yaml` e ajuste os caminhos, ou
   configure tudo pela janela do programa.

## Uso

### Painel desktop

```bat
INICIAR.bat
```

1. Selecione a pasta `CERTIFICADOS A1`.
2. Adicione a(s) planilha(s) de senha (colunas: empresa, CNPJ, senha).
3. (Opcional) Escolha a pasta de saída.
4. Clique em **Processar e gerar lote**.
5. Abra a pasta de saída e leve o `certificados_jettax.zip` e a
   `planilha_importacao_jettax.xlsx` ao Jettax em `Clientes > Importar`.

### Linha de comando

```bash
# Cria um config.yaml de exemplo
python run.py init

# Processa e gera o lote
python run.py run --pasta "C:/.../CERTIFICADOS A1" \
    --planilha "C:/.../senhas.xlsx" \
    --saida "C:/CajuruA1/saida"
```

## Como os certificados são abertos

As senhas são testadas na seguinte ordem, **sem força bruta global**:

1. senha da planilha para o **CNPJ exato** (do nome do arquivo);
2. senha da planilha para o **nome exato/nome único** da empresa;
3. padrão `marca + ano` (ex.: `CAJURU26`, `BIO32026`) para os anos configurados;
4. certificado sem senha.

Opcionalmente, pode-se tentar senhas comuns (`123456` etc.) — desligado por
padrão. O limite de tentativas por arquivo é configurável.

## Saída

- Windows: `%LOCALAPPDATA%\CajuruA1\output` (ou a pasta configurada em `saida`);
- Linux: `~/.local/share/cajuru_a1/output`.

Estrutura:

```
output/
  relatorio.html
  relatorio.xlsx
  lotes/
    lote_AAAAMMDD_HHMMSS/
      certificados_jettax.zip
      planilha_importacao_jettax.xlsx
      senhas_para_conferencia.csv
      rejeitados.csv
      LEIA-ME.txt
```

## O que foi removido (em relação à v3)

Esta versão é focada no fluxo manual que você realmente usa. Foram removidos:

- automação de navegador/Jettax (Playwright) — a importação continua manual;
- leitura de PDF/OCR e tratamento de PDFs escaneados;
- inventário/hash de todo o Dropbox e bloqueios por mudança externa;
- banco de checkpoints SQLite;
- servidor web e dependências de CDN;
- interface gráfica pesada em `customtkinter` (agora é Tkinter puro, sem
  dependência extra).

## Testes

```bash
pip install -r requirements.txt pytest
pytest
```

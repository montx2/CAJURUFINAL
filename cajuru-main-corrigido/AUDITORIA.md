# Auditoria extrema — Cajuru A1

**Data da revisão:** 21/08/2026  
**Escopo:** todo o código versionado no commit-base, configuração, inicialização Windows, integração Jettax, leitura local do Dropbox, Excel, PKCS#12, PDFs, relatórios e testes.

## Resumo executivo

A versão recebida **não estava pronta para produção**. Além de problemas de segurança e precisão, o upload anterior ao GitHub havia achatado a estrutura: não existiam os diretórios `cajuru_a1/` e `tests/` esperados pelos imports e pelo `pytest.ini`. Portanto, o programa e a suíte não podiam ser executados a partir do checkout.

A arquitetura aproveitável foi preservada (separação conceitual entre PFX, matching, Jettax e relatório), mas as fronteiras críticas foram endurecidas. A regra atual é deliberadamente conservadora:

> **Somente CNPJ/CPF válido extraído de dentro do X.509, único nos dois lados e sem conflito, pode produzir `PRONTO`. Nome, fuzzy e CNPJ apenas no arquivo produzem `REVISÃO MANUAL`.**

A suíte final possui **71 testes automatizados** e passa integralmente. Os testes usam PFX, CNPJ, Excel e PDF sintéticos; nenhum segredo real foi incluído no repositório.

## Problemas encontrados e tratamento

### Críticos — corrigidos

1. **Checkout inexecutável:** módulos e testes estavam todos no diretório raiz, embora importassem `cajuru_a1.*` e o pytest procurasse `tests/`.  
   **Impacto:** aplicação e testes não iniciavam.  
   **Correção:** estrutura de pacote e testes reconstruída; ZIP redundante/obsoleto removido.

2. **CNPJ apenas no nome do arquivo autorizava envio com confiança 100.**  
   **Impacto:** um arquivo mal nomeado podia ser associado à empresa errada.  
   **Correção:** apenas documento interno do certificado autoriza; nome do arquivo vale 75 e exige revisão.

3. **Correspondência fuzzy por nome podia produzir `PRONTO`.**  
   **Impacto:** empresas semelhantes podiam receber o certificado errado.  
   **Correção:** nome exato/normalizado/fuzzy é somente triagem manual, com pontuação e margem explicadas.

4. **Extração antiga de CNPJ concatenava todos os dígitos de um texto e deslizava janelas.**  
   **Impacto:** datas, protocolos e notas podiam formar por acaso um CNPJ válido inexistente.  
   **Correção:** somente sequências contíguas ou máscaras delimitadas são aceitas; múltiplos documentos tornam o nome ambíguo.

5. **Varredura global de todas as senhas de empresas diferentes vinha habilitada.**  
   **Impacto:** tentativas não plausíveis, associação de origem enganosa e custo imprevisível.  
   **Correção:** desabilitada e bloqueada por padrão; a GUI mantém o modo conservador; limite rígido de tentativas.

6. **Gerador produzia padrões genéricos como `SERVICOS26` e combinações amplas.**  
   **Impacto:** colisão entre empresas e excesso de tentativas.  
   **Correção:** apenas a marca significativa principal + anos explicitamente configurados.

7. **Inventário verificava somente PFX já existentes.** Não detectava arquivo novo, diretório, PDF, movimento ou mudança de permissão.  
   **Impacto:** a alegação “Dropbox não alterado” era incompleta.  
   **Correção:** inventário integral relativo, SHA-256 de todos os arquivos, tipo/modo/mtime, comparação de criado/excluído/modificado/movido e alerta bloqueante.

8. **Lote com planilha de senhas ficava persistente em `output/`, inclusive em simulação.** A função de limpeza existia, mas não era chamada.  
   **Impacto:** senha em texto puro no disco por tempo indefinido.  
   **Correção:** simulação não cria lote; envio real usa diretório transitório de permissão restrita e limpeza em `finally`, com erro crítico se a limpeza falhar.

9. **`ignore_https_errors=True` no navegador.**  
   **Impacto:** permitia conexão TLS não confiável ao transmitir credenciais/certificados.  
   **Correção:** validação TLS obrigatória e host fixo `https://admin.jettax360.com.br`.

10. **Automação individual podia abrir linha pelo nome e escolher o primeiro campo de arquivo/último campo de senha.**  
    **Impacto:** risco de editar cliente ou campo errado.  
    **Correção:** busca de linha somente por documento; ambiguidade bloqueia; CNPJ da tela é reconfirmado; campos e botão precisam ser únicos; sucesso precisa aparecer na UI.

11. **CLI de lote podia declarar `zip_pronto` sem envio confirmado.**  
    **Impacto:** relatório falso de sucesso.  
    **Correção:** confirmação visual explícita ou confirmação manual; sem ambas, ocorre falha.

12. **A GUI sobrescrevia seu próprio handler seguro de fechamento.**  
    **Impacto:** podia encerrar no meio do processamento e pular inventário/limpeza.  
    **Correção:** único handler bloqueia fechamento durante tarefa e executa verificação final.

### Altos — corrigidos

13. **Leitor Excel aceitava silenciosamente colunas A/B quando não encontrava cabeçalho.**  
    **Impacto:** conteúdo arbitrário podia virar empresa/senha.  
    **Correção:** aba sem cabeçalho reconhecido é ignorada e auditada.

14. **Excel carregava abas inteiras em memória e não relatava vazios, duplicatas, conflitos ou senha numérica.**  
    **Correção:** leitura streaming, busca de cabeçalho nas primeiras 100 linhas, todas as abas, ordem flexível e achados por arquivo/aba/linha sem revelar senha.

15. **Cópia temporária podia apagar um destino preexistente se `open(..., "xb")` falhasse.**  
    **Correção:** só remove arquivo comprovadamente criado pela própria tentativa; destino preexistente permanece intacto.

16. **Limpeza aceitava qualquer pasta chamada `cajuru_a1_*`.**  
    **Impacto:** chamada errada podia apagar diretório alheio.  
    **Correção:** exige prefixo, localização no TEMP e marcador aleatório criado pelo aplicativo.

17. **Saída padrão era relativa ao diretório do programa.** Se o programa estivesse no Dropbox, relatórios/logs alterariam o Dropbox.  
    **Correção:** saída/estado em `%LOCALAPPDATA%/CajuruA1` no Windows (ou XDG state no Linux); qualquer destino dentro de uma árvore chamada Dropbox é recusado.

18. **Senha de login Jettax podia ser persistida em YAML.**  
    **Correção:** `save_config` sempre remove senha/token; login assistido é padrão; login automático usa `JETTAX_PASSWORD` no ambiente.

19. **Relatório Excel aceitava fórmula em nome de empresa/arquivo.**  
    **Impacto:** CSV/Excel formula injection ao abrir o relatório.  
    **Correção:** strings iniciadas por `=`, `+`, `-`, `@`, tab ou CR são neutralizadas; HTML possui escaping.

20. **PFX vazio/corrompido/grande e senha errada não tinham estados suficientemente claros; arquivo enorme era lido inteiro.**  
    **Correção:** limites configuráveis, checagem estrutural inicial, códigos de erro, tentativas limitadas e suporte correto a PKCS#12 sem senha.

21. **e-CNPJ com CPF do responsável no SAN podia ser tratado como múltiplas identidades.**  
    **Correção:** CNPJ interno tem precedência; CPF coexistente do responsável não é conflito; múltiplos CNPJs continuam bloqueados.

22. **Falha de cópia fazia o arquivo desaparecer da análise.**  
    **Correção:** todo arquivo listado gera registro de erro/revisão, sem falha silenciosa.

### Médios — corrigidos

23. Cabeçalhos Excel com acento, pontuação, colunas trocadas, linhas de título e várias abas agora são tratados.
24. Duplicatas byte a byte de PFX e PDF são identificadas por SHA-256.
25. PDF normal, protegido, corrompido, grande, sem texto e escaneado possui estado explícito. OCR é limitado e opcional.
26. Nome dentro de PDF é aceito somente por igualdade de linha normalizada com empresa já conhecida; múltiplos nomes/documentos pedem revisão.
27. Checkpoints SQLite registram execução e arquivo sem senha; execução abandonada vira `interrompida`; metadados seguros de PDF podem ser reutilizados por hash.
28. Contadores antigos da GUI podiam sobreviver a uma nova conciliação. Agora são recalculados.
29. URL antiga `jetax` continua corrigida, mas qualquer outro host/esquema é recusado.
30. `.gitignore` estava versionado com o nome `download`; foi restaurado e ampliado para segredos/artefatos.

## Arquitetura final

- `config.py`: defaults, validação, segredos por ambiente e caminhos locais seguros.
- `dropbox_safe.py`: única capacidade sobre a origem; lista/abre/hash/copia somente para fora; barreiras explícitas contra escrita.
- `audit.py`: inventário, comparação e JSON de decisões/integridade.
- `excel_passwords.py` + `passwords.py`: ingestão auditável e cofre exclusivamente em memória.
- `pfx.py`: abertura limitada de PKCS#12 e identidade X.509/ICP-Brasil.
- `pdf.py`: evidência documental limitada; PDF nunca é certificado elegível para upload.
- `matcher.py`: motor de confiança e decisão conservadora.
- `state.py`: checkpoints SQLite sem segredos.
- `pipeline.py`: orquestração, barreiras antes/depois, simulação e envio.
- `jettax.py`: adaptador de UI com host/TLS/identidade/confirmacão.
- `report.py`: relatórios explicáveis e protegidos contra fórmula/HTML injection.
- `lote.py`: pacote Jettax transitório e limpeza obrigatória.
- `gui.py` / `__main__.py`: interfaces; não contêm regra de matching.

## Matriz de confiança

| Evidência | Confiança | Resultado automático |
|---|---:|---|
| CNPJ/CPF interno do X.509 = documento único do Jettax | 100 | `PRONTO`, se chave/validade/senha e demais barreiras estiverem OK |
| Nome exato dentro do X.509, sem documento interno | até 98 | `REVISÃO MANUAL` |
| Nome normalizado/fuzzy único, com margem | menor | `REVISÃO MANUAL` |
| CNPJ somente no nome do arquivo | 75 | `REVISÃO MANUAL` |
| Nome somente no arquivo | até 80 | `REVISÃO MANUAL` |
| Ambiguidade, duplicidade ou conflito | — | bloqueado |

## Testes criados/ampliados

A suíte cobre, entre outros:

- CNPJ válido, inválido, zero à esquerda, formatado, colado e não concatenação de números independentes;
- Unicode, acentos, hífens, espaços e números em nomes;
- empresas muito semelhantes, cliente/CNPJ duplicado e margem insuficiente;
- senha por CNPJ, nome exato, abreviação, conflito, ausência, padrão e bloqueio de varredura global;
- Excel com colunas trocadas, acentos, título, várias abas, sem cabeçalho, vazio, duplicado, conflitante, numérico, ausente e corrompido;
- PFX normal, sem senha, senha correta/incorreta, corrompido, grande, conflito nome × X.509, chave privada, validade e duplicidade;
- e-CNPJ contendo também CPF do responsável;
- PDF normal, protegido certo/errado, escaneado com OCR injetado, sem texto, corrompido, grande, nome/CNPJ e duplicidade;
- inventário criado/excluído/modificado/movido, inclusive conteúdo oculto;
- cópia interrompida/retry, destino preexistente e limpeza marcada;
- checkpoint interrompido e remoção de chaves de segredo;
- dry-run sem ZIP, ausência de senha em relatório/auditoria/SQLite;
- fórmula Excel e escaping HTML;
- detecção de mudança externa no fechamento e limpeza mesmo após alerta.

## Resultado dos testes

```text
71 passed
pip check: No broken requirements found.
Ruff (erros de execução F/E9): All checks passed.
```

## Riscos e limitações remanescentes

1. **Os dois Excel reais, PFX/PDF reais e a conta Jettax não estavam no repositório.** Não foi possível afirmar quais abas/colunas/duplicidades existem nos dados de produção. A primeira execução gera essa auditoria no relatório.
2. **Não houve teste live no Jettax.** A interface de um serviço de terceiros pode mudar; qualquer seletor ambíguo agora bloqueia, mas a homologação em `dry_run` é obrigatória.
3. **Modo Dropbox é pasta local sincronizada, não API.** Não há rate limit HTTP ou download remoto neste projeto. Retry/integridade cobrem leitura local/hidratação; o SDK e token antigos não são usados.
4. **O processo do sistema operacional ainda roda com a permissão do usuário.** A aplicação não oferece escrita e detecta mudanças, mas não pode impedir que o cliente Dropbox, outro programa ou o próprio usuário altere a origem. Para defesa adicional, conceda permissão de leitura no sistema de arquivos à conta que executa o app.
5. **Existe uma janela temporal após o último inventário.** A garantia vale para o intervalo inventariado; uma alteração externa posterior só será detectada na próxima barreira/execução.
6. **PKCS#12 pode retornar o mesmo erro OpenSSL para senha errada e alguns tipos de corrupção.** O estado conservador é `senha não encontrada ou PFX ilegível`, nunca autorização.
7. **OCR depende do executável Tesseract e fica desligado por padrão.** Sem OCR, PDF escaneado vai para revisão manual. OCR nunca autoriza upload.
8. **Segredos não são persistidos para retomar PFX aberto após reinício.** PDFs e estados não secretos retomam por hash; PFX precisa revalidar a senha, escolha intencional para não gravá-la.
9. **O formato oficial de lote exige senha em texto dentro do XLSX/ZIP.** O artefato existe somente durante o envio, com permissão restrita e limpeza obrigatória; uma queda abrupta do SO ainda pode deixar um diretório TEMP marcado, que deve ser tratado como segredo.
10. **Inventário integral é O(total de bytes).** É mais seguro e suporta milhares de arquivos, mas árvores muito grandes dependem da velocidade do disco/Dropbox. Processamento é propositalmente limitado para evitar exaustão de memória.

## Confirmação de proteção Dropbox

No código entregue:

- `READ_ONLY_MODE` é constante e obrigatório;
- não há SDK/token Dropbox nem chamada de delete/move/upload;
- origem é aberta com `O_RDONLY` e, quando disponível, `O_NOFOLLOW`;
- destino dentro da origem é recusado;
- saída, estado, logs e lote são recusados dentro de árvore Dropbox;
- inventários antes/depois incluem criação, exclusão, modificação e movimento;
- qualquer diferença bloqueia a operação e gera alerta/auditoria;
- o resultado sintético validado foi: **alterados=0, excluídos=0, movidos=0, criados=0**.

A confirmação sobre a **pasta real** deve ser obtida executando `--analisar` com os caminhos reais e conferindo `auditoria_ultima_execucao.json`; ela não pode ser fabricada sem acesso aos dados reais.

## Atualização posterior — pasta `tests/` removida

A pedido do escritório, a suíte automatizada (`tests/`) e o `pytest` foram removidos do
repositório e do instalador para deixar o pacote enxuto para uso real. Os **71 testes não
existem mais** e não protegem mais contra regressão — qualquer alteração futura no código
precisa ser validada manualmente, sempre com `dry_run: true` primeiro, antes de qualquer
envio real ao Jettax. A cobertura de risco descrita no item 2 de "Riscos e limitações
remanescentes" (nenhum teste live contra o Jettax real) passa a ser, na prática, a única
forma de validação disponível.

Nesta mesma revisão, o modo de envio `individual` foi corrigido: antes, a falha de UM
certificado interrompia o lote inteiro; agora cada certificado é enviado em um bloco
try/except isolado — uma falha é registrada (com print de tela em
`output/erro_envio_<CNPJ>.png`) e o programa segue para o próximo cliente, exatamente como
pedido em "REGRAS QUE NÃO PODEM SER QUEBRADAS".

## Atualização posterior — v3.2 (23/08/2026)

Revisão focada no painel web e na qualidade geral do repositório, a pedido do escritório.

**Motor de conciliação:**
- Desempate automático por nome (`matcher.py`) quando o CNPJ do X.509 aponta para mais
  de um cliente Jettax com o mesmo documento (erro de cadastro). Usa os mesmos limiares
  de `PasswordVault.lookup_by_name` (≥94% de similaridade, ≥8% de margem do segundo
  colocado). Quando não há um único candidato claro, o caso permanece `AMBÍGUO` — a
  evidência de nome nunca autoriza envio sozinha, só desempata entre candidatos que já
  compartilham o CNPJ verificado no certificado. Coberto por 2 novos testes em
  `tests/test_selector_matcher.py`.

**Painel web (`webapp.py`):**
- Corrigido um problema real: os botões do painel web nunca conectavam de fato ao
  Jettax para listar clientes, então nenhum certificado conseguia ficar `PRONTO` por
  ali (tudo virava `extra_pfx`). Agora "Analisar", "Gerar lote" e "Rodar tudo" abrem o
  Chrome do Jettax (login assistido, leitura apenas), listam os clientes reais e só
  então conciliam — replicando o "Fluxo completo" que já existia na GUI de mesa.
- Foi avaliada (e depois revertida, a pedido do escritório) uma opção de envio
  automático direto ao Jettax pelo painel web (`enviar()` em modo `individual`). A
  decisão final foi manter o painel web **somente leitura/conciliação**: ele gera o
  lote (ZIP + planilha + CSV de senhas) e o envio ao Jettax continua sempre manual,
  feito pela pessoa. `opcoes.dry_run` permanece `true` por padrão.
- A regeneração duplicada (e mais fraca, sem histórico de execuções anteriores) do
  `diagnostico.html`/`.xlsx` dentro do painel web foi removida; o painel agora
  reaproveita o checkpoint da própria análise para trazer o "histórico anterior" no
  diagnóstico, exatamente como o pipeline interno já fazia.
- Corrigidos dois bugs de front-end: o log de execução só era limpo uma única vez no
  carregamento da página (execuções seguintes ficavam com o log antigo misturado ao
  novo) e os botões de ação não travavam entre si (dava para clicar em duas ações ao
  mesmo tempo). Agora todo novo job limpa o log e trava os botões de ação até terminar.

**Visual (painel web e relatórios gerados):**
- Redesenho completo do painel web: paleta neutra com um único acento (índigo),
  ícones em SVG no lugar de emoji, barra de "saúde dos certificados" no Dashboard e em
  Certificados. Confirmado sem emoji em nenhuma tela do painel web.
- `relatorio.html` (gerado por `report.py`) foi redesenhado para usar exatamente a
  mesma paleta/tipografia do painel — antes era a única tela ainda com fundo claro,
  destoando do resto.
- `diagnostico.html` (gerado por `diagnostico.py`) teve sua paleta de cores realinhada
  aos mesmos tokens do painel (antes usava um azul-marinho ligeiramente diferente).
- Corrigido um bug visual: o filtro de status na página Certificados esticava 100% da
  largura da tela por causa de uma regra CSS genérica sem `max-width`.

**Qualidade do código:**
- `ruff check cajuru_a1/` zerado: removidos imports não usados e variáveis de loop
  ambíguas (`l` → `linha`, jettax.py) já existentes em `diagnostico.py` e `jettax.py`
  antes desta revisão.
- Validado com screenshots reais (Playwright headless) de todas as telas do painel web
  e dos três relatórios gerados, sem erros de JavaScript no console.

```text
11 passed
ruff check cajuru_a1/: All checks passed
```

## Atualização posterior — v3.2.1 (23/08/2026): bugs achados na primeira rodada real

O escritório rodou "Rodar tudo agora" pela primeira vez em dados reais (499 PFX, 152
clientes sem A1, modo `atualizar_todas_empresas` ligado) e colou o log completo para
revisão. Dois problemas reais foram encontrados e corrigidos a partir desse log:

**1. Filtro "com certificado" do Jettax sem verificação (`jettax.py`) — gravidade média.**
`_list_by_certificate_filter(valid_certificate=True)` só confirmava que "alguma" lista
de clientes tinha renderizado (`_wait_client_list_ready`), sem checar que a URL de fato
refletia `validCertificate=true` — diferente do caminho "sem certificado", que já exigia
essa prova e travava com `RuntimeError` se não conseguisse confirmar. No log real, as
duas listas vieram com exatamente 152 clientes cada, o que é o sintoma clássico desse
tipo de falha (o filtro "com certificado" pode ter caído de volta na mesma tela "sem
certificado"). Corrigido em duas camadas:
  - `_filter_is_verified()` passou a aceitar o resultado esperado e é chamado também no
    caminho `valid_certificate=True`, travando com erro claro se a URL não confirmar.
  - `list_all_clients()` ganhou uma checagem extra: se as duas listas devolvidas
    tiverem ≥90% de sobreposição de CNPJ, a função trava com `RuntimeError` em vez de
    devolver dados possivelmente errados ao matcher — um cliente não pode estar
    simultaneamente "sem" e "com" certificado válido.
  - Cobertura de teste: `tests/test_lote_diagnostico_web.py::test_list_all_clients_detecta_filtro_nao_aplicado`
    e `test_list_all_clients_ok_quando_listas_diferentes`.
  - Importante: isso não colocava em risco a regra de ouro (nunca enviar certificado
    para o CNPJ errado — essa proteção é feita pelo CNPJ do X.509, não pela lista "com
    certificado"), mas podia fazer o modo de renovação classificar clientes errado
    quanto a "já tem A1 ou não".

**2. Log ao vivo dizia "OK" para certificados com conflito de identidade (`pipeline.py`)
— gravidade baixa (confiança/clareza, não segurança).** A linha de progresso por
arquivo (`3/5 — Copiando e inspecionando PFX/P12…`) imprimia "OK" sempre que a senha
abria o PFX, mesmo quando `info.identity_conflict` estava `True` (CNPJ do nome do
arquivo diferente do CNPJ interno do certificado, ou mais de um documento no mesmo
certificado). No log real, isso apareceu dezenas de vezes como
`OK — CNPJ do nome do arquivo difere do CNPJ interno do certificado`, o que é enganoso:
esses certificados sempre viram `CONFLITO` no resultado final e nunca ficam PRONTO
(`matcher._classify()` já verificava `identity_conflict` primeiro, antes de qualquer
outra coisa — a lógica de bloqueio sempre esteve correta, só o texto do log mentia).
Corrigido: a linha agora distingue três estados — `OK`, `CONFLITO` e `REVISÃO MANUAL` —
em vez de só dois.

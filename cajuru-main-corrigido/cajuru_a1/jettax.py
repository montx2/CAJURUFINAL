"""Automação do Jettax 360 — lista 'sem certificado' e envio do A1.

Não cria cliente novo. Só edita quem já existe e está sem certificado.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from cajuru_a1.cnpjutil import format_cnpj, is_valid_doc, only_digits, pad_cnpj
from cajuru_a1.config import get_output_dir
from cajuru_a1.models import JetaxClient, MatchResult

log = logging.getLogger("cajuru_a1.jettax")

Progress = Callable[[str], None]

CORRECT_URL = "https://admin.jettax360.com.br"
OLD_WRONG_HOSTS = ("admin.jetax360.com.br",)  # um T a menos — nunca usar


def _normalize_jettax_url(url: str | None) -> str:
    raw = (url or "").strip()
    if not raw:
        return CORRECT_URL
    low = raw.lower().rstrip("/")
    if any(host in low for host in OLD_WRONG_HOSTS):
        return CORRECT_URL
    parsed = urlparse(raw)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() != "admin.jettax360.com.br":
        raise ValueError("Host Jettax não autorizado; use https://admin.jettax360.com.br")
    return CORRECT_URL


def _is_closed_error(exc: Exception) -> bool:
    """Detecta erros de navegador/página fechados em qualquer build do Playwright."""
    name = type(exc).__name__
    if name in ("TargetClosedError", "Error") and "closed" in str(exc).lower():
        return True
    text = str(exc).lower()
    return (
        "target page, context or browser has been closed" in text
        or "browser has been closed" in text
        or "target closed" in text
        or "connection closed" in text
    )


class JettaxBot:
    def __init__(self, cfg: dict, log_fn: Progress | None = None):
        self.cfg = cfg
        self.jt = cfg.get("jettax", {})
        self.base = _normalize_jettax_url(self.jt.get("url"))
        self.log_fn = log_fn or (lambda m: log.info(m))
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._captured_clients: list[dict] = []

    def _say(self, msg: str) -> None:
        self.log_fn(msg)
        log.info(msg)

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        if self.playwright is None:
            self.playwright = sync_playwright().start()
        headless = bool(self.jt.get("headless"))
        profile = Path.home() / ".cajuru_a1" / "chrome-profile"
        profile.mkdir(parents=True, exist_ok=True)
        args = [
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ]

        def _finalize_opened() -> None:
            if self.page is not None:
                try:
                    self.page.on("response", self._on_response)
                except Exception:
                    pass
            self._say("Navegador do Jettax pronto.")

        last_err: Exception | None = None
        for channel in (None, "chrome", "msedge"):
            kwargs = dict(
                user_data_dir=str(profile),
                headless=headless,
                locale="pt-BR",
                viewport={"width": 1440, "height": 900},
                ignore_https_errors=False,
                args=args,
                accept_downloads=True,
            )
            if channel:
                kwargs["channel"] = channel
            try:
                self.context = self.playwright.chromium.launch_persistent_context(**kwargs)
                self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
                if not self._browser_alive():
                    raise RuntimeError(
                        "o navegador abriu e fechou em seguida (o perfil pode estar em uso por outra janela do Chrome)"
                    )
                self._say(f"Chrome aberto ({channel or 'playwright'}).")
                _finalize_opened()
                return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                self._say(f"Não abriu perfil persistente ({channel or 'playwright'}): {exc}")
                self._reset()
        for channel in (None, "chrome", "msedge"):
            launch_kw = {"headless": headless, "args": args}
            if channel:
                launch_kw["channel"] = channel
            try:
                self.browser = self.playwright.chromium.launch(**launch_kw)
                self.context = self.browser.new_context(
                    locale="pt-BR",
                    viewport={"width": 1440, "height": 900},
                    ignore_https_errors=False,
                )
                self.page = self.context.new_page()
                if not self._browser_alive():
                    raise RuntimeError("o navegador abriu e fechou em seguida")
                self._say(f"Chrome aberto sem perfil ({channel or 'playwright'}).")
                _finalize_opened()
                return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                self._say(f"Falha launch {channel or 'playwright'}: {exc}")
                self._reset()
        raise RuntimeError(f"Não foi possível abrir o Chrome: {last_err}") from last_err

    def _browser_alive(self) -> bool:
        """True se o contexto/navegador ainda responde a comandos."""
        try:
            if self.context is not None:
                _probe = self.context.pages  # chamada IPC: falha se morreu
                return True
            if self.browser is not None:
                return bool(self.browser.is_connected())
        except Exception:
            return False
        return False

    def _reset(self) -> None:
        """Descarta contexto/navegador atuais sem parar o driver Playwright."""
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        self.context = None
        self.browser = None
        self.page = None

    def _restart_browser(self, motivo: str) -> bool:
        """Reabre o Chrome depois de uma queda/fechamento, sem derrubar o lote."""
        self._say(
            f"Navegador do Jettax caiu/foi fechado ({motivo}). "
            "Reabrindo o Chrome automaticamente — NÃO feche a janela dele."
        )
        self._reset()
        time.sleep(1)
        try:
            self.start()
            return True
        except Exception as exc:  # noqa: BLE001
            self._say(f"Não consegui reabrir o navegador: {exc}")
            return False

    def _on_response(self, response) -> None:
        try:
            url = response.url.lower()
            ct = (response.headers or {}).get("content-type", "")
            if "application/json" not in ct:
                return
            if not any(k in url for k in ("client", "company", "empresa", "taxpayer")):
                return
            if response.status != 200:
                return
            data = response.json()
            rows = _json_to_rows(data)
            if rows:
                self._captured_clients.extend(rows)
        except Exception:
            return

    def open_site(self) -> None:
        assert self.page
        self._goto_safe(self.base)
        self._say(f"Página atual: {self.page.url}")

    def login(self, wait_manual_seconds: int = 600, wait_fn=None) -> None:
        """Abre o Jettax. wait_fn, se existir, bloqueia até o usuário confirmar o login."""
        self.open_site()
        email = self.jt.get("email") or ""
        senha = self.jt.get("senha") or ""
        mode = (self.jt.get("login") or "assisted").lower()
        if mode != "assisted" and email and senha:
            self._try_fill_login(email, senha)
        self._say("Faça login no Chrome se a tela pedir. Depois confirme na janela do Cajuru A1.")
        if wait_fn is not None:
            wait_fn()
            if not self._browser_alive():
                # O usuário pode ter fechado a janela do Chrome enquanto fazia
                # login. Reabrimos e voltamos para o site (o perfil persistente
                # costuma manter a sessão logada).
                if self._restart_browser("fechado durante o login manual"):
                    self._goto_safe(self.base)
                    self._say("Navegador reaberto — se pedir login de novo, faça login antes de continuar.")
                else:
                    raise RuntimeError(
                        "A janela do Chrome foi fechada durante o login e não foi possível "
                        "reabrí-la. Rode de novo sem fechar a janela do Chrome."
                    )
            self._say(f"Login confirmado. URL={self.page.url}")
            return
        self._wait_logged_in(wait_manual_seconds)

    def _goto_safe(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "").casefold() != "admin.jettax360.com.br":
            raise RuntimeError(f"Navegação bloqueada para host não autorizado: {parsed.hostname or url}")
        if self.page is None or not self._browser_alive():
            # Navegador nem existe mais (fechado antes da navegação): reabre.
            if not self._restart_browser("navegador não está mais aberto"):
                raise RuntimeError(
                    "A janela do Chrome (o robô que trabalha no Jettax) não está aberta e não foi "
                    "possível reabrí-la. Não feche essa janela durante a execução; feche outras "
                    "janelas do Chrome que usem o perfil do programa e rode de novo."
                )
        last = self._goto_attempts(url)
        if last is not None and _is_closed_error(last):
            # A janela foi fechada (pelo usuário ou por queda do Chrome).
            # Reabrimos UMA vez e continuamos em vez de derrubar o trabalho.
            if self._restart_browser(type(last).__name__):
                last = self._goto_attempts(url)
        if last is None:
            return
        if _is_closed_error(last):
            raise RuntimeError(
                "A janela do Chrome (o robô que trabalha no Jettax) foi fechada e não foi "
                "possível reabrí-la. Não feche essa janela durante a execução; feche também "
                "outras janelas do Chrome que estejam usando o perfil do programa, aguarde "
                "alguns segundos e rode de novo."
            ) from last
        raise last

    def _goto_attempts(self, url: str) -> Exception | None:
        """Tenta navegar com 3 estratégias; devolve a última exceção ou None."""
        last: Exception | None = None
        for wait_until in ("domcontentloaded", "commit", "load"):
            try:
                self._say(f"Abrindo {url} ({wait_until})…")
                self.page.goto(url, wait_until=wait_until, timeout=90000)
                return None
            except Exception as exc:  # noqa: BLE001
                last = exc
                self._say(f"Falha ao abrir ({wait_until}): {type(exc).__name__}: {exc}")
                if _is_closed_error(exc):
                    # Navegador morto: não há sentido em tentar de novo agora.
                    return last
                time.sleep(1)
        return last

    def _wait_logged_in(self, wait_manual_seconds: int) -> None:
        deadline = time.time() + wait_manual_seconds
        markers = ("Razão Social", "RAZAO SOCIAL", "Novo Cliente", "Sem certificado")
        while time.time() < deadline:
            try:
                url = (self.page.url or "").lower()
                if any(x in url for x in ("login", "signin", "auth", "sso")):
                    self.page.wait_for_timeout(1000)
                    continue
                for marker in markers:
                    loc = self.page.get_by_text(marker, exact=False)
                    if loc.count() > 0:
                        self._say(f"Login detectado ({marker}).")
                        return
            except Exception as exc:  # noqa: BLE001
                self._say(f"Ainda carregando: {exc}")
            try:
                self.page.wait_for_timeout(1000)
            except Exception as exc:  # noqa: BLE001
                if _is_closed_error(exc):
                    raise RuntimeError(
                        "A janela do Chrome foi fechada durante a espera de login. "
                        "Não feche a janela aberta pelo programa; rode de novo."
                    ) from exc
                raise
        raise TimeoutError(
            "Não vi a lista de clientes. Faça login no Chrome e tente de novo."
        )

    def _try_fill_login(self, email: str, senha: str) -> None:
        page = self.page
        for sel in [
            'input[type="email"]',
            'input[name="email"]',
            'input[name="username"]',
            'input[placeholder*="mail" i]',
            'input[placeholder*="E-mail" i]',
        ]:
            loc = page.locator(sel)
            if loc.count():
                loc.first.fill(email)
                break
        for sel in ['input[type="password"]', 'input[name="password"]']:
            loc = page.locator(sel)
            if loc.count():
                loc.first.fill(senha)
                break
        for sel in [
            'button[type="submit"]',
            'button:has-text("Entrar")',
            'button:has-text("Login")',
            'button:has-text("Acessar")',
        ]:
            loc = page.locator(sel)
            if loc.count():
                loc.first.click()
                break

    def list_without_certificate(self) -> list[JetaxClient]:
        assert self.page
        self._captured_clients.clear()
        self._say("Abrindo lista de clientes sem certificado…")
        # URL vista no escritório; o filtro da UI é a fonte da verdade
        url = (
            f"{self.base}/client/list?page=1&name=&document=&city=&status="
            f"&municipalRegistration=&excel=&validCertificate=false"
            f"&simpleOptionInfo=&certificateStatus=&certificateType="
        )
        self._goto_safe(url)

        self._apply_sem_certificado_filter()
        if not self._filter_is_verified():
            raise RuntimeError("Não consegui confirmar o filtro SEM CERTIFICADO no Jettax. Nenhum envio será feito.")
        clients = self._scrape_all_pages()
        if not clients and self._captured_clients:
            clients = [_row_to_client(r) for r in self._captured_clients]
            clients = [c for c in clients if c.cnpj]
        # Remove somente linhas exatamente repetidas pela paginação. CNPJs iguais
        # com nome/id diferente são preservados para o matcher bloquear a ambiguidade.
        seen_rows: set[tuple[str, str, str]] = set()
        unique: list[JetaxClient] = []
        for client in clients:
            key = (client.cnpj, (client.razao_social or "").strip().casefold(), str(client.client_id or ""))
            if key in seen_rows:
                continue
            seen_rows.add(key)
            client.tem_certificado = False
            unique.append(client)
        self._say(f"Clientes sem certificado: {len(unique)}")
        return unique

    def list_with_certificate(self) -> list[JetaxClient]:
        """Lista clientes que JÁ possuem A1 (para o modo 'atualizar todos')."""
        return self._list_by_certificate_filter(valid_certificate=True)

    def list_all_clients(self) -> tuple[list[JetaxClient], list[JetaxClient]]:
        """Devolve (sem_certificado, com_certificado) no modo renovação total.

        Usado pelo fluxo 'Atualizar/renovar certificados de TODAS as empresas':
        precisamos das duas listas para o matcher conciliar PFX novos com
        clientes que já tenham A1 (renovação) e também com os que não tenham.
        """
        sem = self.list_without_certificate()
        com = self.list_with_certificate()
        # Defesa extra além da checagem de URL em _list_by_certificate_filter:
        # se as duas listas vierem com o mesmo conjunto de CNPJs, o filtro
        # quase certamente não pegou (ex.: a tela recarregou na lista errada
        # por um instante). Um cliente não pode estar simultaneamente "sem"
        # e "com" certificado válido, então esse cenário nunca é esperado.
        if sem and com:
            cnpjs_sem = {c.cnpj for c in sem if c.cnpj}
            cnpjs_com = {c.cnpj for c in com if c.cnpj}
            overlap = cnpjs_sem & cnpjs_com
            if overlap and len(overlap) >= min(len(cnpjs_sem), len(cnpjs_com)) * 0.9:
                raise RuntimeError(
                    "As listas 'sem certificado' e 'com certificado' do Jettax vieram quase "
                    "idênticas — isso indica que o filtro não foi aplicado de verdade em uma "
                    "das duas telas. Nenhum envio será feito. Rode de novo e confira manualmente "
                    "no navegador se as duas telas mostram empresas diferentes."
                )
        return sem, com

    def _list_by_certificate_filter(self, *, valid_certificate: bool) -> list[JetaxClient]:
        assert self.page
        self._captured_clients.clear()
        flag = "true" if valid_certificate else "false"
        self._say(f"Abrindo lista de clientes com certificado={'válido' if valid_certificate else 'inválido/ausente'}…")
        url = (
            f"{self.base}/client/list?page=1&name=&document=&city=&status="
            f"&municipalRegistration=&excel=&validCertificate={flag}"
            f"&simpleOptionInfo=&certificateStatus=&certificateType="
        )
        self._goto_safe(url)
        if not valid_certificate:
            self._apply_sem_certificado_filter()
            if not self._filter_is_verified():
                raise RuntimeError(
                    "Não consegui confirmar o filtro SEM CERTIFICADO no Jettax. Nenhum envio será feito."
                )
        else:
            # Antes, esta checagem só confirmava que "alguma" lista de
            # clientes renderizou — sem garantir que era de fato a lista
            # filtrada por "com certificado". Isso podia devolver
            # silenciosamente a MESMA lista de "sem certificado" (ex.: se a
            # navegação para validCertificate=true falhar ou a página cair
            # de volta no estado anterior). Agora exigimos a mesma prova por
            # URL usada no filtro "sem certificado".
            self._wait_client_list_ready(timeout_seconds=30)
            if not self._filter_is_verified(expected=True):
                raise RuntimeError(
                    "Não consegui confirmar o filtro COM CERTIFICADO no Jettax "
                    "(a página pode ter voltado para a lista errada). Nenhum envio será feito."
                )
        clients = self._scrape_all_pages()
        if not clients and self._captured_clients:
            clients = [_row_to_client(r) for r in self._captured_clients]
            clients = [c for c in clients if c.cnpj]
        seen_rows: set[tuple[str, str, str]] = set()
        unique: list[JetaxClient] = []
        for client in clients:
            key = (client.cnpj, (client.razao_social or "").strip().casefold(), str(client.client_id or ""))
            if key in seen_rows:
                continue
            seen_rows.add(key)
            client.tem_certificado = valid_certificate
            unique.append(client)
        self._say(f"Clientes com certificado={'válido' if valid_certificate else 'ausente'}: {len(unique)}")
        return unique

    def _apply_sem_certificado_filter(self) -> None:
        page = self.page
        # 1) A URL oficial do filtro já é nossa primeira tentativa.
        if "validCertificate=false" in (page.url or ""):
            return
        # 2) Se a UI mudou, tentamos localizar um filtro/chip textual.
        for loc in (
            page.get_by_text("Filtros", exact=False),
            page.locator("button:has-text('Filtros')"),
            page.get_by_text("Certificado", exact=True),
        ):
            try:
                if loc.count():
                    loc.first.click(timeout=2500)
                    page.wait_for_timeout(500)
                    break
            except Exception:
                pass
        for label in ("Sem certificado", "Sem Certificado", "Não possui certificado", "Nao possui certificado"):
            loc = page.get_by_text(label, exact=False)
            try:
                if loc.count():
                    loc.first.click(timeout=2500)
                    page.wait_for_timeout(900)
                    return
            except Exception:
                pass

    def _filter_is_verified(self, expected: bool = False) -> bool:
        # Para segurança, não aceitamos apenas a presença do texto "Sem certificado",
        # pois ele pode aparecer no menu mesmo quando a tabela não está filtrada.
        url = (self.page.url or "").lower()
        flag = "true" if expected else "false"
        return f"validcertificate={flag}" in url

    def _scrape_all_pages(self) -> list[JetaxClient]:
        page = self.page
        all_clients: list[JetaxClient] = []
        seen_page_keys: set[str] = set()
        for _ in range(200):
            page.wait_for_timeout(500)
            page_key = self._page_fingerprint()
            if page_key in seen_page_keys:
                break
            seen_page_keys.add(page_key)
            rows = self._scrape_current_table()
            if not rows:
                break
            all_clients.extend(rows)
            if not self._goto_next_page():
                break
        return all_clients

    def _page_fingerprint(self) -> str:
        try:
            text = self.page.locator("body").inner_text(timeout=2000)
            docs = re.findall(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}", text)
            return (self.page.url or "") + "|" + "|".join(docs[:50])
        except Exception:
            return self.page.url or ""

    def _scrape_current_table(self) -> list[JetaxClient]:
        page = self.page
        clients: list[JetaxClient] = []
        # tenta linhas de tabela / grid
        row_loc = page.locator("table tbody tr, [class*='table'] [class*='row'], [role='row']")
        count = row_loc.count()
        if count == 0:
            # fallback: texto da página inteira + regex CNPJ
            return self._scrape_by_regex()
        for i in range(count):
            row = row_loc.nth(i)
            try:
                text = row.inner_text(timeout=1000)
            except Exception:
                continue
            doc = _first_doc(text)
            if not doc:
                continue
            razao = _guess_razao(text, doc)
            cidade, trib, status = _guess_meta(text)
            href = ""
            try:
                link = row.locator("a").first
                if link.count():
                    href = link.get_attribute("href") or ""
            except Exception:
                href = ""
            clients.append(
                JetaxClient(
                    razao_social=razao,
                    cnpj=doc,
                    cidade=cidade,
                    tributacao=trib,
                    status=status,
                    client_id=href or None,
                )
            )
        return clients

    def _scrape_by_regex(self) -> list[JetaxClient]:
        text = self.page.inner_text("body")
        clients: list[JetaxClient] = []
        # CNPJ formatado
        for m in re.finditer(r"(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})", text):
            doc = pad_cnpj(m.group(1))
            if not is_valid_doc(doc):
                continue
            start = max(0, m.start() - 180)
            chunk = text[start : m.end() + 80]
            razao = _guess_razao(chunk, doc)
            clients.append(JetaxClient(razao_social=razao, cnpj=doc))
        return clients

    def _goto_next_page(self) -> bool:
        page = self.page
        before = self._page_fingerprint()
        selectors = [
            'button[aria-label="Next"]',
            'button[aria-label="Próxima"]',
            'button:has-text("›")',
            'a:has-text("Próximo")',
            'button:has-text("Próximo")',
            'li.page-item.next a',
            '[aria-label="Go to next page"]',
        ]
        for sel in selectors:
            loc = page.locator(sel)
            try:
                if not loc.count():
                    continue
                btn = loc.last
                if not btn.is_enabled():
                    continue
                # disabled/aria-disabled previne loop infinito.
                if (btn.get_attribute("aria-disabled") or "").lower() == "true":
                    continue
                btn.click(timeout=2500)
                page.wait_for_timeout(900)
                after = self._page_fingerprint()
                if after and after != before:
                    return True
            except Exception:
                continue
        return False

    def upload_individual(self, item: MatchResult, dry_run: bool = True) -> str:
        assert self.page
        if not item.cliente or not item.cert:
            return "ignorado"
        if dry_run:
            self._say(
                f"[SIMULAÇÃO] {item.cliente.razao_social} ← {item.cert.filename}"
            )
            return "simulado"
        cliente = item.cliente
        cert = item.cert
        self._say(f"Enviando A1: {cliente.razao_social} ({format_cnpj(cliente.cnpj)}) — senha protegida")
        self._open_client(cliente)
        self._verify_open_client(cliente)
        self._goto_credenciais()
        self._attach_pfx(cert.temp_path, cert.password or "")
        self._save_client()
        self._say(f"OK {cliente.razao_social}")
        return "enviado"

    def _open_client(self, cliente: JetaxClient) -> None:
        page = self.page
        formatted = format_cnpj(cliente.cnpj)
        digits = only_digits(cliente.cnpj)

        if cliente.client_id:
            cid = str(cliente.client_id)
            if cid.startswith("http"):
                self._goto_safe(cid)
                page.wait_for_timeout(600)
                if self._parece_cadastro():
                    return
            elif cid.startswith("/"):
                self._goto_safe(self.base + cid)
                page.wait_for_timeout(600)
                if self._parece_cadastro():
                    return
            elif cid.isdigit():
                for path in (f"/client/{cid}", f"/client/edit/{cid}", f"/client/form/{cid}"):
                    try:
                        self._goto_safe(self.base + path)
                        page.wait_for_timeout(500)
                        if self._parece_cadastro():
                            return
                    except Exception:
                        continue

        for doc in (formatted, digits):
            self._goto_safe(f"{self.base}/client/list?document={doc}")
            page.wait_for_timeout(900)
            if self._abrir_linha(cliente):
                return

        self._goto_safe(f"{self.base}/client/list")
        page.wait_for_timeout(600)
        self._filtrar_documento(digits, formatted, cliente.razao_social)
        page.wait_for_timeout(900)
        if self._abrir_linha(cliente):
            return
        raise RuntimeError(f"Não abriu o cadastro de {cliente.razao_social} ({formatted})")

    def _verify_open_client(self, cliente: JetaxClient) -> None:
        """Confirma o CNPJ no cadastro antes de anexar qualquer arquivo."""
        expected = only_digits(cliente.cnpj)
        formatted = format_cnpj(expected)
        observed: set[str] = set()
        try:
            body = self.page.locator("body").inner_text(timeout=3000)
            for match in re.finditer(r"(?<!\d)\d{14}(?!\d)|\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}", body):
                observed.add(only_digits(match.group(0)))
        except Exception:
            pass
        for selector in ('input[name="document"]', 'input[placeholder*="CNPJ" i]'):
            try:
                locator = self.page.locator(selector)
                for index in range(locator.count()):
                    observed.add(only_digits(locator.nth(index).input_value()))
            except Exception:
                continue
        if expected not in observed:
            raise RuntimeError(f"Cadastro aberto não confirmou o CNPJ esperado {formatted}; upload bloqueado")
        if any(value and len(value) == 14 and value != expected for value in observed):
            raise RuntimeError("Cadastro aberto contém outro CNPJ; upload bloqueado por ambiguidade")

    def _parece_cadastro(self) -> bool:
        page = self.page
        try:
            if page.get_by_text("Credenciais", exact=False).count():
                return True
            if page.get_by_text("Dados Cadastrais", exact=False).count():
                return True
        except Exception:
            return False
        return False

    def _filtrar_documento(self, digits: str, formatted: str, nome: str) -> None:
        page = self.page
        for sel in [
            'input[name="document"]',
            'input[placeholder*="CNPJ" i]',
            'input[placeholder*="documento" i]',
            'input[placeholder*="CPF" i]',
        ]:
            loc = page.locator(sel)
            if loc.count():
                try:
                    loc.first.fill(formatted)
                    loc.first.press("Enter")
                    return
                except Exception:
                    continue
        for sel in [
            'input[name="name"]',
            'input[placeholder*="Razão" i]',
            'input[placeholder*="nome" i]',
            'input[placeholder*="Buscar" i]',
        ]:
            loc = page.locator(sel)
            if loc.count() and nome:
                try:
                    loc.first.fill(nome[:40])
                    loc.first.press("Enter")
                    return
                except Exception:
                    continue

    def _abrir_linha(self, cliente: JetaxClient) -> bool:
        page = self.page
        formatted = format_cnpj(cliente.cnpj)
        digits = only_digits(cliente.cnpj)
        row = None
        for needle in (formatted, digits):
            if not needle:
                continue
            for base_sel in ("tr", "[class*='row']"):
                loc = page.locator(base_sel).filter(has_text=needle)
                try:
                    count = loc.count()
                    if count == 1:
                        row = loc.first
                        break
                    if count > 1:
                        raise RuntimeError("Mais de uma linha contém o CNPJ procurado; abertura bloqueada")
                except RuntimeError:
                    raise
                except Exception:
                    continue
            if row:
                break
        if row is None:
            return False
        try:
            acoes = row.get_by_text("Ações", exact=False)
            if acoes.count():
                acoes.first.click()
                page.wait_for_timeout(400)
                for label in ("Editar", "Alterar", "Editar cliente"):
                    item = page.get_by_text(label, exact=False)
                    if item.count():
                        item.first.click()
                        page.wait_for_timeout(800)
                        return True
        except Exception as exc:
            self._say(f"Ações da linha falhou: {exc}")
        try:
            row.dblclick()
            page.wait_for_timeout(800)
            return self._parece_cadastro() or True
        except Exception:
            return False

    def _goto_credenciais(self) -> None:
        page = self.page
        tab = page.get_by_role("tab", name=re.compile("Credenciais", re.IGNORECASE))
        if tab.count():
            tab.first.click()
        else:
            page.get_by_text("Credenciais", exact=False).first.click()
        page.wait_for_timeout(500)

    def _attach_pfx(self, pfx_path: str, password: str) -> None:
        page = self.page
        # Nunca escolhe "primeiro/último" quando há vários campos: isso poderia
        # preencher credencial de login ou anexar arquivo em outra seção.
        file_box = None
        file_inputs = page.locator('input[type="file"]:visible')
        count = file_inputs.count()
        if count == 1:
            file_target = file_inputs.first
            file_box = file_target.bounding_box()
            file_target.set_input_files(pfx_path)
        elif count == 0:
            browse = page.get_by_text(re.compile(r"Browse|Escolher arquivo|Selecionar arquivo", re.IGNORECASE))
            if browse.count() != 1:
                raise RuntimeError("Campo de certificado não encontrado de forma inequívoca")
            file_box = browse.first.bounding_box()
            with page.expect_file_chooser() as chooser:
                browse.first.click()
            chooser.value.set_files(pfx_path)
        else:
            raise RuntimeError("Mais de um campo de arquivo visível; upload bloqueado por ambiguidade")

        candidates = page.locator(
            'input[type="password"]:visible, input[name*="pass" i]:visible, input[placeholder*="senha" i]:visible'
        )
        total = candidates.count()
        if total == 0:
            raise RuntimeError("Campo de senha do certificado não foi identificado de forma inequívoca")
        if total == 1:
            candidates.first.fill(password)
            return
        # A tela de Credenciais do Jettax tem VÁRIOS campos de senha na mesma
        # página (certificado A1, prefeitura, portal nacional da NFSe). O campo
        # certo é o que fica na MESMA LINHA visual do campo de arquivo do
        # certificado A1 (arquivo à esquerda, senha à direita); os outros ficam
        # em seções bem mais abaixo. Nunca assume "o primeiro" ou "o último".
        if not file_box:
            raise RuntimeError(
                f"Há {total} campos de senha nesta tela e não foi possível localizar a posição "
                "do campo de arquivo para saber qual senha pertence ao certificado A1; upload bloqueado."
            )
        best = None
        best_dist = None
        for index in range(total):
            candidate = candidates.nth(index)
            box = candidate.bounding_box()
            if not box:
                continue
            dist = abs(box["y"] - file_box["y"])
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best = candidate
        row_tolerance_px = 60
        if best is None or best_dist is None or best_dist > row_tolerance_px:
            raise RuntimeError(
                f"Há {total} campos de senha nesta tela e nenhum está claramente na mesma linha "
                "do campo de arquivo do certificado A1; upload bloqueado por segurança (evita gravar "
                "a senha do A1 num campo de outra credencial, como prefeitura ou portal nacional)."
            )
        best.fill(password)

    def _save_client(self) -> None:
        page = self.page
        button = page.get_by_role("button", name=re.compile(r"^(Salvar|Save|Confirmar|Atualizar)$", re.IGNORECASE))
        if button.count() != 1:
            raise RuntimeError("Botão salvar não identificado de forma inequívoca")
        button.first.click()
        page.wait_for_timeout(1500)
        success = page.get_by_text(re.compile(r"salvo com sucesso|atualizado com sucesso|certificado.*sucesso", re.IGNORECASE))
        if success.count() == 0:
            raise RuntimeError("O Jettax não exibiu confirmação de sucesso; envio não será registrado como concluído")

    def upload_lote_planilha(
        self, zip_path: Path, planilha_path: Path, dry_run: bool = True, wait_fn=None
    ) -> str:
        """Abre a tela de importação do Jettax e aguarda confirmação manual.

        Depois de muita tentativa de adivinhar os textos de uma tela de
        importação em lote automática (que variam e são frágeis), o caminho
        mais simples e confiável é: o programa gera os dois arquivos exigidos
        pelo Jettax (ZIP de certificados + planilha oficial preenchida),
        abre a tela certa pra você, e você mesmo seleciona os dois arquivos —
        só 2 cliques, sem risco de a automação escolher o campo errado.
        """
        zip_path = Path(zip_path)
        planilha_path = Path(planilha_path)
        if dry_run:
            self._say("[SIMULAÇÃO] nenhum lote com senha foi criado ou enviado.")
            return "simulado"
        if not zip_path.is_file():
            raise FileNotFoundError(zip_path)
        if not planilha_path.is_file():
            raise FileNotFoundError(planilha_path)
        # Sempre avisa os caminhos ANTES de qualquer clique: se o clique
        # automático no botão "Importar" falhar por qualquer motivo, o
        # usuário já sabe onde estão os dois arquivos para abrir manualmente.
        self._say(f"ZIP de certificados pronto em: {zip_path}")
        self._say(f"Planilha de importação pronta em: {planilha_path}")
        self._goto_safe(f"{self.base}/client/list")
        pronto = self._wait_client_list_ready()
        if not pronto:
            self._say("A tela de clientes do Jettax demorou para sair de 'Processando...'.")
        abriu = self._abrir_tela_importar()
        if abriu:
            self._say("Tela 'Importar' aberta no Jettax.")
        else:
            screenshot_path = self._save_diagnostic_screenshot("botao_importar_nao_encontrado")
            aviso = "Não consegui clicar automaticamente no botão 'Importar' — abra você mesmo em Clientes."
            if screenshot_path:
                aviso += f" Print da tela salvo em: {screenshot_path}"
            self._say(aviso)
        if wait_fn is None:
            raise RuntimeError(
                "Não há confirmação manual disponível; envio não pode ser marcado como concluído"
            )
        self._say(
            "AÇÃO NECESSÁRIA: na tela 'Importar' do Jettax, selecione o ZIP de certificados e a "
            "planilha de senhas (os dois caminhos acima). Depois que o Jettax confirmar o "
            "recebimento, volte aqui e clique SIM. Os arquivos com senha serão apagados assim "
            "que você confirmar (ou cancelar)."
        )
        wait_fn()
        return "confirmado_manualmente"

    def _wait_client_list_ready(self, timeout_seconds: int = 30) -> bool:
        """Espera o app do Jettax sair da tela de splash ('Processando...'
        com o logo do pi) e realmente renderizar a lista de clientes.

        O Jettax é um app que carrega os dados depois da navegação — a URL
        pode responder 'domcontentloaded' enquanto a tela ainda mostra só o
        splash. Sem essa espera, qualquer busca por botão feita logo em
        seguida sempre dá "não encontrado", mesmo que o botão exista e vá
        aparecer meio segundo depois.
        """
        page = self.page
        deadline = time.time() + timeout_seconds
        markers = ("Razão Social", "RAZAO SOCIAL", "Novo Cliente", "Importar", "Sem certificado")
        while time.time() < deadline:
            try:
                ainda_processando = page.get_by_text("Processando", exact=False).count() > 0
                if not ainda_processando:
                    for marker in markers:
                        if page.get_by_text(marker, exact=False).count() > 0:
                            return True
            except Exception:
                pass
            try:
                page.wait_for_timeout(400)
            except Exception:
                return False
        return False

    def _abrir_tela_importar(self) -> bool:
        page = self.page
        # wait_for(state="visible") de fato espera o elemento aparecer,
        # diferente de .count(), que só olha o instante atual do DOM.
        botao = page.get_by_role("button", name=re.compile(r"^Importar$", re.IGNORECASE))
        try:
            botao.first.wait_for(state="visible", timeout=15000)
            botao.first.click(timeout=2500)
            page.wait_for_timeout(600)
            return True
        except Exception:
            pass
        texto = page.get_by_text("Importar", exact=True)
        try:
            texto.first.wait_for(state="visible", timeout=5000)
            texto.first.click(timeout=2500)
            page.wait_for_timeout(600)
            return True
        except Exception:
            pass
        return False

    def _save_diagnostic_screenshot(self, label: str) -> Path | None:
        """Salva um print da tela atual para diagnóstico quando a automação não
        acha o que procura. Nunca levanta exceção — é só um extra informativo."""
        try:
            output_dir = get_output_dir(self.cfg)
            output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f"diagnostico_{label}_{int(time.time())}.png"
            self.page.screenshot(path=str(path), full_page=True)
            return path
        except Exception:
            return None

    def screenshot(self, path: Path) -> None:
        if self.page:
            try:
                self.page.screenshot(path=str(path), full_page=True)
            except Exception as exc:  # noqa: BLE001
                self._say(f"Print não pôde ser tirado ({type(exc).__name__}): {exc}")

    def close(self) -> None:
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        self.context = None
        self.browser = None
        self.page = None
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.playwright = None


def _first_doc(text: str) -> str | None:
    m = re.search(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}", text)
    if m and is_valid_doc(m.group(0)):
        return pad_cnpj(m.group(0))
    digits = only_digits(text)
    # avoid grabbing mixed garbage; look for 14 consecutive
    m2 = re.search(r"\d{14}", digits)
    if m2 and is_valid_doc(m2.group(0)):
        return pad_cnpj(m2.group(0))
    return None


def _guess_razao(text: str, doc: str) -> str:
    formatted = format_cnpj(doc)
    # pega o trecho antes do CNPJ
    idx = text.find(formatted)
    chunk = text[:idx] if idx > 0 else text
    lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
    if lines:
        # última linha "substantiva"
        for ln in reversed(lines):
            if len(ln) >= 5 and not ln.lower().startswith("ações"):
                return re.sub(r"\s+", " ", ln)[:180]
    return formatted


def _guess_meta(text: str) -> tuple[str, str, str]:
    cidade = ""
    trib = ""
    status = ""
    if re.search(r"Simples Nacional", text, re.IGNORECASE):
        trib = "Simples Nacional"
    elif re.search(r"Lucro Presumido", text, re.IGNORECASE):
        trib = "Lucro Presumido"
    elif re.search(r"Lucro Real", text, re.IGNORECASE):
        trib = "Lucro Real"
    if re.search(r"\bAtivo\b", text):
        status = "Ativo"
    elif re.search(r"\bInativo\b", text):
        status = "Inativo"
    return cidade, trib, status


def _json_to_rows(data) -> list[dict]:
    if isinstance(data, list):
        seq = data
    elif isinstance(data, dict):
        seq = None
        for k in ("data", "items", "results", "content", "clients", "rows"):
            if isinstance(data.get(k), list):
                seq = data[k]
                break
        if seq is None:
            return []
    else:
        return []
    rows = []
    for item in seq:
        if not isinstance(item, dict):
            continue
        blob = json.dumps(item, ensure_ascii=False)
        doc = _first_doc(blob)
        if not doc:
            for k, v in item.items():
                if re.search(r"cnpj|document", str(k), re.IGNORECASE):
                    if is_valid_doc(str(v)):
                        doc = pad_cnpj(str(v))
        if not doc:
            continue
        name = ""
        for k, v in item.items():
            if re.search(r"razao|name|nome|company", str(k), re.IGNORECASE) and isinstance(v, str):
                name = v
                break
        rows.append({"cnpj": doc, "name": name, "raw": item})
    return rows


def _row_to_client(row: dict) -> JetaxClient:
    return JetaxClient(
        razao_social=row.get("name") or format_cnpj(row["cnpj"]),
        cnpj=row["cnpj"],
        extra=row.get("raw") or {},
    )

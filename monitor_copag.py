#!/usr/bin/env python3
"""
Monitor de produtos - Copag B2B (versão para GitHub Actions)

Cada execução: faz login, verifica as páginas, avisa no Telegram se algo mudou
e grava o resultado em estado.json (que o GitHub salva para a próxima execução).

Credenciais vêm dos "Secrets" do GitHub (NUNCA escreva a senha neste arquivo):
  COPAG_EMAIL, COPAG_SENHA, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""
import json
import os
import random
import time
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = "https://www.b2b.copagloja.com.br"

# Para adicionar ou remover produtos, edite esta lista:
URLS = [
    f"{BASE}/treinador-avancado-pokemon-30-anos/p",
    f"{BASE}/box-ex-sylveon/p",
    f"{BASE}/box-greninja/p",
    f"{BASE}/blister-triplo-lucario-/p",
    f"{BASE}/blister-triplo-exeggutor/p",
    f"{BASE}/blister-duplo-com-moeda/p",
    f"{BASE}/box-colecao-com-poster-/p",
]

EMAIL = os.environ.get("COPAG_EMAIL", "").strip()
SENHA = os.environ.get("COPAG_SENHA", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

ARQ_ESTADO = Path(__file__).resolve().parent / "estado.json"

SINAIS_404 = ["página não encontrada", "pagina nao encontrada", "não encontramos a página",
              "page not found", "ops! não encontramos"]
SINAIS_SEM_ESTOQUE = ["indisponível", "indisponivel", "esgotado", "avise-me", "fora de estoque"]

AUSENTE, SEM_ESTOQUE, DISPONIVEL, PAGINA_VOLTOU, ERRO = (
    "AUSENTE", "SEM_ESTOQUE", "DISPONIVEL", "PAGINA_VOLTOU", "ERRO")

MENSAGENS = {
    DISPONIVEL: "🟢 EM ESTOQUE!",
    PAGINA_VOLTOU: "🟢 A página voltou!",
    SEM_ESTOQUE: "🟡 Página existe, mas sem estoque",
    AUSENTE: "⚪ Página saiu do ar de novo",
}


def log(msg):
    print(msg, flush=True)


def telegram(msg):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        log("Telegram não configurado: " + msg)
        return
    dados = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID, "text": msg, "disable_web_page_preview": "true"
    }).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data=dados, timeout=20)
    except Exception as e:
        log(f"Falha ao enviar Telegram: {e}")


def carregar_estado():
    try:
        return json.loads(ARQ_ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def salvar_estado(estado):
    ARQ_ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=2, sort_keys=True),
                          encoding="utf-8")


def slug_de(url):
    return url.rstrip("/").removesuffix("/p").rsplit("/", 1)[-1]


# ------------------------------------------------------------------ login
def esta_logado(page):
    try:
        r = page.request.get(f"{BASE}/api/sessions?items=profile.isAuthenticated", timeout=30000)
        v = (r.json().get("namespaces", {}).get("profile", {})
             .get("isAuthenticated", {}).get("value"))
        return None if v is None else str(v).lower() == "true"
    except Exception:
        return None


def fazer_login(page):
    page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)

    # Algumas lojas mostram primeiro um botão "Entrar com e-mail e senha"
    for texto in ["Entrar com e-mail e senha", "e-mail e senha", "Email e senha"]:
        botao = page.get_by_text(texto, exact=False)
        if botao.count() > 0:
            try:
                botao.first.click(timeout=5000)
                page.wait_for_timeout(1500)
                break
            except Exception:
                pass

    campo_email = page.locator(
        'input[type="email"], input[name="email"], input[placeholder*="mail" i]').first
    campo_email.fill(EMAIL, timeout=20000)

    campo_senha = page.locator('input[type="password"]').first
    try:
        campo_senha.wait_for(state="visible", timeout=3000)
    except PWTimeout:
        # Fluxo em duas etapas: e-mail -> continuar -> senha
        campo_email.press("Enter")
        campo_senha.wait_for(state="visible", timeout=15000)

    campo_senha.fill(SENHA)
    campo_senha.press("Enter")

    for _ in range(20):
        page.wait_for_timeout(1500)
        if esta_logado(page):
            return True
    if "recaptcha" in page.content().lower():
        log("A página de login parece ter captcha.")
    return False


# ------------------------------------------------------------------ produtos
def consultar_api(page, url):
    api = f"{BASE}/api/catalog_system/pub/products/search/{slug_de(url)}/p"
    try:
        r = page.request.get(api, timeout=30000)
        if not r.ok:
            return None
        dados = r.json()
        if not dados:
            return {"existe": False}
        prod = dados[0]
        ofertas = [s.get("commertialOffer", {})
                   for it in prod.get("items", []) for s in it.get("sellers", [])]
        qtd = sum(o.get("AvailableQuantity", 0) or 0 for o in ofertas)
        return {"existe": True, "nome": prod.get("productName", ""), "qtd": qtd,
                "disponivel": qtd > 0 or any(o.get("IsAvailable") for o in ofertas)}
    except Exception:
        return None


def checar_produto(page, url):
    api = consultar_api(page, url)

    status, texto, url_final = None, "", url
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
        status = resp.status if resp else None
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
        url_final = page.url
        texto = page.inner_text("body").lower()
    except Exception as e:
        if api is None:
            return ERRO, f"erro ao abrir página: {type(e).__name__}"

    if "/login" in url_final:
        return ERRO, "redirecionou para o login"

    if api and api.get("existe"):
        if api["disponivel"]:
            return DISPONIVEL, f"{api['nome']} — quantidade disponível: {api['qtd']}"
        return SEM_ESTOQUE, f"{api['nome']} — sem estoque"

    if status in (404, 410) or any(s in texto for s in SINAIS_404):
        return AUSENTE, f"página não existe (HTTP {status})"
    if any(s in texto for s in SINAIS_SEM_ESTOQUE):
        return SEM_ESTOQUE, "página voltou, mas aparece como indisponível"
    return PAGINA_VOLTOU, f"página voltou a aparecer (HTTP {status}) — confira o estoque"


# ------------------------------------------------------------------ principal
def main():
    estado = carregar_estado()
    primeira_execucao = not any(k.startswith("http") for k in estado)

    if not (EMAIL and SENHA):
        log("ERRO: configure os Secrets COPAG_EMAIL e COPAG_SENHA no GitHub.")
        return

    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=True)
        ctx = navegador.new_context(locale="pt-BR", viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        try:
            logado = fazer_login(page)
        except Exception as e:
            log(f"Erro durante o login: {type(e).__name__}")
            logado = False

        if not logado:
            log("Não consegui fazer login. Pulando esta verificação.")
            if not estado.get("_aviso_login"):
                telegram("⚠️ Monitor Copag: não consegui fazer login no site. "
                         "Verifique se a senha nos Secrets do GitHub está certa.")
                estado["_aviso_login"] = True
            salvar_estado(estado)
            navegador.close()
            return
        estado["_aviso_login"] = False
        log("Login OK.")

        resumo = []
        for url in URLS:
            novo, detalhe = checar_produto(page, url)
            anterior = estado.get(url)
            log(f"{slug_de(url):40s} {novo:14s} {detalhe}")
            resumo.append(f"• {slug_de(url)}: {novo}")

            if novo != ERRO:
                if novo != anterior and not (anterior is None and novo == AUSENTE):
                    telegram(f"{MENSAGENS[novo]}\n{detalhe}\n{url}")
                estado[url] = novo
            time.sleep(random.uniform(2, 4))

        navegador.close()

    if primeira_execucao:
        telegram("✅ Monitor Copag ativado! Situação atual:\n" + "\n".join(resumo))

    salvar_estado(estado)


if __name__ == "__main__":
    main()

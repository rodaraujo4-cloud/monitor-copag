#!/usr/bin/env python3
"""
Monitor de produtos - Copag B2B (versão para GitHub Actions)

Cada execução: faz login, verifica as páginas, avisa no Telegram se algo mudou
e grava o resultado em estado.json (que o GitHub salva para a próxima execução).

Credenciais vêm dos "Secrets" do GitHub (NUNCA escreva a senha neste arquivo):
  COPAG_EMAIL, COPAG_SENHA, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""
import html
import json
import os
import random
import re
import time
import unicodedata
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

# Quantas verificações cada execução do GitHub faz, e quantos minutos entre elas
VERIFICACOES = max(1, int(os.environ.get("VERIFICACOES", "2")))
ESPERA_MIN = float(os.environ.get("ESPERA_MIN", "12"))

MANUAL = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

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


def telegram_foto(png, legenda):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    limite = f"----monitorcopag{random.randint(0, 10**9)}"
    partes = []
    for nome, valor in (("chat_id", TELEGRAM_CHAT_ID), ("caption", legenda[:1000])):
        partes.append(f'--{limite}\r\nContent-Disposition: form-data; name="{nome}"\r\n\r\n'
                      f'{valor}\r\n'.encode())
    partes.append(f'--{limite}\r\nContent-Disposition: form-data; name="photo"; '
                  f'filename="tela.png"\r\nContent-Type: image/png\r\n\r\n'.encode()
                  + png + b"\r\n")
    partes.append(f"--{limite}--\r\n".encode())
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=b"".join(partes),
        headers={"Content-Type": f"multipart/form-data; boundary={limite}"})
    try:
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:
        log(f"Falha ao enviar foto: {e}")


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


def fazer_login(page, info):
    info["etapa"] = "abrindo a página de login"
    page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)

    # Se a página abrir na tela de recuperação (pede CNPJ), volta para a tela normal
    if page.get_by_text("Digite seu CNPJ", exact=False).count() > 0:
        try:
            page.get_by_text("Voltar", exact=True).first.click(timeout=5000)
            page.wait_for_timeout(1500)
        except Exception:
            pass

    info["etapa"] = "procurando o campo de e-mail"
    campo_email = page.locator(
        'input[placeholder*="exemplo@mail" i], input[type="email"], input[name="email"]').first
    campo_email.fill(EMAIL, timeout=20000)

    info["etapa"] = "procurando o campo de senha"
    campo_senha = page.locator('input[type="password"]').first
    campo_senha.fill(SENHA, timeout=15000)

    # Fecha o aviso de cookies, se aparecer
    for texto in ["Rejeitar", "Aceitar"]:
        try:
            page.get_by_role("button", name=texto, exact=False).first.click(timeout=2000)
            break
        except Exception:
            pass

    # O site tem outro "Entrar" no menu do topo; clica no que fica logo abaixo da senha
    info["etapa"] = "procurando o botão Entrar"
    page.mouse.move(10, 400)  # tira o mouse do menu do topo
    caixa_senha = campo_senha.bounding_box()
    candidatos = page.locator("button, a, input[type=submit]").filter(
        has_text=re.compile(r"^\s*Entrar\s*$", re.I))
    melhor = None
    for i in range(candidatos.count()):
        botao = candidatos.nth(i)
        try:
            if not botao.is_visible():
                continue
            caixa = botao.bounding_box()
        except Exception:
            continue
        if not caixa or not caixa_senha or caixa["y"] <= caixa_senha["y"]:
            continue
        distancia = caixa["y"] - caixa_senha["y"]
        if melhor is None or distancia < melhor[0]:
            melhor = (distancia, botao)
    if melhor is None:
        raise RuntimeError("botão Entrar abaixo da senha não encontrado")
    melhor[1].click(timeout=15000)
    info["etapa"] = "cliquei em Entrar, mas o site não confirmou o login"

    for _ in range(20):
        page.wait_for_timeout(1500)
        logado = esta_logado(page)
        if logado:
            return True
        if logado is None and "/login" not in page.url:
            return True  # saiu da tela de login: consideramos que entrou
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


# ------------------------------------------------------------------ catálogo Pokémon
# Além das páginas da lista URLS, o monitor varre TODOS os produtos Pokémon do site:
# avisa quando um produto novo é cadastrado ou quando um antigo volta ao estoque.
PALAVRA_CHAVE = "pokemon"
MAX_AVISOS_SEPARADOS = 8  # acima disso, junta tudo em uma mensagem só


def normalizar(texto):
    sem_acento = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return sem_acento.lower()


def telegram_lista(titulo, linhas):
    """Envia uma lista longa dividida em várias mensagens (limite do Telegram)."""
    bloco = titulo
    for linha in linhas:
        if len(bloco) + len(linha) + 1 > 3800:
            telegram(bloco)
            bloco = titulo + " (continuação)"
        bloco += "\n" + linha
    telegram(bloco)


def resumo_produto(prod):
    ofertas = [s.get("commertialOffer", {})
               for it in prod.get("items", []) for s in it.get("sellers", [])]
    qtd = sum(o.get("AvailableQuantity", 0) or 0 for o in ofertas)
    return {
        "nome": prod.get("productName", "").strip(),
        "slug": prod.get("linkText", ""),
        "qtd": qtd,
        "disponivel": qtd > 0 or any(o.get("IsAvailable") for o in ofertas),
        "skus": [(it.get("itemId"), sl.get("sellerId"))
                 for it in prod.get("items", []) for sl in it.get("sellers", [])
                 if it.get("itemId") and sl.get("sellerId")],
    }


_canal = {}


def canal_de_vendas(page):
    """Canal de vendas (tabela B2B) da sua sessão logada."""
    if "sc" not in _canal:
        try:
            r = page.request.get(f"{BASE}/api/sessions?items=store.channel", timeout=30000)
            _canal["sc"] = (r.json().get("namespaces", {}).get("store", {})
                            .get("channel", {}).get("value"))
        except Exception:
            _canal["sc"] = None
    return _canal["sc"]


MOTIVOS = {
    "withoutStock": "sem estoque",
    "withoutPriceFulfillment": "sem preço definido",
    "withoutPrice": "sem preço definido",
    "cannotBeDelivered": "não entregável",
    "unavailableProduct": "produto indisponível",
}


def confirmar_estoque(page, info, link):
    """Confirma o estoque real antes de avisar.
    Devolve (True/False/None, motivo). None = não deu para saber."""
    # 1) Simula um carrinho, como se fosse comprar 1 unidade
    if info.get("skus"):
        sc = canal_de_vendas(page)
        url = f"{BASE}/api/checkout/pub/orderForms/simulation" + (f"?sc={sc}" if sc else "")
        corpo = {"items": [{"id": sku, "quantity": 1, "seller": vend}
                           for sku, vend in info["skus"][:5]], "country": "BRA"}
        try:
            r = page.request.post(url, data=corpo, timeout=30000,
                                  headers={"Content-Type": "application/json"})
            if r.ok:
                itens = r.json().get("items", [])
                if itens:
                    situacoes = [i.get("availability") for i in itens]
                    log(f"Simulação de carrinho para {info['slug']}: {situacoes}")
                    if "available" in situacoes:
                        return True, ""
                    motivo = ", ".join(sorted({MOTIVOS.get(x, str(x)) for x in situacoes}))
                    return False, motivo
            else:
                log(f"Simulação HTTP {r.status} para {info['slug']}")
        except Exception as e:
            log(f"Simulação falhou para {info['slug']}: {type(e).__name__}")
    # 2) Se a simulação não respondeu, olha a página do produto
    try:
        resp = page.goto(link, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
        if resp and resp.status in (404, 410):
            return False, "página ainda não está no ar"
        texto = page.inner_text("body").lower()
        if any(sinal in texto for sinal in SINAIS_SEM_ESTOQUE):
            return False, "página mostra indisponível"
    except Exception:
        pass
    return None, ""


def categorias_pokemon(page):
    """Descobre as categorias do site cujo nome contém 'Pokémon'."""
    r = page.request.get(f"{BASE}/api/catalog_system/pub/category/tree/3", timeout=30000)
    if not r.ok:
        return []
    achadas = []

    def percorrer(nos, ids):
        for no in nos:
            atual = ids + [str(no.get("id"))]
            if PALAVRA_CHAVE in normalizar(no.get("name")):
                caminho = urllib.parse.urlparse(no.get("url", "")).path.strip("/").split("/")
                facetas = "/".join(f"category-{n}/{parte}"
                                   for n, parte in enumerate(caminho, 1) if parte)
                achadas.append({"ids": "/".join(atual), "facetas": facetas})
            else:
                percorrer(no.get("children", []), atual)

    percorrer(r.json(), [])
    return achadas


def trecho_resposta(r):
    try:
        return r.text()[:200].replace("\n", " ")
    except Exception:
        return ""


def buscar_inteligente(page, facetas="", termo=""):
    """Busca da plataforma VTEX (Intelligent Search)."""
    produtos = {}
    for pagina in range(1, 51):
        params = urllib.parse.urlencode({
            "query": termo, "page": pagina, "count": 50, "locale": "pt-BR",
            "hideUnavailableItems": "false", "fuzzy": "0"})
        url = f"{BASE}/api/io/_v/api/intelligent-search/product_search/{facetas}?{params}"
        r = page.request.get(url, timeout=30000)
        if not r.ok:
            if pagina == 1:
                raise RuntimeError(f"HTTP {r.status} {trecho_resposta(r)}")
            break
        dados = r.json()
        lista = dados.get("products", [])
        for prod in lista:
            produtos[str(prod.get("productId"))] = resumo_produto(prod)
        if not lista or pagina * 50 >= (dados.get("recordsFiltered") or 0):
            break
        time.sleep(0.5)
    return produtos


def buscar_antiga(page, filtro):
    """Busca antiga do catálogo VTEX (algumas lojas desativam)."""
    produtos = {}
    for inicio in range(0, 2500, 50):
        url = (f"{BASE}/api/catalog_system/pub/products/search?{filtro}"
               f"&_from={inicio}&_to={inicio + 49}")
        r = page.request.get(url, timeout=30000)
        if not r.ok:
            if inicio == 0:
                raise RuntimeError(f"HTTP {r.status} {trecho_resposta(r)}")
            break
        dados = r.json()
        for prod in dados:
            produtos[str(prod.get("productId"))] = resumo_produto(prod)
        if len(dados) < 50:
            break
        time.sleep(0.5)
    return produtos


def verificar_catalogo(page, estado):
    try:
        categorias = categorias_pokemon(page)
    except Exception as e:
        categorias = []
        log(f"Não consegui ler as categorias: {type(e).__name__}")
    log(f"Categorias Pokémon encontradas: {categorias}")

    buscas = [("busca por termo", lambda: buscar_inteligente(page, termo=PALAVRA_CHAVE))]
    for c in categorias:
        if c["facetas"]:
            buscas.append((f"categoria {c['facetas']}",
                           lambda c=c: buscar_inteligente(page, facetas=c["facetas"])))
    buscas.append(("busca antiga por termo", lambda: buscar_antiga(page, "ft=" + PALAVRA_CHAVE)))
    for c in categorias:
        buscas.append((f"busca antiga C:/{c['ids']}/",
                       lambda c=c: buscar_antiga(page, f"fq=C:/{c['ids']}/")))

    encontrados = {}
    for nome, funcao in buscas:
        try:
            achados = funcao()
            log(f"{nome}: {len(achados)} produtos")
            encontrados.update(achados)
        except Exception as e:
            log(f"{nome} falhou: {e}")

    if not encontrados:
        log("Catálogo: nenhum produto retornado; mantendo o estado anterior.")
        return

    primeira_vez = "catalogo" not in estado
    catalogo = estado.setdefault("catalogo", {})
    ja_na_lista = {slug_de(u) for u in URLS}  # esses já têm aviso próprio
    extras = estado.setdefault("extras", [])  # páginas achadas pelo mapa do site
    slugs_encontrados = {i["slug"] for i in encontrados.values()}
    eram_extras = set(extras) & slugs_encontrados
    # extras que já apareceram na busca passam a ser acompanhados por aqui
    estado["extras"] = [x for x in extras if x not in slugs_encontrados]

    novos_com_estoque, voltaram, liberados, novos_sem_estoque, pre_lancamento = [], [], [], [], []

    for pid, info in encontrados.items():
        anterior = catalogo.get(pid) or {}
        produto_novo = pid not in catalogo
        link = f"{BASE}/{info['slug']}/p"
        ignorar = primeira_vez or info["slug"] in ja_na_lista
        ja_pre_avisado = bool(anterior.get("pre_avisado"))
        virou_pre = False
        # Registros da versão anterior (sem confirmação de carrinho): reconfere uma vez
        antigo_sem_conferir = (not produto_novo and anterior.get("disponivel")
                               and "pre_avisado" not in anterior)
        estava_disponivel = bool(anterior.get("disponivel")) and not antigo_sem_conferir

        # A busca do site às vezes mostra estoque antes de ele estar liberado.
        # Confirma com uma simulação de carrinho antes de avisar.
        if info["disponivel"] and not ignorar and not estava_disponivel:
            confirmado, motivo = confirmar_estoque(page, info, link)
            info["confirmado"] = confirmado
            if confirmado is False:
                info["disponivel"] = False
                if not ja_pre_avisado:
                    pre_lancamento.append((info, link, motivo))
                    virou_pre = True

        catalogo[pid] = {"nome": info["nome"], "slug": info["slug"],
                         "disponivel": info["disponivel"],
                         "pre_avisado": ja_pre_avisado or virou_pre}
        if ignorar:
            continue
        if antigo_sem_conferir and not virou_pre:
            continue  # já estava disponível antes; só atualiza o registro, sem avisar
        if info["disponivel"] and not estava_disponivel:
            if ja_pre_avisado:
                liberados.append((info, link))
            elif produto_novo:
                novos_com_estoque.append((info, link))
            else:
                voltaram.append((info, link))
        elif produto_novo and not virou_pre and info["slug"] not in eram_extras:
            novos_sem_estoque.append((info, link))

    for info, link, motivo in pre_lancamento:
        telegram("👀 Possível pré-lançamento: estoque apareceu no sistema, "
                 f"mas ainda não está liberado para compra.\n{info['nome']}\n"
                 f"Motivo informado pelo carrinho: {motivo or 'não informado'}\n{link}")

    disponiveis = [i for i in encontrados.values() if i["disponivel"]]
    log(f"Catálogo Pokémon: {len(encontrados)} produtos, {len(disponiveis)} com estoque.")

    if primeira_vez:
        linhas = [f"• {i['nome']} (qtd {i['qtd']})" for i in
                  sorted(disponiveis, key=lambda i: i["nome"])] or ["(nenhum no momento)"]
        telegram_lista(f"📦 Varredura de TODOS os produtos Pokémon ativada!\n"
                       f"{len(encontrados)} produtos encontrados. Em estoque agora:", linhas)
        return

    grupos = [
        ("🚀🟢 LIBERADO PARA COMPRA! (era pré-lançamento)", liberados, True),
        ("🆕🟢 NOVO PRODUTO EM ESTOQUE!", novos_com_estoque, True),
        ("🟢 VOLTOU AO ESTOQUE!", voltaram, True),
        ("🆕 Novo produto Pokémon cadastrado (ainda sem estoque)", novos_sem_estoque, False),
    ]
    for titulo, itens, mostrar_qtd in grupos:
        if not itens:
            continue
        if len(itens) > MAX_AVISOS_SEPARADOS:
            telegram_lista(f"{titulo} ({len(itens)} produtos)",
                           [f"• {i['nome']}\n  {link}" for i, link in itens])
            continue
        for info, link in itens:
            extra = ""
            if mostrar_qtd:
                # 10000 é um número "genérico" da busca, não a quantidade real
                if 0 < info["qtd"] < 10000:
                    extra = f" — quantidade: {info['qtd']}"
                if info.get("confirmado") is None:
                    extra += "\n⚠️ Não consegui confirmar no carrinho; confira no site."
                else:
                    extra += "\n✅ Confirmado na simulação de carrinho."
            telegram(f"{titulo}\n{info['nome']}{extra}\n{link}")


# ------------------------------------------------------------------ mapa do site
# Algumas páginas novas aparecem no mapa do site (sitemap) antes da busca.
MAX_PAGINAS_NOVAS_POR_RODADA = 15


def ler_sitemap(page):
    """Devolve o conjunto de produtos (slugs) listados no mapa do site, ou None."""
    slugs, fila, lidos = set(), [f"{BASE}/sitemap.xml"], 0
    while fila and lidos < 30:
        endereco = fila.pop(0)
        lidos += 1
        try:
            r = page.request.get(endereco, timeout=30000)
            if not r.ok:
                continue
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text())
        except Exception:
            continue
        for loc in locs:
            loc = html.unescape(loc)
            caminho = urllib.parse.urlparse(loc).path.rstrip("/")
            if caminho.endswith("/p"):
                slugs.add(caminho.removesuffix("/p").rsplit("/", 1)[-1])
            elif ".xml" in caminho and "product" in caminho.lower():
                fila.append(loc)
    return slugs or None


def verificar_sitemap(page, estado):
    atuais = ler_sitemap(page)
    if not atuais:
        log("Mapa do site: não disponível ou vazio.")
        return
    primeira_vez = "sitemap" not in estado
    conhecidos = set(estado.get("sitemap", []))
    log(f"Mapa do site: {len(atuais)} produtos.")
    if primeira_vez:
        estado["sitemap"] = sorted(atuais)
        return

    ja_vistos = ({v.get("slug") for v in estado.get("catalogo", {}).values()}
                 | {slug_de(u) for u in URLS} | set(estado.get("extras", [])))
    novos = sorted(atuais - conhecidos - ja_vistos)
    verificados = set()
    for slug in novos[:MAX_PAGINAS_NOVAS_POR_RODADA]:
        verificados.add(slug)
        url = f"{BASE}/{slug}/p"
        situacao, detalhe = checar_produto(page, url)
        try:
            titulo = page.title()
        except Exception:
            titulo = ""
        if PALAVRA_CHAVE in normalizar(slug) or PALAVRA_CHAVE in normalizar(titulo):
            log(f"Nova página Pokémon no mapa do site: {slug} ({situacao})")
            if situacao != ERRO:
                estado[url] = situacao
            estado.setdefault("extras", []).append(slug)
            estado["extras"] = estado["extras"][-50:]
            telegram("🆕👀 Nova página Pokémon detectada (ainda fora da busca do site)\n"
                     f"{titulo or slug}\n{detalhe}\n{url}")
    # os que ficaram para depois serão verificados na próxima rodada
    estado["sitemap"] = sorted((conhecidos | atuais) - (set(novos) - verificados))


# ------------------------------------------------------------------ principal
def main():
    estado = carregar_estado()
    primeira_execucao = not any(k.startswith("http") for k in estado)

    if not (EMAIL and SENHA):
        log("ERRO: configure os Secrets COPAG_EMAIL e COPAG_SENHA no GitHub.")
        return

    with sync_playwright() as p:
        navegador = p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = navegador.new_context(
            locale="pt-BR", timezone_id="America/Sao_Paulo", user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900})
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = ctx.new_page()

        info = {"etapa": "início"}
        try:
            logado = fazer_login(page, info)
        except Exception as e:
            info["erro"] = type(e).__name__
            logado = False

        if not logado:
            log(f"Não consegui fazer login. Etapa: {info['etapa']} {info.get('erro', '')}")
            if MANUAL:
                try:
                    telegram_foto(page.screenshot(),
                                  f"🔍 Diagnóstico do login\nParou em: {info['etapa']}"
                                  f"\nErro: {info.get('erro', 'nenhum')}")
                except Exception as e:
                    log(f"Não consegui tirar a foto: {type(e).__name__}")
            if not estado.get("_aviso_login"):
                telegram("⚠️ Monitor Copag: não consegui fazer login no site. "
                         "Verifique se a senha nos Secrets do GitHub está certa.")
                estado["_aviso_login"] = True
            salvar_estado(estado)
            navegador.close()
            return
        estado["_aviso_login"] = False
        log("Login OK.")

        for rodada in range(1, VERIFICACOES + 1):
            log(f"===== Verificação {rodada} de {VERIFICACOES} =====")
            if rodada > 1 and esta_logado(page) is False:
                log("Sessão caiu; fazendo login de novo.")
                try:
                    fazer_login(page, {"etapa": ""})
                except Exception as e:
                    log(f"Novo login falhou: {type(e).__name__}")
            verificar_tudo(page, estado, primeira_execucao and rodada == 1)
            salvar_estado(estado)  # grava a cada rodada
            if rodada < VERIFICACOES:
                log(f"Aguardando {ESPERA_MIN} min para a próxima verificação...")
                time.sleep(ESPERA_MIN * 60)

        navegador.close()


def verificar_tudo(page, estado, avisar_ativacao):
    resumo = []
    extras = [f"{BASE}/{x}/p" for x in estado.get("extras", [])]
    for url in URLS + extras:
        novo, detalhe = checar_produto(page, url)
        anterior = estado.get(url)
        log(f"{slug_de(url):40s} {novo:14s} {detalhe}")
        if url in URLS:
            resumo.append(f"• {slug_de(url)}: {novo}")

        if novo != ERRO:
            if novo != anterior and not (anterior is None and novo == AUSENTE):
                telegram(f"{MENSAGENS[novo]}\n{detalhe}\n{url}")
            estado[url] = novo
        time.sleep(random.uniform(2, 4))

    try:
        verificar_catalogo(page, estado)
    except Exception as e:
        log(f"Erro na varredura do catálogo: {type(e).__name__}: {e}")

    try:
        verificar_sitemap(page, estado)
    except Exception as e:
        log(f"Erro no mapa do site: {type(e).__name__}: {e}")

    if avisar_ativacao:
        telegram("✅ Monitor Copag ativado! Situação atual:\n" + "\n".join(resumo))


if __name__ == "__main__":
    main()

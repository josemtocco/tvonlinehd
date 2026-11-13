#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TV Online HD -> Lista M3U (otimizada para SS IPTV)
===================================================

Fonte: https://www.tvonlinehd.app.br/

O site é um SPA (Vercel) e NÃO tem paginação / botão "carregar mais": a página
inicial já entrega o catálogo completo, com todos os canais e categorias.
Duas fontes são lidas e cruzadas a cada execução:

  1) HTML da home  -> cards de canal (nome, logo, categorias, id do player)
  2) player.html   -> objeto JS `window.TV` com o catálogo completo:
                      id, nome (n), logo (i), categorias (c), link (s)

A categoria de cada canal é a do próprio site (chips da home).

Regras de atualização (reconciliação):
  - canal que responde -> mantém na lista.
  - canal que falha    -> incrementa contador de falhas; só é REMOVIDO após
                          6 atualizações consecutivas com falha.
  - canal novo         -> adicionado automaticamente.
  - o estado fica em estado_canais.json (commitado no repositório).

Uso:
    python atualizar_m3u.py                 # gera + testa + lista final
    python atualizar_m3u.py --sem-testar    # não valida os streams
    python atualizar_m3u.py --testar-tudo   # testa até os mais lentos
    python atualizar_m3u.py --iniciar-estado # zera os contadores de falha

Só usa a biblioteca padrão do Python (sem pip). Usa ffprobe se estiver
instalado (é o caso do GitHub Actions) para confirmar que o vídeo decodifica.
"""

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from urllib.parse import urljoin, urlparse

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

BASE = os.environ.get("TVHD_BASE", "https://www.tvonlinehd.app.br").rstrip("/")
HOME_URL = BASE + "/"
PLAYER_URL = BASE + "/player.html"

RAIZ = os.path.dirname(os.path.abspath(__file__))
ARQ_LISTA = os.path.join(RAIZ, "tvonlinehd.m3u")
ARQ_ESTADO = os.path.join(RAIZ, "estado_canais.json")
ARQ_RELATORIO = os.path.join(RAIZ, "relatorio.json")

LIMITE_FALHAS = int(os.environ.get("LIMITE_FALHAS", "6"))  # remover após 6 updates
TIMEOUT_HTTP = float(os.environ.get("TIMEOUT_HTTP", "15"))
TIMEOUT_PROBE = float(os.environ.get("TIMEOUT_PROBE", "25"))
WORKERS_HTTP = int(os.environ.get("WORKERS_HTTP", "24"))
WORKERS_PROBE = int(os.environ.get("WORKERS_PROBE", "10"))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Sinônimos para juntar categorias equivalentes (o site pode renomear chips).
SINONIMOS = {
    "noticias": "Notícias",
    "notícias": "Notícias",
    "news": "Notícias",
    "esportes": "Esportes",
    "esporte": "Esportes",
    "infantil": "Infantil",
    "kids": "Infantil",
    "desenhos": "Infantil",
    "documentarios": "Documentários",
    "documentários": "Documentários",
    "documentario": "Documentários",
    "filmes e series": "Filmes e Séries",
    "filmes e séries": "Filmes e Séries",
    "filmes": "Filmes e Séries",
    "series": "Filmes e Séries",
    "séries": "Filmes e Séries",
    "abertos": "Abertos",
    "aberto": "Abertos",
    "variedades": "Variedades",
    "variedade": "Variedades",
    "portugal": "Portugal",
    "pt": "Portugal",
}

# Ordem das categorias na lista (mantém a ordem original do site como base).
ORDEM_PADRAO = [
    "Esportes",
    "Abertos",
    "Notícias",
    "Filmes e Séries",
    "Variedades",
    "Infantil",
    "Documentários",
    "Portugal",
]

LOG_LINHAS = []


def log(msg=""):
    print(msg, flush=True)
    LOG_LINHAS.append(str(msg))


# ---------------------------------------------------------------------------
# HTTP (sem dependências externas)
# ---------------------------------------------------------------------------

try:
    import urllib.request as _url
    import urllib.error as _urlerr
    import gzip as _gzip
    import zlib as _zlib
except ImportError:  # pragma: no cover
    _url = None


def http_get(url, timeout=None, referer=None, aceitar_erro=False):
    """GET simples com UA de browser. Retorna (status, bytes, headers)."""
    timeout = timeout or TIMEOUT_HTTP
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "no-cache",
    }
    if referer:
        headers["Referer"] = referer
    req = _url.Request(url, headers=headers)
    try:
        with _url.urlopen(req, timeout=timeout) as resp:
            dados = resp.read()
            enc = (resp.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc:
                dados = _gzip.decompress(dados)
            elif "deflate" in enc:
                try:
                    dados = _zlib.decompress(dados)
                except _zlib.error:
                    dados = _zlib.decompress(dados, -_zlib.MAX_WBITS)
            return resp.status, dados, dict(resp.headers)
    except _urlerr.HTTPError as e:
        corpo = b""
        try:
            corpo = e.read()
        except Exception:
            pass
        if aceitar_erro:
            return e.code, corpo, dict(e.headers or {})
        return e.code, corpo, dict(e.headers or {})
    except Exception as e:  # timeout, DNS, SSL...
        return 0, str(e).encode("utf-8", "ignore"), {}


def texto(url):
    status, dados, _ = http_get(url)
    if status != 200:
        raise RuntimeError(f"Falha ao baixar {url} (HTTP {status})")
    for enc in ("utf-8", "latin-1"):
        try:
            return dados.decode(enc)
        except UnicodeDecodeError:
            continue
    return dados.decode("utf-8", "ignore")


# ---------------------------------------------------------------------------
# Utilidades de texto
# ---------------------------------------------------------------------------

def limpar_entidades(s):
    if not s:
        return ""
    s = s.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
    s = s.replace("&apos;", "'").replace("&lt;", "<").replace("&gt;", ">")
    s = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    return s.strip()


def slug(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalizar_categoria(nome):
    nome = limpar_entidades(nome or "").strip()
    if not nome or slug(nome) in ("todos", "all", "todos os canais"):
        return None
    return SINONIMOS.get(slug(nome), nome)


def esc_m3u(s):
    return (s or "").replace("\r", " ").replace("\n", " ").strip()


# ---------------------------------------------------------------------------
# Coleta 1: catálogo embutido no player (window.TV)
# ---------------------------------------------------------------------------

def extrair_window_tv(html):
    m = re.search(r"window\.TV\s*=\s*", html)
    if not m:
        return None
    dec = json.JSONDecoder()
    try:
        obj, _ = dec.raw_decode(html[m.end():])
        return obj
    except Exception as e:
        log(f"  [aviso] não foi possível decodificar window.TV: {e}")
    return None


def coletar_catalogo():
    """Lê a home (cards + chips) e o player (catálogo com os links)."""
    log("[1/5] Baixando página inicial...")
    home = texto(HOME_URL)

    log("[2/5] Lendo categorias do site...")
    categorias = {}
    for cid, nome in re.findall(
        r'data-cat="(\d+)"[^>]*>([^<]*)</button>', home
    ):
        n = normalizar_categoria(nome)
        if n:
            categorias[int(cid)] = n
    log(f"      categorias encontradas: {', '.join(categorias.values()) or '(nenhuma)'}")

    cards = {}
    padrao_card = re.compile(r'<div class="channel-card"[^>]*>', re.S)
    aberturas = list(padrao_card.finditer(home))
    for i, m in enumerate(aberturas):
        tag = m.group(0)
        fim = aberturas[i + 1].start() if i + 1 < len(aberturas) else len(home)
        bloco = home[m.start():fim]
        attrs = dict(re.findall(r'data-(id|name|num|cats)="([^"]*)"', tag))
        cid = attrs.get("id", "")
        if not cid:
            continue
        nome = attrs.get("name", "")
        num = attrs.get("num", "")
        cats = attrs.get("cats", "")
        logo = re.search(r'<img[^>]*src="([^"]+)"', bloco)
        link = re.search(r'href="([^"]*player\.html[^"]*)"', bloco)
        cards[cid] = {
            "id": cid,
            "nome": limpar_entidades(nome),
            "num": num,
            "cats": [int(x) for x in cats.split(",") if x.strip().isdigit()],
            "logo": urljoin(HOME_URL, limpar_entidades(logo.group(1))) if logo else "",
            "player": urljoin(HOME_URL, limpar_entidades(link.group(1))) if link else "",
            "iframe": 'data-iframe="1"' in bloco,
        }
    log(f"      cards de canal na home: {len(cards)}")

    log("[3/5] Abrindo pagina do player para pegar os links de transmissao...")
    player_html = texto(PLAYER_URL)
    tv = extrair_window_tv(player_html)
    if not tv or "channels" not in tv:
        raise RuntimeError("Catálogo não encontrado no player (window.TV ausente).")

    # categorias também podem vir do catálogo (fallback / complemento)
    for c in tv.get("categories") or []:
        nome = normalizar_categoria(c.get("name"))
        if nome:
            categorias.setdefault(int(c.get("id")), nome)

    canais = []
    vistos_link = set()
    for c in tv["channels"]:
        cid = c.get("id") or ""
        card = cards.get(cid, {})
        link = (c.get("s") or "").strip()
        nome = limpar_entidades(c.get("n") or card.get("nome") or cid)
        if not nome:
            continue
        ids_cat = [i for i in (c.get("c") or card.get("cats") or []) if int(i) in categorias]
        nomes_cat = []
        for i in ids_cat:
            n = categorias.get(int(i))
            if n and n not in nomes_cat:
                nomes_cat.append(n)
        logo = c.get("i") or card.get("logo") or ""
        if logo:
            logo = urljoin(HOME_URL, limpar_entidades(logo))
        if re.search(r"/(logo\.png)$", urlparse(logo).path or ""):
            logo = ""  # logo genérica do site não ajuda no SS IPTV
        canais.append(
            {
                "id": cid,
                "nome": nome,
                "logo": logo,
                "link": link,
                "categorias": nomes_cat,
                "player": card.get("player") or f"{PLAYER_URL}?id={c.get('e','')}",
                "num": card.get("num") or "",
                "ordem_cat": ids_cat,
            }
        )
        if link:
            vistos_link.add(link)

    # canais que existem na home mas não estão no catálogo do player (raro)
    for cid, card in cards.items():
        if cid in {c["id"] for c in canais}:
            continue
        nomes_cat = [categorias[i] for i in card["cats"] if i in categorias]
        canais.append(
            {
                "id": cid,
                "nome": card["nome"],
                "logo": "" if card["logo"].endswith("/logo.png") else card["logo"],
                "link": "",
                "categorias": nomes_cat,
                "player": card["player"],
                "num": card["num"],
                "ordem_cat": card["cats"],
            }
        )

    log(f"      canais no catálogo: {len(canais)}")
    com_link = sum(1 for c in canais if c["link"])
    log(f"      canais com link direto .m3u8: {com_link}")
    return canais, categorias


# ---------------------------------------------------------------------------
# Teste de reprodução
# ---------------------------------------------------------------------------

def _ffprobe(caminho):
    return shutil.which(caminho)


def testar_http(url):
    """Etapa 1: o manifesto responde e é HLS de verdade?"""
    if not url:
        return False, "sem link"
    if not url.lower().startswith(("http://", "https://")):
        return False, "formato não suportado"
    status, dados, headers = http_get(url, timeout=TIMEOUT_HTTP, referer=HOME_URL, aceitar_erro=True)
    if status in (301, 302, 303, 307, 308):
        destino = headers.get("Location") or headers.get("location")
        if destino:
            status, dados, headers = http_get(urljoin(url, destino), timeout=TIMEOUT_HTTP, referer=HOME_URL, aceitar_erro=True)
    if status != 200:
        return False, f"HTTP {status or 'sem resposta'}"
    amostra = dados[:4000].decode("utf-8", "ignore")
    if "#EXTM3U" not in amostra:
        return False, "não é manifesto HLS"
    if "#EXT-X-STREAM-INF" in amostra:
        return True, "HLS (master)"
    if "#EXTINF" in amostra or "#EXT-X-TARGETDURATION" in amostra:
        return True, "HLS (ao vivo)"
    return True, "HLS (vazio)"


def testar_video(url):
    """Etapa 2: o vídeo decodifica? (usa ffprobe quando disponível)"""
    if not url:
        return False, "sem link"
    if _ffprobe("ffprobe"):
        cmd = [
            "ffprobe", "-v", "error", "-rw_timeout", "10000000",
            "-analyzeduration", "5000000", "-probesize", "2000000",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height",
            "-of", "csv=p=0", url,
        ]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_PROBE)
            saida = (p.stdout or "").strip().splitlines()
            if saida and saida[0].strip(","):
                return True, saida[0].strip()
            return False, (p.stderr or "sem vídeo").strip().splitlines()[-1][:70] if p.stderr else "sem vídeo"
        except subprocess.TimeoutExpired:
            return False, "timeout do ffprobe"
        except Exception as e:
            return False, f"erro ffprobe: {e}"
    return testar_http(url)


def testar_canal(canal):
    url = canal.get("link") or ""
    ok, motivo = testar_http(url)
    if ok and canal.get("probe", True):
        ok2, motivo2 = testar_video(url)
        if not ok2:
            return False, motivo2
        return True, motivo2
    return ok, motivo


# ---------------------------------------------------------------------------
# Estado / reconciliação
# ---------------------------------------------------------------------------

def hoje():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def carregar_estado():
    if os.path.exists(ARQ_ESTADO):
        try:
            with open(ARQ_ESTADO, encoding="utf-8") as f:
                st = json.load(f)
            st.setdefault("canais", {})
            return st
        except Exception as e:
            log(f"  [aviso] estado ilegível ({e}); começando do zero.")
    return {"versao": 1, "atualizacoes": 0, "limite_falhas": LIMITE_FALHAS, "canais": {}}


def salvar_json(caminho, obj):
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=False)
        f.write("\n")
    os.replace(tmp, caminho)


# ---------------------------------------------------------------------------
# Geração da M3U
# ---------------------------------------------------------------------------

def agrupar(canais, ordem_site):
    """Agrupa por categoria (uma entrada por categoria; a 1ª da ordem do site)."""
    grupos = {}
    for c in canais:
        cats = c.get("categorias") or []
        # "Todos" não é categoria: usa a mais específica (menor id > 0)
        especificas = [x for x in cats if slug(x) != "todos"]
        cat = (especificas or cats or ["Outros"])[0]
        grupos.setdefault(cat, []).append(c)

    ordem = [x for x in (ordem_site + ORDEM_PADRAO) if x in grupos]
    ordem = list(dict.fromkeys(ordem))
    for cat in grupos:
        if cat not in ordem and cat != "Outros":
            ordem.append(cat)
    if "Outros" in grupos:
        ordem.append("Outros")
    return [(cat, grupos[cat]) for cat in ordem]


def gerar_m3u(canais, ordem_site):
    linhas = ["#EXTM3U"]
    total = 0
    for cat, itens in agrupar(canais, ordem_site):
        for c in sorted(
            itens,
            key=lambda x: (
                min(x.get("ordem_cat") or [99]),
                slug(x["nome"]),
            ),
        ):
            nome = esc_m3u(c["nome"])
            logo = esc_m3u(c.get("logo") or "")
            attrs = f' tvg-id="{esc_m3u(c["id"])}" tvg-name="{nome}"'
            if logo:
                attrs += f' tvg-logo="{logo}"'
            attrs += f' group-title="{esc_m3u(cat)}"'
            linhas.append(f"#EXTINF:-1{attrs},{nome}")
            linhas.append(c["link"])
            total += 1
    return "\n".join(linhas) + "\n", total


def gerar_m3u_completa(canais, ordem_site):
    """Versão sem filtro (inclui canais temporariamente com falha)."""
    return gerar_m3u([c for c in canais if c.get("link")], ordem_site)


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Gera a M3U do TV Online HD")
    ap.add_argument("--sem-testar", action="store_true", help="não valida os streams")
    ap.add_argument("--testar-tudo", action="store_true", help="valida o vídeo de todos (mais lento)")
    ap.add_argument("--iniciar-estado", action="store_true", help="zera contadores de falha")
    ap.add_argument("--limite-falhas", type=int, default=None, help="padrão 6")
    args = ap.parse_args()

    global LIMITE_FALHAS
    if args.limite_falhas:
        LIMITE_FALHAS = args.limite_falhas

    inicio = time.time()
    log("=" * 70)
    log(f" TV Online HD -> M3U  |  {dt.datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    log("=" * 70)

    try:
        canais, categorias_por_id = coletar_catalogo()
    except Exception as e:
        log(f"\n[ERRO] Não foi possível ler o catálogo: {e}")
        log("A lista anterior (se existir) foi preservada.")
        return 2

    estado = carregar_estado()
    if args.iniciar_estado:
        log("\n[opção] --iniciar-estado: contadores de falha zerados.")
        estado = {"versao": 1, "atualizacoes": 0, "limite_falhas": LIMITE_FALHAS, "canais": {}}

    estado["atualizacoes"] = int(estado.get("atualizacoes", 0)) + 1
    estado["limite_falhas"] = LIMITE_FALHAS
    log(f"\n[4/5] Validando transmissões (atualizacao #{estado['atualizacoes']})...")

    resultados = {}
    if args.sem_testar:
        log("      (validação desativada por --sem-testar)")
        for c in canais:
            resultados[c["id"]] = (bool(c["link"]), "não testado")
    else:
        para_testar = [c for c in canais if c["link"]]
        sem_link = [c for c in canais if not c["link"]]
        for c in sem_link:
            resultados[c["id"]] = (False, "site não informa link direto")

        ff = bool(_ffprobe("ffprobe"))
        log(f"      ffprobe disponível: {'sim' if ff else 'não (só checagem HTTP)'}")
        feitos = 0
        t0 = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS_HTTP) as ex:
            futuros = {ex.submit(testar_canal, c): c for c in para_testar}
            for fut in concurrent.futures.as_completed(futuros):
                c = futuros[fut]
                try:
                    ok, motivo = fut.result()
                except Exception as e:
                    ok, motivo = False, f"erro: {e}"
                resultados[c["id"]] = (ok, motivo)
                feitos += 1
                if feitos % 50 == 0 or feitos == len(para_testar):
                    log(f"      {feitos}/{len(para_testar)} testados ({time.time()-t0:.0f}s)")

    # ---- reconciliação ----
    st_canais = estado["canais"]
    ativos, falhando, removidos, novos = [], [], [], []
    marcas = hoje()

    for c in canais:
        ok, motivo = resultados.get(c["id"], (False, "não testado"))
        reg = st_canais.get(c["id"])
        if reg is None:
            reg = {"nome": c["nome"], "falhas": 0, "desde": marcas, "ultimo_ok": ""}
            st_canais[c["id"]] = reg
            if ok:
                novos.append(c)
        reg["nome"] = c["nome"]
        reg["link"] = c["link"]
        reg["categorias"] = c["categorias"]
        reg["ultimo_teste"] = marcas
        if not c["link"]:
            reg["ultimo_motivo"] = motivo
            continue
        if ok:
            if reg.get("falhas"):
                # registra a sequencia interrompida (canal instavel, voltou a tocar)
                hist = reg.setdefault("historico", [])
                hist.append({"ate": marcas, "falhas": int(reg["falhas"])})
                del hist[:-3]
            reg["falhas"] = 0
            reg["ultimo_ok"] = marcas
            reg["ultimo_motivo"] = motivo
            c["sem_falhas"] = True
            ativos.append(c)
        else:
            reg["falhas"] = int(reg.get("falhas", 0)) + 1
            reg["ultimo_motivo"] = motivo
            c["falhas"] = reg["falhas"]
            if reg["falhas"] >= LIMITE_FALHAS:
                removidos.append(c)
            else:
                falhando.append(c)

    # canais que saíram do site
    ids_site = {c["id"] for c in canais}
    fora_do_site = [i for i in st_canais if i not in ids_site]
    for i in fora_do_site:
        st_canais.pop(i, None)

    # ---- lista final ----
    final = ativos + falhando
    ordem_ids = sorted({i for c in canais for i in (c.get("ordem_cat") or [])})
    ordem_site = [categorias_por_id[i] for i in ordem_ids if i in categorias_por_id]
    for c in canais:
        for cat in c["categorias"]:
            if cat not in ordem_site:
                ordem_site.append(cat)

    conteudo, total = gerar_m3u(final, ordem_site)
    with open(ARQ_LISTA, "w", encoding="utf-8", newline="\n") as f:
        f.write(conteudo)

    # lista completa (para quem quiser tudo, inclusive os que estão em falha)
    completo, total_completo = gerar_m3u_completa(
        [c for c in canais if c["link"]], ordem_site
    )
    with open(os.path.join(RAIZ, "tvonlinehd_completo.m3u"), "w", encoding="utf-8", newline="\n") as f:
        f.write(completo)

    salvar_json(ARQ_ESTADO, estado)

    rel = {
        "gerado_em": marcas,
        "atualizacao": estado["atualizacoes"],
        "limite_falhas": LIMITE_FALHAS,
        "catalogo_site": len(canais),
        "com_link": sum(1 for c in canais if c["link"]),
        "na_lista": total,
        "tocando": len(ativos),
        "em_falha": len(falhando),
        "removidos": len(removidos),
        "novos": len(novos),
        "fora_do_site": len(fora_do_site),
        "por_categoria": {},
        "detalhes_falha": [
            {"nome": c["nome"], "motivo": resultados.get(c["id"], (False, ""))[1]}
            for c in falhando
        ][:40],
    }
    for cat, itens in agrupar(final, ordem_site):
        rel["por_categoria"][cat] = len(itens)
    salvar_json(ARQ_RELATORIO, rel)

    # ---- log final ----
    log("\n[5/5] Lista gerada")
    log("-" * 70)
    log(f"Canais no site .............: {len(canais)}")
    log(f"Com link direto ...........: {rel['com_link']}")
    log(f"TOCANDO (na lista) ........: {len(ativos)}")
    log(f"Em falha (mantidos) .......: {len(falhando)}  (removidos após {LIMITE_FALHAS} falhas)")
    log(f"Removidos nesta rodada ....: {len(removidos)}")
    log(f"Novos canais ..............: {len(novos)}")
    log(f"Fora do site (limpo) ......: {len(fora_do_site)}")
    log(f"Total na lista ............: {total} canais")
    log("-" * 70)
    log("Canais por categoria:")
    for cat, n in rel["por_categoria"].items():
        log(f"   {cat:<20} {n:>4}")
    if removidos:
        log("-" * 70)
        log(f"Removidos após {LIMITE_FALHAS} falhas:")
        for c in removidos[:20]:
            log(f"   - {c['nome']}")
    log("-" * 70)
    log(f"Arquivo: {os.path.basename(ARQ_LISTA)}  ({total} canais)")
    log(f"Tempo total: {time.time()-inicio:.0f}s")

    with open("log_ultima_execucao.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(LOG_LINHAS) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

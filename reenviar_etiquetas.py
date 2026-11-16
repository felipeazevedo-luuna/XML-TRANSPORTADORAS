"""
Reenvia ao Platinum (OMS) a etiqueta RTE/Rodonaves de NFs específicas.

Uso (GitHub Actions > "Reenviar etiquetas RTE" > Run workflow): informe os
números das NFs separados por vírgula, ex.: 122954, 123051.

Para cada NF: acha o XML no Zecore pela chave de acesso, gera a etiqueta pela
API da RTE e sobe no Platinum com o acesso da Rodonaves. Não mexe no histórico
nem envia NOTFIS.
"""

import os
import sys
from datetime import date
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

import rte_etiqueta as rte
import bot

ZECORE = "https://zecore.zebrands.mx"
CNPJ_EMITENTE = "42418313000104"
PASTA = Path("xmls_baixados") / "etiquetas_rte_reenvio"


def _meses_recentes(n=3):
    hoje = date.today()
    a, m = hoje.year, hoje.month
    for _ in range(n):
        yield f"{a % 100:02d}{m:02d}"
        a, m = (a, m - 1) if m > 1 else (a - 1, 12)


def zecore_sessao() -> requests.Session:
    s = requests.Session()
    r = s.post(f"{ZECORE}/api/method/login", timeout=30,
               data={"usr": os.environ["SISTEMA_EMAIL"], "pwd": os.environ["SISTEMA_SENHA"]})
    r.raise_for_status()
    return s


def baixar_xml(s: requests.Session, nnf: str) -> bytes | None:
    """O nome do arquivo no Zecore é a chave de acesso: 35 + AAMM + CNPJ + 55 + série 001 + NF + tpEmis 1."""
    for aamm in _meses_recentes():
        prefixo = f"35{aamm}{CNPJ_EMITENTE}55001{int(nnf):09d}1"
        r = s.get(f"{ZECORE}/api/resource/File", timeout=60, params={
            "filters": f'[["file_name","like","{prefixo}%.xml"]]',
            "fields": '["file_name","file_url"]', "limit_page_length": 5})
        r.raise_for_status()
        achados = r.json().get("data") or []
        if achados:
            x = s.get(ZECORE + achados[0]["file_url"], timeout=60)
            x.raise_for_status()
            print(f"   📎  NF {nnf}: {achados[0]['file_name']}")
            return x.content
    return None


def main():
    nfs = [n.strip() for n in os.getenv("NFS", "").replace(";", ",").split(",") if n.strip()]
    if not nfs or not all(n.isdigit() for n in nfs):
        print("❌  Informe os números das NFs separados por vírgula (só dígitos).")
        sys.exit(1)

    os.environ["TRANSPORTADORA"] = "RODONAVES"   # usa o acesso do OMS da Rodonaves
    PASTA.mkdir(parents=True, exist_ok=True)
    print(f"🏷️  Reenvio de etiqueta RTE para: {', '.join(nfs)}")

    s = zecore_sessao()
    etiquetas, falhas = [], []
    for nnf in nfs:
        try:
            xml = baixar_xml(s, nnf)
            if not xml:
                print(f"   ❌  NF {nnf}: XML não encontrado no Zecore.")
                falhas.append(nnf)
                continue
            pdf = PASTA / f"RTE_{nnf}.pdf"
            pdf.write_bytes(rte.gerar_etiqueta_pdf(xml))
            print(f"   ✅  NF {nnf}: etiqueta gerada ({rte.ler_nfe(xml)['volumes']} volume(s)).")
            etiquetas.append((nnf, pdf))
        except Exception as e:
            print(f"   ❌  NF {nnf}: {e}")
            falhas.append(nnf)

    resultado = {"platinum_ok": [], "platinum_falha": []}
    if etiquetas:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(accept_downloads=True)
            resultado = bot.platinum_subir_etiquetas(context, etiquetas)
            browser.close()

    print(f"\n📊  Etiquetas geradas: {len(etiquetas)} | Platinum OK: {len(resultado['platinum_ok'])}"
          f" | falhas: {falhas + resultado['platinum_falha']}")
    if falhas or resultado.get("platinum_falha") or resultado.get("platinum_pulado"):
        sys.exit(1)


if __name__ == "__main__":
    main()

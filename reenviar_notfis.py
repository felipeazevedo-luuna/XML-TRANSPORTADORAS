"""
Reenvia à RTE o NOTFIS de NFs específicas (ex.: um NOTFIS que saiu errado).

Uso (GitHub Actions > "Reenviar NOTFIS RTE" > Run workflow): informe os números
das NFs separados por vírgula. NF de filial: use SÉRIE/NÚMERO, ex.: 14/691.
Sem série, vale a série 1 (matriz).

Busca o XML de cada NF no Zecore pela chave de acesso, gera um NOTFIS (embarcador
sempre a matriz, como no bot). Com enviar=false (padrão) só gera o arquivo e o guarda
no artefato, para conferir; com enviar=true manda pelo mesmo e-mail do bot.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

import notfis
import rodonaves as rd
from reenviar_etiquetas import zecore_sessao, _meses_recentes, ZECORE

CNPJS = ("42418313000104", "42418313000619")   # matriz e filial


def baixar_xml(s, serie: str, nnf: str) -> bytes | None:
    for cnpj in CNPJS:
        for aamm in _meses_recentes():
            prefixo = f"35{aamm}{cnpj}55{int(serie):03d}{int(nnf):09d}1"
            r = s.get(f"{ZECORE}/api/resource/File", timeout=60, params={
                "filters": f'[["file_name","like","{prefixo}%.xml"]]',
                "fields": '["file_name","file_url"]', "limit_page_length": 5})
            r.raise_for_status()
            achados = r.json().get("data") or []
            if achados:
                x = s.get(ZECORE + achados[0]["file_url"], timeout=60)
                x.raise_for_status()
                print(f"   📎  NF {serie}/{nnf}: {achados[0]['file_name']}")
                return x.content
    return None


def main():
    pedidas = [n.strip() for n in os.getenv("NFS", "").replace(";", ",").split(",") if n.strip()]
    itens = []
    for n in pedidas:
        serie, _, nnf = n.rpartition("/")
        if not nnf.isdigit() or (serie and not serie.isdigit()):
            print(f"❌  NF inválida: {n!r} (use 123456 ou 14/691)")
            sys.exit(1)
        itens.append((serie or "1", nnf))
    if not itens:
        print("❌  Informe ao menos uma NF.")
        sys.exit(1)

    print(f"📄  Reenvio de NOTFIS para: {', '.join(pedidas)}")
    s = zecore_sessao()
    xmls = []
    for serie, nnf in itens:
        x = baixar_xml(s, serie, nnf)
        if not x:
            print(f"❌  NF {serie}/{nnf}: XML não encontrado no Zecore — nada foi enviado.")
            sys.exit(1)
        xmls.append(x)

    agora = datetime.now()
    pasta = Path("xmls_baixados")
    pasta.mkdir(exist_ok=True)
    caminho = pasta / f"NOTFIS_LUUNA_{agora:%Y%m%d_%H%M}_REENVIO.txt"
    caminho.write_text(notfis.gerar_notfis(xmls, agora), encoding="ascii", newline="")
    linha311 = next(l for l in caminho.read_bytes().decode("ascii").split("\r\n") if l.startswith("311"))
    print(f"📄  NOTFIS gerado: {caminho.name} ({len(xmls)} NF) — embarcador {linha311[3:17]}")

    if os.getenv("ENVIAR", "false").lower() != "true":
        print("ℹ️  Modo só gerar: NOTFIS salvo no artefato, nenhum e-mail enviado.")
        return
    if rd.enviar_notfis_por_email([caminho], len(xmls)) is not True:
        print("❌  NOTFIS não enviado.")
        sys.exit(1)


if __name__ == "__main__":
    main()

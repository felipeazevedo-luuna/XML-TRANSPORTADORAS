"""
Rodonaves (RTE) no bot XML Transportadoras.

1. Para cada XML: consulta a malha da RTE e gera o PDF da etiqueta única (rte_etiqueta.py)
2. Gera o NOTFIS só com as NFs que ficaram com etiqueta (notfis.py)
3. Envia o NOTFIS por e-mail para a RTE

As etiquetas sobem no Platinum pela mesma rotina da JAMEF (platinum_subir_etiquetas, no bot.py).

Variáveis (GitHub Secrets / .env):
    RTE_USER, RTE_PASS        acesso à API da RTE (obrigatórias)
    RTE_AUTH_TYPE             padrão "dev"
    RTE_ENVIAR_NOTFIS         "false" desliga o envio do NOTFIS (padrão: envia)
    RTE_EMAIL_NOTFIS          destino do NOTFIS (padrão edi.emissao@rte.com.br)
"""

import os
import json
import base64
import smtplib
from pathlib import Path
from datetime import datetime, time as dtime, timedelta, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

import rte_etiqueta as rte
import notfis

# NFs (chave de 44 dígitos) que já foram num NOTFIS enviado à RTE. O pedido pode
# ficar pendente (ex.: etiqueta sem pedido no OMS) e voltar nas execuções seguintes;
# sem esse registro, a mesma NF ia para a RTE a cada 30 min (17/11: NF 691 e 123456, 6x).
ARQUIVO_NOTFIS_ENVIADOS = Path("notfis_enviados.json")


def notfis_ja_enviados() -> set[str]:
    try:
        return set(json.loads(ARQUIVO_NOTFIS_ENVIADOS.read_text(encoding="utf-8")).get("chaves", []))
    except (FileNotFoundError, ValueError):
        return set()


def registrar_notfis_enviados(chaves) -> None:
    todas = sorted(notfis_ja_enviados() | set(chaves))
    ARQUIVO_NOTFIS_ENVIADOS.write_text(json.dumps({"chaves": todas}, indent=2) + "\n", encoding="utf-8")


# ── NOTFIS só de manhã ──────────────────────────────────────────────────────
# Regra combinada em 26/11: o NOTFIS da Rodonaves só vai para a RTE nas execuções
# até a das 12:00. Depois disso o bot só gera a etiqueta; as NFs ficam na fila
# (notfis_pendentes.json, com o XML) e o NOTFIS delas sai na primeira execução
# da manhã seguinte (dia útil seguinte, pelo cron).
# O disparo das 12:00 costuma começar alguns minutos depois, por isso o corte é 12:15.
FUSO_BR = timezone(timedelta(hours=-3), "BRT")   # Brasília (sem horário de verão desde 2019)
CORTE_NOTFIS = dtime(12, 15)
ARQUIVO_NOTFIS_PENDENTES = Path("notfis_pendentes.json")


def agora_br() -> datetime:
    return datetime.now(FUSO_BR)


# Hora em que esta execução começou (import do módulo, no início do bot). A decisão
# do NOTFIS usa o INÍCIO da execução, não a hora em que a Rodonaves é processada:
# o disparo das 12:00 só chega nessa etapa uns 10–15 min depois e passaria do corte.
INICIO_EXECUCAO = agora_br()


def notfis_no_horario(agora: datetime | None = None) -> bool:
    """True se esta execução ainda pode enviar NOTFIS (começou antes do corte)."""
    return (agora or INICIO_EXECUCAO).time() < CORTE_NOTFIS


def carregar_notfis_pendentes() -> list[dict]:
    try:
        return json.loads(ARQUIVO_NOTFIS_PENDENTES.read_text(encoding="utf-8")).get("itens", [])
    except (FileNotFoundError, ValueError):
        return []


def salvar_notfis_pendentes(itens: list[dict]) -> None:
    ARQUIVO_NOTFIS_PENDENTES.write_text(
        json.dumps({"itens": itens}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def adiar_notfis(xmls: list[bytes]) -> int:
    """Põe as NFs na fila do NOTFIS do dia seguinte. Devolve quantas entraram agora."""
    itens = carregar_notfis_pendentes()
    na_fila = {i["chave"] for i in itens}
    hoje = agora_br().date().isoformat()
    novas = 0
    for dados in xmls:
        chave = _chave(dados)
        if chave in na_fila:
            continue
        itens.append({"chave": chave, "nf": int(chave[25:34]), "data": hoje,
                      "xml_b64": base64.b64encode(dados).decode("ascii")})
        na_fila.add(chave)
        novas += 1
    if novas:
        salvar_notfis_pendentes(itens)
    return novas


def enviar_notfis_pendentes(pasta: Path) -> dict | None:
    """Na manhã seguinte: manda num NOTFIS as NFs que ficaram na fila depois do corte.

    Roda no começo da execução da Rodonaves, antes do Zecore, para não depender de
    haver pedido novo no Report 3PL. Fora do horário, ou sem fila, não faz nada.
    """
    itens = carregar_notfis_pendentes()
    if not itens or not notfis_no_horario():
        return None

    enviadas = notfis_ja_enviados()
    restantes = [i for i in itens if i["chave"] not in enviadas]
    if len(restantes) < len(itens):
        print(f"   ℹ️  {len(itens) - len(restantes)} NF(s) da fila já tinham ido em NOTFIS — saem da fila.")
    if not restantes:
        salvar_notfis_pendentes([])
        return None

    nfs = ", ".join(str(i["nf"]) for i in restantes)
    print(f"\n📄  NOTFIS da fila (etiquetadas depois das 12:00): {len(restantes)} NF(s) — {nfs}")
    agora = datetime.now()
    try:
        conteudo = notfis.gerar_notfis([base64.b64decode(i["xml_b64"]) for i in restantes], agora)
        pasta.mkdir(parents=True, exist_ok=True)
        caminho = pasta / f"NOTFIS_LUUNA_{agora:%Y%m%d_%H%M}_PENDENTES.txt"
        with open(caminho, "w", encoding="ascii", newline="") as f:
            f.write(conteudo)
    except Exception as e:
        print(f"   ❌  Erro ao gerar o NOTFIS da fila: {e} — a fila fica para a próxima execução.")
        salvar_notfis_pendentes(restantes)
        return {"nfs": len(restantes), "email": False}

    email = enviar_notfis_por_email([caminho], len(restantes))
    if email is True:
        registrar_notfis_enviados([i["chave"] for i in restantes])
        salvar_notfis_pendentes([])
    else:
        # Falhou ou envio desligado: a fila continua para a próxima execução da manhã.
        salvar_notfis_pendentes(restantes)
    return {"nfs": len(restantes), "email": email}

COPIA_INTERNA = ["felipe.azevedo@zeb.mx", "israel.lopes@zeb.mx"]


def _envio_notfis_ligado() -> bool:
    return os.getenv("RTE_ENVIAR_NOTFIS", "true").strip().lower() != "false"


def rodonaves_processar_xmls(arquivos: list[Path], pasta: Path) -> dict:
    """Gera as etiquetas e o NOTFIS das NFs baixadas. Devolve o resultado para o Chat e o histórico."""
    vazio = {"ok": [], "falha": [], "etiquetas_pdfs": [], "notfis": None, "notfis_nfs": 0}

    if not (os.getenv("RTE_USER") and os.getenv("RTE_PASS")):
        print("   ⚠️  RTE_USER / RTE_PASS não configurados — pulando Rodonaves.")
        return {**vazio, "pulado": True}

    xmls = [f for f in arquivos if f.suffix.lower() == ".xml"]
    if not xmls:
        print("   ⚠️  Nenhum XML para gerar etiqueta RTE.")
        return vazio

    try:
        rte.obter_token()
        print("\n🔐  Token da API RTE OK!")
    except Exception as e:
        print(f"   ❌  Token da API RTE falhou: {e}")
        return {**vazio, "falha": [f.name for f in xmls], "erro_login": True}

    pasta_etiquetas = pasta / "etiquetas_rte"
    pasta_etiquetas.mkdir(parents=True, exist_ok=True)

    print(f"\n🏷️  Gerando {len(xmls)} etiqueta(s) RTE...")
    ok, falha, etiquetas_pdfs, xmls_prontos = [], [], [], []
    for xml in xmls:
        dados = xml.read_bytes()
        try:
            nf = rte.ler_nfe(dados)
        except Exception as e:
            print(f"   ❌  {xml.name}: XML inválido ({e})")
            falha.append(f"{xml.name} — XML inválido")
            continue

        n_nf = nf["nnf"].lstrip("0") or nf["nnf"]
        try:
            pdf = rte.gerar_etiqueta_pdf(dados)          # consulta a malha e desenha a etiqueta
        except requests.HTTPError as e:
            motivo = f"API RTE respondeu {e.response.status_code}"
            print(f"   ❌  NF {n_nf}: {motivo} — {e.response.text[:200]}")
            falha.append(f"NF {n_nf} — {motivo}")
            continue
        except Exception as e:
            print(f"   ❌  NF {n_nf}: {e}")
            falha.append(f"NF {n_nf} — {str(e)[:80]}")
            continue

        caminho = pasta_etiquetas / f"RTE_{n_nf}.pdf"
        caminho.write_bytes(pdf)
        print(f"   🏷️  Etiqueta NF {n_nf}: {nf['volumes']} volume(s) → {caminho}")
        ok.append(f"NF {n_nf} ({nf['volumes']} vol.)")
        etiquetas_pdfs.append((n_nf, caminho))
        xmls_prontos.append(dados)

    # NOTFIS só com as notas que têm etiqueta (nota sem etiqueta não sai) e que ainda
    # não foram num NOTFIS enviado. Um arquivo só: o embarcador é sempre a matriz.
    notfis_paths, notfis_chaves, ja_enviado, adiadas = [], [], False, 0
    if xmls_prontos:
        enviadas = notfis_ja_enviados()
        novas = [(d, rte.ler_nfe(d)) for d in xmls_prontos]
        novas = [(d, nf) for d, nf in novas if _chave(d) not in enviadas]
        repetidas = len(xmls_prontos) - len(novas)
        if repetidas:
            print(f"\n   ℹ️  {repetidas} NF(s) já foram em NOTFIS anterior — não reenviadas.")
        if not novas:
            ja_enviado = True
        elif not notfis_no_horario():
            # Depois das 12:00: só etiqueta. O NOTFIS vai na manhã seguinte.
            entraram = adiar_notfis([d for d, _ in novas])
            adiadas = len(novas)
            print(f"\n⏰  Depois das 12:00: NOTFIS de {adiadas} NF(s) fica para a manhã seguinte"
                  f" ({entraram} entraram na fila agora).")
        else:
            agora = datetime.now()
            try:
                conteudo = notfis.gerar_notfis([d for d, _ in novas], agora)
                caminho = pasta / f"NOTFIS_LUUNA_{agora:%Y%m%d_%H%M}.txt"
                with open(caminho, "w", encoding="ascii", newline="") as f:
                    f.write(conteudo)
                notfis_paths.append(caminho)
                notfis_chaves = [_chave(d) for d, _ in novas]
                print(f"\n📄  NOTFIS gerado: {caminho.name} ({len(novas)} NF)")
            except Exception as e:
                print(f"   ❌  Erro ao gerar o NOTFIS: {e}")

    print(f"\n   📊  RTE: {len(ok)} etiqueta(s) OK, {len(falha)} falha(s)")
    return {"ok": ok, "falha": falha, "etiquetas_pdfs": etiquetas_pdfs,
            "notfis": notfis_paths or None, "notfis_nfs": len(notfis_chaves),
            "notfis_chaves": notfis_chaves, "notfis_ja_enviado": ja_enviado,
            "notfis_adiado": adiadas}


def _chave(xml_bytes: bytes) -> str:
    """Chave de acesso (44 dígitos) da NF-e, pelo NOTFIS (mesma leitura do arquivo)."""
    return notfis.ler_nfe(xml_bytes)["chave"]


def enviar_notfis_por_email(notfis_path: Path | list[Path] | None, n_nfs: int) -> bool | None:
    """Envia o(s) NOTFIS (um por CNPJ emitente) para a RTE com cópia interna, num e-mail só.
    Devolve None quando não havia o que enviar."""
    if not notfis_path:
        return None
    arquivos = list(notfis_path) if isinstance(notfis_path, (list, tuple)) else [notfis_path]
    if not _envio_notfis_ligado():
        print("   ℹ️  RTE_ENVIAR_NOTFIS=false — NOTFIS gerado, mas não enviado.")
        return None

    usuario = os.getenv("GMAIL_USUARIO", "carlos.berbert@zeb.mx")
    senha = os.getenv("GMAIL_APP_PASSWORD", "")
    if not senha:
        print("   ⚠️  GMAIL_APP_PASSWORD não configurado — NOTFIS não enviado.")
        return False

    para = [os.getenv("RTE_EMAIL_NOTFIS", "edi.emissao@rte.com.br")]
    cc = [usuario] + [e for e in COPIA_INTERNA if e != usuario]
    data_hoje = datetime.now().strftime("%d/%m/%Y")
    assunto = f"NOTFIS LUUNA {data_hoje} - RODONAVES"

    corpo = (
        '<html><body style="font-family: Arial, sans-serif; color: #1a1a1a;">'
        f"<p>Olá,</p><p>Segue o arquivo NOTFIS da coleta de hoje ({data_hoje}), "
        f"com {n_nfs} nota(s) fiscal(is).</p>"
        f"<p>Arquivo(s): <strong>{', '.join(a.name for a in arquivos)}</strong></p>"
        '<p style="color:#6b7280;font-size:12px;">Enviado automaticamente pelo Bot XML Transportadoras — Zebrands/Luuna</p>'
        "</body></html>"
    )

    try:
        print(f"\n📧  Enviando NOTFIS — Para: {', '.join(para)} | CC: {', '.join(cc)}")
        msg = MIMEMultipart()
        msg["From"], msg["To"], msg["Cc"], msg["Subject"] = usuario, ", ".join(para), ", ".join(cc), assunto
        msg.attach(MIMEText(corpo, "html"))
        for arquivo in arquivos:
            parte = MIMEBase("text", "plain")
            parte.set_payload(arquivo.read_bytes())
            encoders.encode_base64(parte)
            parte.add_header("Content-Disposition", f"attachment; filename={arquivo.name}")
            msg.attach(parte)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as servidor:
            servidor.login(usuario, senha)
            servidor.sendmail(usuario, para + cc, msg.as_string())
        print("   ✅  NOTFIS enviado!")
        return True
    except Exception as e:
        print(f"   ❌  Erro ao enviar o NOTFIS: {e}")
        return False


def resumo_chat(res: dict) -> str:
    """Trecho da mensagem do Google Chat com o resultado da Rodonaves."""
    if res.get("pulado"):
        return ("\n⚠️ *API RTE:* credenciais não configuradas — etiquetas e NOTFIS NÃO gerados"
                " (os pedidos serão tentados de novo na próxima execução)")
    if res.get("erro_login"):
        return "\n❌ *API RTE:* token recusado — ver log do Actions (pedidos ficam para a próxima execução)"

    ok, falha = res.get("ok", []), res.get("falha", [])
    txt = f"\n🏷️ *Etiquetas RTE:* {len(ok)} OK, {len(falha)} falha(s)"
    if ok:
        txt += "\n   ✅ " + " | ".join(ok[:10])
    if falha:
        txt += "\n   ⚠️ " + " | ".join(falha[:10])
        txt += "\n   👉 Gere as que falharam pelo painel de etiquetas Rodonaves e envie o NOTFIS delas."

    email = res.get("notfis_email")
    if res.get("notfis_ja_enviado"):
        txt += "\n📄 *NOTFIS:* NF(s) desta rodada já tinham ido em NOTFIS anterior — nada reenviado"
    if res.get("notfis_adiado"):
        txt += (f"\n⏰ *NOTFIS:* {res['notfis_adiado']} NF(s) etiquetada(s) depois das 12:00 —"
                " o NOTFIS vai para a RTE na manhã seguinte")
    if res.get("notfis"):
        if email is True:
            txt += f"\n📄 *NOTFIS:* {res.get('notfis_nfs', 0)} NF(s) enviada(s) para a RTE"
        elif email is None:
            txt += "\n📄 *NOTFIS:* gerado, envio desligado (RTE_ENVIAR_NOTFIS=false)"
        else:
            txt += "\n❌ *NOTFIS:* falha no envio por e-mail — ver log (pedidos ficam para a próxima execução)"

    plat_ok, plat_falha = res.get("platinum_ok", []), res.get("platinum_falha", [])
    if res.get("platinum_pulado"):
        txt += "\n📦 *Platinum:* não executado (login/credenciais — ver log)"
    elif plat_ok or plat_falha:
        txt += f"\n📦 *Platinum:* {len(plat_ok)} etiqueta(s) OK, {len(plat_falha)} falha(s)"
        if plat_falha:
            txt += "\n   ⚠️ " + " | ".join(plat_falha[:10])
    return txt


def rodonaves_nao_rodou(res: dict | None) -> bool:
    """True quando a rodada da RTE não se completou e os pedidos devem ser tentados de novo."""
    if not res:
        return False
    if any(res.get(k) for k in ("pulado", "erro_login", "platinum_pulado")):
        return True
    if (res.get("etiquetas_pdfs") and not res.get("notfis") and not res.get("notfis_ja_enviado")
            and not res.get("notfis_adiado")):
        # etiquetas prontas, mas o NOTFIS não foi gerado
        return True
    return bool(res.get("notfis")) and res.get("notfis_email") is False

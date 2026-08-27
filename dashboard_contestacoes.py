import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from collections import defaultdict
import pandas as pd
import streamlit as st
import requests
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from dotenv import load_dotenv
try:
    from google.cloud import bigquery
    from google.oauth2 import service_account
    _BQ_AVAILABLE = True
except ImportError:
    _BQ_AVAILABLE = False

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

ARQUIVO_CONTESTACOES = os.path.join(os.path.dirname(__file__), "contestacoes.json")
ARQUIVO_CONTAS   = os.path.join(os.path.dirname(__file__), "accounts_to_invite.txt")
ARQUIVO_DETALHES = os.path.join(os.path.dirname(__file__), "account_details.json")
ARQUIVO_SERIAL      = os.path.join(os.path.dirname(__file__), "last_serial.json")
ARQUIVO_TIMESTAMPS  = os.path.join(os.path.dirname(__file__), "invite_timestamps.json")
ARQUIVO_FEGSYS_DONE  = os.path.join(os.path.dirname(__file__), "fegsys_done.json")
ARQUIVO_CREDENTIALS  = os.path.join(os.path.dirname(__file__), "credentials.json")

def carregar_timestamps() -> dict:
    if not os.path.exists(ARQUIVO_TIMESTAMPS):
        return {}
    with open(ARQUIVO_TIMESTAMPS, encoding="utf-8") as f:
        return json.load(f)

@st.cache_data(ttl=60)
def carregar_fegsys_done() -> set:
    """Consulta BigQuery para obter contas com token FEG ativo. Fallback para JSON local."""
    if _BQ_AVAILABLE and os.path.exists(ARQUIVO_CREDENTIALS):
        try:
            creds = service_account.Credentials.from_service_account_file(
                ARQUIVO_CREDENTIALS,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            client = bigquery.Client(credentials=creds, project="grupofeg-lakehouse")
            rows = client.query("""
                SELECT customer_id
                FROM gold_feg.gads_tokens
                WHERE active = TRUE AND customer_id IS NOT NULL
            """).result()
            ids = set()
            for r in rows:
                cid = str(r["customer_id"])
                if len(cid) == 10:
                    ids.add(f"{cid[:3]}-{cid[3:6]}-{cid[6:]}")
            return ids
        except Exception:
            pass
    # fallback para arquivo local
    if not os.path.exists(ARQUIVO_FEGSYS_DONE):
        return set()
    with open(ARQUIVO_FEGSYS_DONE, encoding="utf-8") as f:
        return set(json.load(f))

def sincronizar_status_bigquery() -> tuple[int, str | None]:
    """Atualiza account_details.json usando gads_tokens do BigQuery como fonte de status."""
    if not _BQ_AVAILABLE or not os.path.exists(ARQUIVO_CREDENTIALS):
        return 0, "BigQuery não disponível"
    try:
        creds = service_account.Credentials.from_service_account_file(
            ARQUIVO_CREDENTIALS,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        client = bigquery.Client(credentials=creds, project="grupofeg-lakehouse")
        rows = list(client.query("""
            SELECT customer_id, active
            FROM gold_feg.gads_tokens
            WHERE customer_id IS NOT NULL
        """).result())

        detalhes = carregar_detalhes()
        atualizados = 0
        for row in rows:
            cid = str(row["customer_id"])
            if len(cid) != 10:
                continue
            formatted = f"{cid[:3]}-{cid[3:6]}-{cid[6:]}"
            novo_status = "ENABLED" if row["active"] else "SUSPENDED"
            existente = detalhes.get(formatted, {}).get("status_conta")
            if existente != novo_status:
                if formatted not in detalhes:
                    detalhes[formatted] = {}
                detalhes[formatted]["status_conta"] = novo_status
                atualizados += 1

        salvar_detalhes(detalhes)
        return atualizados, None
    except Exception as e:
        return 0, str(e)


def _ler_ultimo_serial() -> int:
    if os.path.exists(ARQUIVO_SERIAL):
        with open(ARQUIVO_SERIAL, encoding="utf-8") as f:
            return json.load(f).get("last_serial", 0)
    return 0

def _salvar_ultimo_serial(serial: int):
    with open(ARQUIVO_SERIAL, "w", encoding="utf-8") as f:
        json.dump({"last_serial": serial}, f)

def _sync_novas_contas_redtrack() -> int:
    """Verifica Redtrack por contas com serial > last_serial e adiciona ao arquivo."""
    import re
    ultimo = _ler_ultimo_serial()
    API_KEY = os.getenv("REDTRACK_API_KEY")
    BASE = "https://api.redtrack.io"

    existentes = set()
    with open(ARQUIVO_CONTAS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            for prefix in ["✅", "⏳", "🚫"]:
                if line.startswith(prefix):
                    line = line[1:].strip()
                    break
            acc_id = line.split(" | ")[0].strip()
            existentes.add(acc_id)

    novos = []
    maior_serial = ultimo
    page = 1
    try:
        while True:
            r = requests.get(f"{BASE}/sources",
                             params={"api_key": API_KEY, "limit": 100, "page": page},
                             timeout=15)
            items = r.json() if isinstance(r.json(), list) else r.json().get("items", [])
            if not items:
                break
            for s in items:
                serial = s.get("serial_number", 0)
                if serial > maior_serial:
                    maior_serial = serial
                if serial <= ultimo:
                    continue
                name = s.get("title", "")
                network_id = s.get("network_id", "")
                campaign_pattern = s.get("campaign_pattern", "")
                ids = re.findall(r'\d{3}-\d{3}-\d{4}',
                                 f"{network_id} {campaign_pattern} {name}")
                for acc_id in ids:
                    if acc_id not in existentes:
                        nome_match = re.search(r'\|\s*([^\|]+)$', name)
                        nome = nome_match.group(1).strip() if nome_match else name.split("|")[-1].strip()
                        novos.append((serial, acc_id, nome))
                        existentes.add(acc_id)
                        break
            if len(items) < 100:
                break
            page += 1

        if novos:
            with open(ARQUIVO_CONTAS, "a", encoding="utf-8") as f:
                for _, acc_id, nome in sorted(novos):
                    f.write(f"   {acc_id} | {nome}\n")

        if maior_serial > ultimo:
            _salvar_ultimo_serial(maior_serial)

        return len(novos)
    except Exception:
        return 0

st.set_page_config(page_title="Dashboard Google Ads", layout="wide")

def carregar():
    with open(ARQUIVO_CONTESTACOES, encoding="utf-8") as f:
        return json.load(f)

def salvar(dados):
    with open(ARQUIVO_CONTESTACOES, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)

def badge(resultado):
    if resultado == "aprovado":
        return "✅ Aprovado"
    elif resultado == "reprovado":
        return "❌ Reprovado"
    return "⏳ Pendente"

def carregar_detalhes():
    if not os.path.exists(ARQUIVO_DETALHES):
        return {}
    with open(ARQUIVO_DETALHES, encoding="utf-8") as f:
        return json.load(f)

def salvar_detalhes(detalhes):
    with open(ARQUIVO_DETALHES, "w", encoding="utf-8") as f:
        json.dump(detalhes, f, ensure_ascii=False, indent=2)

def carregar_contas():
    detalhes   = carregar_detalhes()
    timestamps = carregar_timestamps()
    contas = []
    with open(ARQUIVO_CONTAS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("✅"):
                status = "✅ Aceito"
                line = line[1:].strip()
            elif line.startswith("⏳"):
                status = "⏳ Pendente"
                line = line[1:].strip()
            elif line.startswith("🚫"):
                status = "🚫 Suspensa"
                line = line[1:].strip()
            else:
                status = "📤 Não enviado"
            parts = line.split(" | ", 1)
            acc_id = parts[0].strip()
            name = parts[1].strip() if len(parts) > 1 else "?"
            d = detalhes.get(acc_id, {})
            ts = timestamps.get(acc_id)
            data_convite = ""
            if ts:
                try:
                    data_convite = datetime.fromisoformat(ts).strftime("%d/%m/%Y")
                except Exception:
                    pass
            contas.append({
                "id": acc_id,
                "nome": name,
                "status": status,
                "status_conta": d.get("status_conta", ""),
                "moeda": d.get("moeda", ""),
                "data_convite": data_convite,
            })
    # Remove duplicatas mantendo a primeira ocorrência
    vistos = set()
    unicos = []
    for c in contas:
        if c["id"] not in vistos:
            vistos.add(c["id"])
            unicos.append(c)
    return unicos

def salvar_contas(contas):
    with open(ARQUIVO_CONTAS, "w", encoding="utf-8") as f:
        for c in contas:
            if c["status"] == "✅ Aceito":
                prefix = "✅"
            elif c["status"] == "⏳ Pendente":
                prefix = "⏳"
            elif c["status"] == "🚫 Suspensa":
                prefix = "🚫"
            else:
                prefix = "  "
            f.write(f"{prefix} {c['id']} | {c['nome']}\n")

STATUS_CONTA = {
    "ENABLED":   ("🟢 Ativa",      "normal"),
    "SUSPENDED": ("🔴 Suspensa",   "inverse"),
    "CANCELLED": ("⚫ Cancelada",  "off"),
    "CLOSED":    ("⚫ Fechada",    "off"),
}

def sincronizar_com_api(contas):
    try:
        resp = requests.post("https://oauth2.googleapis.com/token", data={
            "grant_type": "refresh_token",
            "refresh_token": os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
            "client_id": os.getenv("GOOGLE_ADS_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
        }, timeout=15)
        token = resp.json()["access_token"]
        MCC_ID = os.getenv("GOOGLE_ADS_MCC_ID", "").replace("-", "")

        headers = {
            "Authorization": f"Bearer {token}",
            "developer-token": os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
            "login-customer-id": MCC_ID,
            "Content-Type": "application/json",
        }
        base_url = f"https://googleads.googleapis.com/v21/customers/{MCC_ID}/googleAds:search"

        # 1. Busca status dos convites
        r = requests.post(base_url, headers=headers, json={"query": """
            SELECT customer_client_link.client_customer, customer_client_link.status
            FROM customer_client_link
            WHERE customer_client_link.status IN (ACTIVE, PENDING)
        """}, timeout=15)
        link_status = {}
        for row in r.json().get("results", []):
            link = row.get("customerClientLink", {})
            raw = link.get("clientCustomer", "").replace("customers/", "")
            formatted = f"{raw[:3]}-{raw[3:6]}-{raw[6:]}"
            link_status[formatted] = link.get("status", "")

        # 2. Busca detalhes das contas vinculadas (ACTIVE)
        r2 = requests.post(base_url, headers=headers, json={"query": """
            SELECT
                customer_client.id,
                customer_client.descriptive_name,
                customer_client.status,
                customer_client.currency_code
            FROM customer_client
            WHERE customer_client.level = 1
        """}, timeout=15)
        account_details = {}
        for row in r2.json().get("results", []):
            cc = row.get("customerClient", {})
            raw = str(cc.get("id", ""))
            if len(raw) >= 7:
                formatted = f"{raw[:3]}-{raw[3:6]}-{raw[6:]}"
                account_details[formatted] = {
                    "nome_google": cc.get("descriptiveName", ""),
                    "status_conta": cc.get("status", "UNKNOWN"),
                    "moeda": cc.get("currencyCode", ""),
                }

        atualizados = 0
        detalhes_persistir = carregar_detalhes()

        for c in contas:
            det = account_details.get(c["id"], {})

            # Preserva status 🚫 Suspensa marcado manualmente — não sobrescreve com API
            if c["status"] == "🚫 Suspensa":
                if det:
                    c["status_conta"] = det.get("status_conta", "")
                    c["moeda"] = det.get("moeda", "")
                    detalhes_persistir[c["id"]] = {
                        "status_conta": det.get("status_conta", ""),
                        "moeda": det.get("moeda", ""),
                    }
                continue

            status_api = link_status.get(c["id"])
            novo_invite = "✅ Aceito" if status_api == "ACTIVE" else ("⏳ Pendente" if status_api == "PENDING" else "📤 Não enviado")

            changed = novo_invite != c["status"]
            c["status"] = novo_invite
            c["status_conta"] = det.get("status_conta", "")
            c["moeda"] = det.get("moeda", "")

            if det:
                detalhes_persistir[c["id"]] = {
                    "status_conta": det.get("status_conta", ""),
                    "moeda": det.get("moeda", ""),
                }
            if changed:
                atualizados += 1

        salvar_detalhes(detalhes_persistir)
        return contas, atualizados, None
    except Exception as e:
        return contas, 0, str(e)



# ── Título ───────────────────────────────────────────────
REDTRACK_API_KEY = os.getenv("REDTRACK_API_KEY")
REDTRACK_BASE = "https://api.redtrack.io"

def rt_get_all_conversions(date_from: str, date_to: str) -> list:
    """Busca todas as conversões do período paginando."""
    all_items, page, limit = [], 1, 500
    while True:
        resp = requests.get(f"{REDTRACK_BASE}/conversions", params={
            "api_key": REDTRACK_API_KEY,
            "date_from": date_from,
            "date_to": date_to,
            "limit": limit,
            "page": page,
        }, timeout=30)
        items = resp.json().get("items", [])
        all_items.extend(items)
        if len(items) < limit:
            break
        page += 1
    return all_items

def rt_extrair_ofertas(conversoes: list) -> dict:
    """Retorna {offer_id: offer_name} únicas encontradas nas conversões."""
    return {c["offer_id"]: c["offer"] for c in conversoes if c.get("offer_id") and c.get("offer")}

def rt_custo_por_hora(date_from: str, date_to: str, conversoes_sel: list, conversoes_todas: list) -> tuple:
    """
    Estima custo para as ofertas selecionadas proporcionalmente.
    Custo_selecionado = custo_total × (conv_selecionadas / conv_totais)
    Distribui por hora pelo peso dos ICs/vendas no track_time.
    Retorna (custo_hora: dict, custo_total_sel: float, proporcao: float)
    """
    # Custo total sem filtro
    r = requests.get(f"{REDTRACK_BASE}/report", params={
        "api_key": REDTRACK_API_KEY,
        "date_from": date_from,
        "date_to": date_to,
    }, timeout=15)
    data = r.json()
    custo_total_geral = float(data[0].get("cost", 0) or 0) if isinstance(data, list) and data else 0

    # Proporção: conversões selecionadas / conversões totais
    total_convs = len(conversoes_todas) or 1
    sel_convs = len(conversoes_sel) or 0
    proporcao = sel_convs / total_convs
    custo_estimado = custo_total_geral * proporcao

    # Distribui por hora pelo peso dos eventos da seleção
    peso_hora = defaultdict(int)
    for c in conversoes_sel:
        try:
            hora = datetime.fromisoformat(c["track_time"]).hour
            peso_hora[hora] += 1
        except Exception:
            pass

    total_peso = sum(peso_hora.values()) or 1
    custo_hora = {h: round(custo_estimado * peso_hora.get(h, 0) / total_peso, 2) for h in range(24)}
    return custo_hora, custo_estimado, proporcao

def rt_agregar_por_hora(conversoes: list, offer_ids: set) -> dict:
    """Agrega por hora do clique (track_time) para as ofertas selecionadas."""
    por_hora = defaultdict(lambda: {"vendas": 0, "ics": 0, "receita": 0.0})
    for c in conversoes:
        if c.get("offer_id") not in offer_ids:
            continue
        try:
            hora = datetime.fromisoformat(c["track_time"]).hour
        except Exception:
            continue
        if c.get("type") == "Purchase":
            por_hora[hora]["vendas"] += 1
            por_hora[hora]["receita"] += float(c.get("payout", 0) or 0)
        elif c.get("type") == "InitiateCheckout":
            por_hora[hora]["ics"] += 1
    return dict(por_hora)


# ── CSS ──────────────────────────────────────────────────
st.markdown("""
<style>
#MainMenu, footer { visibility: hidden; }
.main .block-container {
    padding-top: 2rem !important;
    padding-left: 2.5rem !important;
    padding-right: 2.5rem !important;
    max-width: 100% !important;
}
/* Sidebar */
section[data-testid="stSidebar"] > div:first-child {
    background: #0c0c18 !important;
    padding-top: 1.5rem;
    border-right: 1px solid #1a1a2e;
}
/* Métricas */
[data-testid="stMetric"] {
    background: #13131f;
    border: 1px solid #1e1e30;
    border-radius: 12px;
    padding: 1rem 1.2rem !important;
}
[data-testid="stMetricLabel"] p {
    color: #888 !important;
    font-size: 0.73rem;
    text-transform: uppercase;
    letter-spacing: 0.07em;
}
[data-testid="stMetricValue"] { font-size: 1.9rem !important; font-weight: 700; }
/* Botões */
.stButton > button { border-radius: 8px !important; font-weight: 600 !important; }
.stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #6c63ff, #4a90e2) !important;
    border: none !important;
}
.stButton > button:hover { opacity: 0.82; }
/* Progress */
[data-testid="stProgressBar"] > div > div {
    background: linear-gradient(90deg, #6c63ff, #4ecdc4) !important;
    border-radius: 99px;
}
/* Data editor */
[data-testid="stDataFrameResizable"] {
    border: 1px solid #1e1e30 !important;
    border-radius: 10px;
    overflow: hidden;
}
/* Expanders */
[data-testid="stExpander"] {
    border: 1px solid #1e1e30 !important;
    border-radius: 10px !important;
}
/* Divider */
hr { border-color: #1e1e30 !important; margin: 0.8rem 0 !important; }
/* Inputs */
[data-testid="stTextInput"] > div > div > input {
    border-radius: 8px !important;
    background: #13131f !important;
}
</style>
""", unsafe_allow_html=True)

# ── Sidebar ───────────────────────────────────────────────
with st.sidebar:
    st.markdown("### 📊 Google Ads")
    st.caption("Dashboard de controle")
    st.divider()
    pagina = st.radio(
        "nav",
        ["📋 Contestações", "📈 Click Time", "🔑 FEG Tracker", "⚡ Agente Dev"],
        label_visibility="collapsed",
    )


# ════════════════════════════════════════════════════════
# PÁGINA 1 — CONTESTAÇÕES
# ════════════════════════════════════════════════════════
if pagina == "📋 Contestações":
    dados = carregar()
    historico = dados["historico"]

    st.subheader("Histórico de Contestações")
    st.caption("Acompanhe o resultado de cada contestação e aprenda o que funciona")

    total = len(historico)
    aprovados = sum(1 for c in historico if c["resultado"] == "aprovado")
    reprovados = sum(1 for c in historico if c["resultado"] == "reprovado")
    pendentes = sum(1 for c in historico if not c["resultado"])
    taxa = round(aprovados / (aprovados + reprovados) * 100) if (aprovados + reprovados) > 0 else 0

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total", total)
    col2.metric("✅ Aprovados", aprovados)
    col3.metric("❌ Reprovados", reprovados)
    col4.metric("⏳ Pendentes", pendentes)
    col5.metric("Taxa de Aprovação", f"{taxa}%")

    st.divider()

    col_f1, col_f2 = st.columns(2)
    with col_f1:
        politicas = ["Todas"] + sorted(set(c["tipo_politica"] for c in historico))
        filtro_politica = st.selectbox("Filtrar por política", politicas)
    with col_f2:
        filtro_resultado = st.selectbox("Filtrar por resultado", ["Todos", "✅ Aprovado", "❌ Reprovado", "⏳ Pendente"])

    resultado_map = {"Todos": None, "✅ Aprovado": "aprovado", "❌ Reprovado": "reprovado", "⏳ Pendente": ""}
    filtrado = [
        c for c in historico
        if (filtro_politica == "Todas" or c["tipo_politica"] == filtro_politica)
        and (resultado_map[filtro_resultado] is None or (c["resultado"] or "") == (resultado_map[filtro_resultado] or ""))
    ]

    st.markdown(f"**{len(filtrado)} contestação(ões) encontrada(s)**")
    st.divider()

    for c in reversed(filtrado):
        resultado = c.get("resultado") or ""
        with st.expander(f"#{c['id']} — {c['tipo_politica']}  |  {badge(resultado)}  |  {c['data']}"):
            col_a, col_b = st.columns([2, 1])

            with col_a:
                st.markdown("**Argumento principal:**")
                st.info(c["argumento_principal"])
                st.markdown("**Texto completo:**")
                st.text_area("", c["texto"], height=220, key=f"texto_{c['id']}", disabled=True)

            with col_b:
                st.markdown("**Observações:**")
                st.write(c.get("observacoes") or "—")

                if c.get("contas_testadas"):
                    st.markdown("**Contas testadas:**")
                    for conta in c["contas_testadas"]:
                        st.write(f"• {conta}")

                st.markdown("**Atualizar resultado:**")
                novo_resultado = st.radio(
                    "",
                    ["pendente", "aprovado", "reprovado"],
                    index=["pendente", "aprovado", "reprovado"].index(resultado or "pendente"),
                    key=f"radio_{c['id']}",
                    horizontal=True,
                )
                nova_obs = st.text_input("Observação", value=c.get("observacoes") or "", key=f"obs_{c['id']}")
                nova_conta = st.text_input("Adicionar conta testada (opcional)", key=f"conta_{c['id']}")

                if st.button("💾 Salvar", key=f"salvar_{c['id']}"):
                    for item in dados["historico"]:
                        if item["id"] == c["id"]:
                            item["resultado"] = novo_resultado if novo_resultado != "pendente" else None
                            item["observacoes"] = nova_obs
                            if nova_conta and nova_conta not in item["contas_testadas"]:
                                item["contas_testadas"].append(nova_conta)
                    arg = item["argumento_principal"]
                    if novo_resultado == "aprovado" and arg not in dados["padroes_aprovados"]:
                        dados["padroes_aprovados"].append(arg)
                        dados["padroes_reprovados"] = [p for p in dados["padroes_reprovados"] if p != arg]
                    elif novo_resultado == "reprovado" and arg not in dados["padroes_reprovados"]:
                        dados["padroes_reprovados"].append(arg)
                        dados["padroes_aprovados"] = [p for p in dados["padroes_aprovados"] if p != arg]
                    salvar(dados)
                    st.success("Salvo!")
                    st.rerun()

    st.divider()

    with st.expander("➕ Adicionar nova contestação"):
        novo_tipo = st.text_input("Tipo de política")
        novo_argumento = st.text_input("Argumento principal")
        novo_texto = st.text_area("Texto completo", height=200)
        nova_data = st.date_input("Data", value=date.today())

        if st.button("Adicionar"):
            if novo_tipo and novo_texto:
                novo_id = max(c["id"] for c in dados["historico"]) + 1
                dados["historico"].append({
                    "id": novo_id,
                    "data": str(nova_data),
                    "tipo_politica": novo_tipo,
                    "argumento_principal": novo_argumento,
                    "texto": novo_texto,
                    "resultado": None,
                    "contas_testadas": [],
                    "observacoes": "Pendente de resultado",
                })
                salvar(dados)
                st.success(f"Contestação #{novo_id} adicionada!")
                st.rerun()
            else:
                st.warning("Preencha pelo menos o tipo de política e o texto.")

    with st.expander("🧠 Padrões aprendidos"):
        col_ap, col_rep = st.columns(2)
        with col_ap:
            st.markdown("**✅ Argumentos que funcionaram:**")
            if dados["padroes_aprovados"]:
                for p in dados["padroes_aprovados"]:
                    st.success(p)
            else:
                st.write("Nenhum ainda.")
        with col_rep:
            st.markdown("**❌ Argumentos que falharam:**")
            if dados["padroes_reprovados"]:
                for p in dados["padroes_reprovados"]:
                    st.error(p)
            else:
                st.write("Nenhum ainda.")



# ════════════════════════════════════════════════════════
# PÁGINA 3 — CLICK TIME
# ════════════════════════════════════════════════════════
elif pagina == "📈 Click Time":
    st.subheader("ROI por Hora do Clique")
    st.caption("Receita e custo agrupados pelo horário em que o clique aconteceu (não a compra)")

    # ── Botões de período rápido ─────────────────────────
    hoje = date.today()
    periodos = {
        "Hoje":        (hoje,                    hoje),
        "Ontem":       (hoje - timedelta(days=1), hoje - timedelta(days=1)),
        "7 dias":      (hoje - timedelta(days=7), hoje - timedelta(days=1)),
        "14 dias":     (hoje - timedelta(days=14),hoje - timedelta(days=1)),
        "Mês atual":   (hoje.replace(day=1),      hoje - timedelta(days=1)),
        "Personalizado": None,
    }

    if "ct_periodo_sel" not in st.session_state:
        st.session_state["ct_periodo_sel"] = "Ontem"

    # Botões em linha
    cols_periodo = st.columns(len(periodos))
    for i, (label, _) in enumerate(periodos.items()):
        ativo = st.session_state["ct_periodo_sel"] == label
        with cols_periodo[i]:
            if st.button(
                label,
                key=f"ct_per_{label}",
                type="primary" if ativo else "secondary",
                use_container_width=True,
            ):
                st.session_state["ct_periodo_sel"] = label
                st.rerun()

    periodo_sel = st.session_state["ct_periodo_sel"]

    # Datas: automáticas ou personalizadas
    if periodo_sel == "Personalizado":
        col_d1, col_d2 = st.columns(2)
        with col_d1:
            ct_de  = st.date_input("De",  value=hoje - timedelta(days=30), key="ct_de")
        with col_d2:
            ct_ate = st.date_input("Até", value=hoje - timedelta(days=1),  key="ct_ate")
    else:
        ct_de, ct_ate = periodos[periodo_sel]
        st.caption(f"Período: **{ct_de.strftime('%d/%m/%Y')}** até **{ct_ate.strftime('%d/%m/%Y')}**")

    # ── Auto-carrega ofertas na 1ª visita (últimos 90 dias) ─
    if "ct_todas_ofertas" not in st.session_state:
        with st.spinner("Carregando ofertas disponíveis..."):
            _d_fim   = hoje - timedelta(days=1)
            _d_ini   = hoje - timedelta(days=90)
            _convs   = rt_get_all_conversions(str(_d_ini), str(_d_fim))
            st.session_state["ct_todas_ofertas"] = rt_extrair_ofertas(_convs)

    st.divider()

    # ── Linha de ação ─────────────────────────────────────
    col_oferta, col_receita, col_gasto, col_gerar, col_reload = st.columns([2.5, 1.5, 1.5, 1.2, 0.6])

    ofertas_map  = st.session_state.get("ct_todas_ofertas", {})
    nomes_sorted = sorted(set(ofertas_map.values())) if ofertas_map else []
    ja_selecionados = st.session_state.get("ct_sel_acumulado", [])

    with col_reload:
        st.write("")
        if st.button("🔄", key="ct_reload", help="Recarregar lista de ofertas"):
            del st.session_state["ct_todas_ofertas"]
            st.session_state.pop("ct_conversoes", None)
            st.rerun()

    with col_oferta:
        with st.popover(
            f"🔍 Filtrar por Oferta  {'· ' + str(len(ja_selecionados)) + ' selecionada(s)' if ja_selecionados else ''}",
            use_container_width=True,
        ):
            if not nomes_sorted:
                st.caption("Aguarde o carregamento automático das ofertas.")
            else:
                busca_pop = st.text_input("Buscar", placeholder="Nome da oferta...", key="ct_busca_pop")
                filtrados_pop = [n for n in nomes_sorted if not busca_pop or busca_pop.lower() in n.lower()]

                if st.button("Selecionar todos", key="ct_sel_todos", use_container_width=True):
                    st.session_state["ct_sel_acumulado"] = filtrados_pop
                    st.rerun()
                if st.button("Limpar seleção", key="ct_limpar", use_container_width=True):
                    st.session_state["ct_sel_acumulado"] = []
                    st.rerun()

                st.divider()
                novos = list(ja_selecionados)
                for nome in filtrados_pop:
                    checked = nome in novos
                    if st.checkbox(nome, value=checked, key=f"chk_{nome}"):
                        if nome not in novos:
                            novos.append(nome)
                    else:
                        if nome in novos:
                            novos.remove(nome)
                st.session_state["ct_sel_acumulado"] = novos
                ja_selecionados = novos

    selecionadas = ja_selecionados

    with col_receita:
        receita_manual = st.number_input(
            "💰 Receita real (USD)",
            min_value=0.0, value=st.session_state.get("ct_receita_manual", 0.0),
            step=100.0, key="ct_receita_manual",
            help="Valor real da PagAmerican. Se 0, usa o Redtrack.",
        )

    with col_gasto:
        gasto_manual = st.number_input(
            "💸 Gasto real (USD)",
            min_value=0.0, value=st.session_state.get("ct_gasto_manual", 0.0),
            step=100.0, key="ct_gasto_manual",
            help="Total gasto no Google Ads no período.",
        )

    with col_gerar:
        st.write("")
        gerar = st.button(
            "📊 Gerar",
            use_container_width=True, key="ct_gerar",
            type="primary", disabled=not selecionadas,
        )

    if selecionadas:
        st.caption(f"✅ {len(selecionadas)} oferta(s) selecionada(s): {', '.join(selecionadas[:3])}{'...' if len(selecionadas) > 3 else ''}")

    if "ct_ofertas_map" not in st.session_state or not st.session_state["ct_ofertas_map"]:
        st.info("Selecione um período e clique em **📥 Carregar ofertas** para começar.")

        if gerar and selecionadas:
            offer_ids_sel = {oid for oid, nome in ofertas_map.items() if nome in selecionadas}
            with st.spinner(f"Buscando conversões de {ct_de} a {ct_ate}..."):
                conversoes = rt_get_all_conversions(str(ct_de), str(ct_ate))
            convs_sel = [c for c in conversoes if c.get("offer_id") in offer_ids_sel]
            por_hora  = rt_agregar_por_hora(conversoes, offer_ids_sel)

            # Peso por hora (ICs + vendas) para distribuir custo e escalar receita
            peso_hora = defaultdict(int)
            for c in convs_sel:
                try:
                    peso_hora[datetime.fromisoformat(c["track_time"]).hour] += 1
                except Exception:
                    pass
            total_peso = sum(peso_hora.values()) or 1

            # Custo: distribui gasto real pelo peso de eventos por hora
            gasto_total = gasto_manual if gasto_manual > 0 else 0
            custo_hora  = {h: round(gasto_total * peso_hora.get(h, 0) / total_peso, 2) for h in range(24)}

            horas    = list(range(24))
            # Receita: se informada manualmente, escala a distribuição do Redtrack para o total real
            receitas_rt = [por_hora.get(h, {}).get("receita", 0) for h in horas]
            if receita_manual > 0:
                total_rt = sum(receitas_rt) or 1
                receitas = [round(r * receita_manual / total_rt, 2) for r in receitas_rt]
            else:
                receitas = receitas_rt
            custos   = [custo_hora.get(h, 0) for h in horas]
            vendas   = [por_hora.get(h, {}).get("vendas", 0) for h in horas]
            ics      = [por_hora.get(h, {}).get("ics", 0) for h in horas]
            rois     = [round(receitas[h] / custos[h], 2) if custos[h] > 0 else 0 for h in horas]

            total_receita = sum(receitas)
            total_custo   = sum(custos)
            total_vendas  = sum(vendas)
            total_ics     = sum(ics)
            roi_total     = round(total_receita / total_custo, 2) if total_custo > 0 else 0

            mc1, mc2, mc3, mc4, mc5 = st.columns(5)
            mc1.metric("💰 Receita", f"${total_receita:,.0f}")
            mc2.metric("💸 Custo",   f"${total_custo:,.0f}")
            mc3.metric("📈 ROI",     f"{roi_total}x")
            mc4.metric("🛒 Vendas",  total_vendas)
            mc5.metric("🔄 ICs",     total_ics)
            st.divider()

            xs = [f"{h:02d}" for h in horas]
            all_rois = [r for r in rois if r > 0]
            roi_max  = max(all_rois) * 1.8 if all_rois else 3   # mais espaço acima para labels
            roi_min  = min(all_rois) * 0.3 if all_rois else 0   # mais espaço abaixo para labels ruins
            max_bar  = max(max(receitas, default=1), max(custos, default=1)) or 1

            fig = make_subplots(
                rows=3, cols=1,
                row_heights=[0.22, 0.60, 0.18],
                shared_xaxes=True,
                vertical_spacing=0.02,
            )

            # ── Uma única linha de ROI com segmentos coloridos ──
            dot_colors = ["#1abc9c" if rois[h] >= 1.0 else "#e07b6a" for h in horas]

            # Segmentos coloridos (23 traces de linha)
            for i in range(len(horas) - 1):
                h1, h2 = horas[i], horas[i + 1]
                r1, r2 = rois[h1], rois[h2]
                seg_color = "#1abc9c" if (r1 >= 1.0 and r2 >= 1.0) else "#e07b6a"
                fig.add_trace(go.Scatter(
                    x=[xs[h1], xs[h2]], y=[r1, r2],
                    mode="lines",
                    line=dict(color=seg_color, width=2),
                    showlegend=False, hoverinfo="skip",
                ), row=1, col=1)

            # Pontos e labels (cliponaxis=False evita corte nas bordas)
            fig.add_trace(go.Scatter(
                x=xs, y=rois,
                mode="markers+text",
                marker=dict(
                    color=dot_colors, size=9,
                    line=dict(color="#0e1117", width=1.5),
                ),
                text=[f"{r}x" if r > 0 else "" for r in rois],
                textposition=["top center" if rois[h] >= 1.0 else "bottom center" for h in horas],
                textfont=dict(size=9, color=dot_colors),
                showlegend=False,
                cliponaxis=False,
                hovertemplate="%{x}h: %{y}x<extra></extra>",
            ), row=1, col=1)

            # ── Barras lado a lado ────────────────────────
            fig.add_trace(go.Bar(
                x=xs, y=receitas, name="Receita",
                marker_color="#4ecb8d",
            ), row=2, col=1)
            fig.add_trace(go.Bar(
                x=xs, y=custos, name="Custo",
                marker_color="#e07b6a",
            ), row=2, col=1)

            # ── Círculos de Vendas e ICs (subplot 3) ─────
            fig.add_trace(go.Scatter(
                x=xs, y=[1.5] * 24,
                mode="markers+text",
                marker=dict(symbol="circle", size=28, color="#4ecb8d"),
                text=[str(v) for v in vendas],
                textfont=dict(color="white", size=10, family="Arial Black"),
                textposition="middle center",
                showlegend=False, hoverinfo="skip",
            ), row=3, col=1)
            fig.add_trace(go.Scatter(
                x=xs, y=[0.5] * 24,
                mode="markers+text",
                marker=dict(symbol="circle", size=28, color="#e07b6a"),
                text=[str(i) for i in ics],
                textfont=dict(color="white", size=10, family="Arial Black"),
                textposition="middle center",
                showlegend=False, hoverinfo="skip",
            ), row=3, col=1)

            fig.update_layout(
                barmode="group",
                bargap=0.2,
                bargroupgap=0.0,
                height=560,
                margin=dict(t=30, b=10, l=50, r=20),
                # ROI
                yaxis=dict(range=[roi_min, roi_max], showgrid=False,
                           zeroline=False, showticklabels=False),
                xaxis=dict(tickmode="array", tickvals=xs, ticktext=xs,
                           showgrid=False, zeroline=False, showticklabels=False),
                # Barras
                yaxis2=dict(range=[0, max_bar * 1.08], showgrid=True,
                            gridcolor="#2a2a2a", zeroline=False),
                xaxis2=dict(tickmode="array", tickvals=xs, ticktext=xs,
                            showgrid=False, zeroline=False, showticklabels=False),
                # Círculos
                yaxis3=dict(range=[0, 2], showgrid=False, zeroline=False,
                            showticklabels=False),
                xaxis3=dict(tickmode="array", tickvals=xs, ticktext=xs,
                            showgrid=False, zeroline=False),
                legend=dict(orientation="h", y=1.04, x=0),
                plot_bgcolor="#0e1117",
                paper_bgcolor="#0e1117",
                font=dict(color="white", size=11),
            )
            st.plotly_chart(fig, use_container_width=True)


# ════════════════════════════════════════════════════════
# PÁGINA 4 — FEG TRACKER
# ════════════════════════════════════════════════════════
elif pagina == "🔑 FEG Tracker":
    _novas = _sync_novas_contas_redtrack()
    if _novas > 0:
        st.toast(f"✅ {_novas} nova(s) conta(s) adicionada(s) do RedTrack!", icon="🔗")

    col_titulo_feg, col_btn1, col_btn2 = st.columns([3, 1, 1])
    with col_titulo_feg:
        st.subheader("Contas com Token FEG Conectado")
        st.caption("Contas que concluíram a autorização OAuth no tracker.fegsys.com")
    with col_btn1:
        st.write("")
        st.write("")
        if st.button("🔄 Atualizar tokens", use_container_width=True):
            carregar_fegsys_done.clear()
            st.rerun()
    with col_btn2:
        st.write("")
        st.write("")
        if st.button("🟢 Sincronizar status", use_container_width=True):
            with st.spinner("Consultando BigQuery..."):
                n, erro = sincronizar_status_bigquery()
            if erro:
                st.error(f"Erro: {erro}")
            else:
                st.success(f"{n} status atualizado(s)!")
                st.rerun()

    fegsys_ids = carregar_fegsys_done()
    contas_todas = carregar_contas()

    # Total elegível: ENABLED + sem status (novas ainda não sincronizadas)
    total_elegivel = sum(1 for c in contas_todas if c.get("status_conta") in ("ENABLED", ""))
    total_conectadas = sum(1 for acc_id in fegsys_ids
                          if any(c["id"] == acc_id and c.get("status_conta") == "ENABLED"
                                 for c in contas_todas))
    progresso_feg = round(total_conectadas / total_elegivel * 100) if total_elegivel > 0 else 0

    # Mapa id → nome
    id_para_nome = {c["id"]: c["nome"] for c in contas_todas}

    col1, col2, col3 = st.columns(3)
    col1.metric("🔑 Com token conectado", total_conectadas)
    col2.metric("🟢 Contas ativas", total_elegivel)
    col3.metric("📊 Progresso", f"{progresso_feg}%")

    st.progress(progresso_feg / 100)
    st.divider()

    # Filtro de busca
    busca_feg = st.text_input("Buscar por ID ou nome", key="busca_feg")

    # Monta tabela
    rows_feg = []
    for acc_id in sorted(fegsys_ids):
        nome = id_para_nome.get(acc_id, "—")
        if busca_feg and busca_feg.lower() not in acc_id.lower() and busca_feg.lower() not in nome.lower():
            continue
        rows_feg.append({"ID": acc_id, "Nome": nome})

    st.markdown(f"**{len(rows_feg)} conta(s) encontrada(s)**")
    st.divider()

    if rows_feg:
        df_feg = pd.DataFrame(rows_feg)
        st.dataframe(
            df_feg,
            column_config={
                "ID":   st.column_config.TextColumn("ID",   width="medium"),
                "Nome": st.column_config.TextColumn("Nome", width="large"),
            },
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.info("Nenhuma conta encontrada com o filtro atual." if busca_feg else "Nenhuma conta com token FEG conectado ainda.")

    # Contas SEM token: ENABLED + novas sem status; exclui suspensas (por status_conta ou prefixo 🚫)
    sem_token = [
        c for c in contas_todas
        if c.get("status_conta") in ("ENABLED", "")
        and c.get("status_conta") != "SUSPENDED"
        and c.get("status") != "🚫 Suspensa"
        and c["id"] not in fegsys_ids
    ]
    with st.expander(f"⏳ Contas ainda sem token ({len(sem_token)})"):
        if sem_token:
            df_sem = pd.DataFrame([{"ID": c["id"], "Nome": c["nome"], "Convite": c["status"]} for c in sem_token])
            st.dataframe(
                df_sem,
                column_config={
                    "ID":      st.column_config.TextColumn("ID",      width="medium"),
                    "Nome":    st.column_config.TextColumn("Nome",    width="large"),
                    "Convite": st.column_config.TextColumn("Convite", width="medium"),
                },
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.success("Todas as contas elegíveis já têm token conectado!")


# ════════════════════════════════════════════════════════
# PÁGINA 4 — AGENTE DEV
# ════════════════════════════════════════════════════════
elif pagina == "⚡ Agente Dev":
    st.subheader("⚡ Agente Dev")
    st.caption("Automação de setup para novas contas")
    st.divider()

    _offer_configs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "offer_configs.json")
    _templates_dir      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
    _available_ofertas  = ["BrainMary"]
    _offer_cfg_all = {}
    if os.path.exists(_offer_configs_path):
        import json as _json
        with open(_offer_configs_path, encoding="utf-8") as _f:
            _offer_cfg_all = _json.load(_f)
        _available_ofertas = list(_offer_cfg_all.keys())

    oferta = st.selectbox("Oferta", _available_ofertas)

    # Gestor — sempre visível; para JellyFill também define qual offer_id usar
    _gestor = st.selectbox("Gestor", ["GH", "AN"])

    # ZIP do template
    _zip_path = os.path.join(_templates_dir, f"{oferta}.zip")
    _zip_exists = os.path.exists(_zip_path)
    if _zip_exists:
        _col_badge, _col_sub = st.columns([4, 1])
        with _col_badge:
            st.success(f"✓ Template ZIP carregado: **{oferta}.zip**")
        with _col_sub:
            _substituir = st.checkbox("Substituir", key=f"sub_zip_{oferta}")
    else:
        _substituir = True
        st.warning(f"Nenhum template ZIP para **{oferta}**. Faça upload abaixo.")

    if not _zip_exists or _substituir:
        _uploaded = st.file_uploader(f"Upload do ZIP template ({oferta})", type=["zip"], key=f"zip_{oferta}")
        if _uploaded:
            os.makedirs(_templates_dir, exist_ok=True)
            with open(_zip_path, "wb") as _zf:
                _zf.write(_uploaded.read())
            st.success(f"✓ ZIP salvo em templates/{oferta}.zip")
            st.rerun()

    col1, col2, col3 = st.columns(3)
    with col1:
        _lc      = st.text_input("Nome da conta", placeholder="LC160")
    with col2:
        _conta   = st.text_input("Conta Google Ads", placeholder="123-456-7890")
    with col3:
        _dominio = st.text_input("Domínio", placeholder="mynewdomain.online")

    st.markdown("")
    oc1, oc2, oc3, oc4, oc5 = st.columns(5)
    with oc1: _dry_run       = st.checkbox("Dry-run (simular)")
    with oc2: _skip_redtrack = st.checkbox("Pular RedTrack")
    with oc3: _skip_vturb    = st.checkbox("Pular Vturb")
    with oc4: _use_adspect   = st.checkbox("Usar Adspect")
    with oc5: _skip_ftp      = st.checkbox("Pular upload")

    # Slugs de players desta oferta (ex: ["vsl01","micro01"] ou ["vsl01","vsl02","vsl03"])
    _offer_slugs = list((_json.load(open(_offer_configs_path, encoding="utf-8")) if os.path.exists(_offer_configs_path) else {}).get(oferta, {}).get("vturb_templates", {}).keys()) if os.path.exists(_offer_configs_path) else ["vsl01", "micro01"]

    _campaign_id  = ""
    _player_inputs = {}
    if _skip_redtrack or _skip_vturb:
        st.markdown("")
        _extra_cols = st.columns(1 + len(_offer_slugs)) if _skip_vturb else st.columns(2)
        _col_idx = 0
        if _skip_redtrack:
            _campaign_id = _extra_cols[_col_idx].text_input("Campaign ID (RedTrack)", placeholder="Cole o ID da campanha")
            _col_idx += 1
        if _skip_vturb:
            for _slug in _offer_slugs:
                _player_inputs[_slug] = _extra_cols[_col_idx].text_input(
                    f"Player ID {_slug} (Vturb)", placeholder=f"Cole o player ID {_slug}"
                )
                _col_idx += 1

    st.markdown("")
    _run = st.button("▶  Executar Setup", use_container_width=True)

    if _run:
        if not all([_lc.strip(), _conta.strip(), _dominio.strip()]):
            st.error("Preencha LC, Conta e Domínio antes de executar.")
            st.stop()
        if _skip_redtrack and not _campaign_id.strip():
            st.error("Campaign ID obrigatório quando RedTrack está pulado.")
            st.stop()
        if _skip_vturb and not all(v.strip() for v in _player_inputs.values()):
            st.error("Todos os Player IDs do Vturb são obrigatórios quando Vturb está pulado.")
            st.stop()

        _lc      = _lc.strip().upper()
        _conta   = _conta.strip()
        _dominio = _dominio.strip().lower()

        _script_dir  = os.path.dirname(os.path.abspath(__file__))
        _script_path = os.path.join(_script_dir, "brainmary_setup.py")
        _venv_python = os.path.join(_script_dir, "venv", "bin", "python3")
        _python_bin  = _venv_python if os.path.exists(_venv_python) else sys.executable

        _cmd = [_python_bin, _script_path,
                f"--lc={_lc}", f"--conta={_conta}", f"--dominio={_dominio}",
                f"--oferta={oferta}"]
        if _dry_run:            _cmd.append("--dry-run")
        if _skip_redtrack:      _cmd.append("--skip-redtrack")
        if _skip_vturb:         _cmd.append("--skip-vturb")
        if not _use_adspect:    _cmd.append("--skip-adspect")
        if _skip_ftp:           _cmd.append("--skip-ftp")
        if _gestor:             _cmd.append(f"--gestor={_gestor}")
        if _campaign_id.strip(): _cmd.append(f"--campaign-id={_campaign_id.strip()}")
        try:
            _wh_url = st.secrets["VTURB_WEBHOOK_URL"]
            _wh_tok = st.secrets.get("VTURB_WEBHOOK_TOKEN", "vturb-secret-2026")
            if _wh_url:
                _cmd.append(f"--webhook-url={_wh_url}")
                _cmd.append(f"--webhook-token={_wh_tok}")
        except Exception:
            pass
        for _slug, _pid in _player_inputs.items():
            if _pid.strip():
                _cmd.append(f"--player-{_slug}={_pid.strip()}")

        def _color(line):
            l = line.rstrip()
            if not l: return ""
            if l.startswith("=="): return f"<span style='color:#818cf8;font-weight:700'>{l}</span>"
            if any(x in l for x in ["✓", " OK", "Pronto", "CONCLUÍDO", "Upload concluído"]):
                return f"<span style='color:#4ade80'>{l}</span>"
            if any(x in l for x in ["ERRO", "Error", "Traceback", "falhou"]):
                return f"<span style='color:#f87171'>{l}</span>"
            if l.startswith("["): return f"<span style='color:#93c5fd'>{l}</span>"
            return f"<span style='color:#c8cce0'>{l}</span>"

        st.markdown("**Log de execução**")
        _log_area   = st.empty()
        _raw_lines  = []
        _html_lines = []

        _env = os.environ.copy()
        try:
            _env["VTURB_WEBHOOK_URL"]   = st.secrets["VTURB_WEBHOOK_URL"]
            _env["VTURB_WEBHOOK_TOKEN"] = st.secrets.get("VTURB_WEBHOOK_TOKEN", "vturb-secret-2026")
        except Exception:
            pass
        _proc = subprocess.Popen(
            _cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=_script_dir, env=_env,
        )
        for _line in _proc.stdout:
            _raw_lines.append(_line.rstrip())
            _html_lines.append(_color(_line))
            _log_area.markdown(
                "<div style='background:#0d0f18;border:1px solid #2d3148;border-radius:8px;"
                "padding:1rem;font-family:monospace;font-size:0.78rem;line-height:1.7;"
                "max-height:420px;overflow-y:auto;white-space:pre-wrap'>"
                + "<br>".join(_html_lines) + "</div>",
                unsafe_allow_html=True,
            )
        _proc.wait()
        _ok = _proc.returncode == 0

        st.markdown("")
        _full = "\n".join(_raw_lines)
        st.markdown(
            "**Resultado** &nbsp; " + (
                "<span style='background:#14532d;color:#4ade80;padding:2px 10px;border-radius:20px;font-size:0.75rem'>✓ Concluído</span>"
                if _ok else
                "<span style='background:#450a0a;color:#f87171;padding:2px 10px;border-radius:20px;font-size:0.75rem'>✗ Erro</span>"
            ),
            unsafe_allow_html=True,
        )

        if _ok:
            _rows = [("LC", _lc), ("Conta", _conta), ("Domínio", f"fg.{_dominio}")]
            _m = re.search(r"Campaign ID\s*:\s*(\S+)", _full)
            if _m: _rows.append(("Campaign ID", _m.group(1).strip()))
            for _m2 in re.finditer(r"Vturb (\S+)\s*:\s*(\S+)", _full):
                _rows.append((f"Vturb {_m2.group(1)}", _m2.group(2).strip()))
            _m3 = re.search(r"Páginas\s*:\s*(.+)", _full)
            if _m3: _rows.append(("Páginas", _m3.group(1).strip()))

            _tbl = "".join(
                f"<tr><td style='color:#818cf8;font-weight:600;width:130px;padding:4px 8px'>{k}</td>"
                f"<td style='padding:4px 8px'><code style='color:#e2e8f0;background:#0d0f18;"
                f"padding:1px 6px;border-radius:4px'>{v}</code></td></tr>"
                for k, v in _rows
            )
            st.markdown(
                f"<div style='background:#1a1d27;border:1px solid #2d3148;border-radius:10px;"
                f"padding:1.2rem 1.5rem'><table style='width:100%;border-collapse:collapse'>"
                f"{_tbl}</table></div>",
                unsafe_allow_html=True,
            )

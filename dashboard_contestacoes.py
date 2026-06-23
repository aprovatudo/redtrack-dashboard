import json
import os
import time
from datetime import date, datetime, timedelta
from collections import defaultdict
import pandas as pd
import streamlit as st
import requests
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

ARQUIVO_CONTESTACOES = os.path.join(os.path.dirname(__file__), "contestacoes.json")
ARQUIVO_CONTAS   = os.path.join(os.path.dirname(__file__), "accounts_to_invite.txt")
ARQUIVO_DETALHES = os.path.join(os.path.dirname(__file__), "account_details.json")
ARQUIVO_SERIAL      = os.path.join(os.path.dirname(__file__), "last_serial.json")
ARQUIVO_TIMESTAMPS  = os.path.join(os.path.dirname(__file__), "invite_timestamps.json")

def carregar_timestamps() -> dict:
    if not os.path.exists(ARQUIVO_TIMESTAMPS):
        return {}
    with open(ARQUIVO_TIMESTAMPS, encoding="utf-8") as f:
        return json.load(f)

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

def buscar_gastos_mcc(contas: list, date_from: str, date_to: str) -> dict:
    """Retorna {acc_id: custo_usd} para contas Aceitas via Google Ads API."""
    try:
        resp = requests.post("https://oauth2.googleapis.com/token", data={
            "grant_type":    "refresh_token",
            "refresh_token": os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
            "client_id":     os.getenv("GOOGLE_ADS_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
        }, timeout=15)
        token  = resp.json()["access_token"]
        MCC_ID = os.getenv("GOOGLE_ADS_MCC_ID", "").replace("-", "")
        hdrs   = {
            "Authorization":     f"Bearer {token}",
            "developer-token":   os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
            "login-customer-id": MCC_ID,
            "Content-Type":      "application/json",
        }
    except Exception as e:
        return {"_erro": str(e)}

    gastos  = {}
    aceitas = [c for c in contas if c["status"] == "✅ Aceito"]
    for c in aceitas:
        acc_clean = c["id"].replace("-", "")
        try:
            r = requests.post(
                f"https://googleads.googleapis.com/v21/customers/{acc_clean}/googleAds:search",
                headers=hdrs,
                json={"query": f"""
                    SELECT metrics.cost_micros
                    FROM campaign
                    WHERE segments.date BETWEEN '{date_from}' AND '{date_to}'
                    AND campaign.status != 'REMOVED'
                """},
                timeout=10,
            )
            if r.status_code == 403:
                erros    = r.json().get("error", {}).get("details", [{}])
                auth_err = erros[0].get("errors", [{}])[0].get("errorCode", {}).get("authorizationError", "")
                gastos[c["id"]] = "INATIVA" if auth_err == "CUSTOMER_NOT_ENABLED" else "ERRO"
            else:
                total_micros = sum(
                    int(row.get("metrics", {}).get("costMicros", 0) or 0)
                    for row in r.json().get("results", [])
                )
                gastos[c["id"]] = round(total_micros / 1_000_000, 2)
        except Exception:
            gastos[c["id"]] = "ERRO"
        time.sleep(0.15)

    return gastos


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
        ["📋 Contestações", "🔗 Convites MCC", "📈 Click Time"],
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
# PÁGINA 2 — CONVITES MCC
# ════════════════════════════════════════════════════════
elif pagina == "🔗 Convites MCC":
    # Auto-sync silencioso: detecta e adiciona novas contas do Redtrack
    _novas = _sync_novas_contas_redtrack()
    if _novas > 0:
        st.toast(f"✅ {_novas} nova(s) conta(s) adicionada(s) automaticamente!", icon="🔗")

    contas = carregar_contas()

    col_titulo, col_btn = st.columns([4, 1])
    with col_titulo:
        st.subheader("Convites MCC")
        st.caption("Acompanhe o status de cada convite enviado para vinculação na MCC")
    with col_btn:
        st.write("")
        st.write("")
        if st.button("🔄 Sincronizar com Google Ads", use_container_width=True):
            with st.spinner("Consultando API..."):
                contas, atualizados, erro = sincronizar_com_api(contas)
            if erro:
                st.error(f"Erro: {erro}")
            else:
                salvar_contas(contas)
                st.success(f"{atualizados} conta(s) atualizada(s)!")
                st.rerun()

    # Seletor de período para gastos
    col_de, col_ate, col_btn_gastos = st.columns([1.5, 1.5, 1])
    with col_de:
        gastos_de = st.date_input("De", value=date.today().replace(day=1), key="gastos_de")
    with col_ate:
        gastos_ate = st.date_input("Até", value=date.today(), key="gastos_ate")
    with col_btn_gastos:
        st.write("")
        st.write("")
        if st.button("💰 Carregar gastos", use_container_width=True):
            with st.spinner("Consultando gastos via API..."):
                contas_recarregadas = carregar_contas()
                gastos_result = buscar_gastos_mcc(
                    contas_recarregadas,
                    str(gastos_de),
                    str(gastos_ate),
                )
                st.session_state["gastos_contas"] = gastos_result
                st.session_state["gastos_periodo"] = f"{gastos_de.strftime('%d/%m')} – {gastos_ate.strftime('%d/%m/%Y')}"
            st.success("Gastos carregados!")

    gastos_map = st.session_state.get("gastos_contas", {})
    gastos_periodo_label = st.session_state.get("gastos_periodo", "")

    total_c = len(contas)
    aceitos = sum(1 for c in contas if c["status"] == "✅ Aceito")
    pendentes_c = sum(1 for c in contas if c["status"] == "⏳ Pendente")
    suspensas_c = sum(1 for c in contas if c["status"] == "🚫 Suspensa")
    nao_enviados = sum(1 for c in contas if c["status"] == "📤 Não enviado")
    elegíveis = total_c - suspensas_c
    progresso = round(aceitos / elegíveis * 100) if elegíveis > 0 else 0

    if "filtro_contas_ativo" not in st.session_state:
        st.session_state.filtro_contas_ativo = "Todos"

    st.markdown("""
    <style>
    div[data-testid="column"] button[kind="secondary"] {
        background: #1e1e2e; border: 1px solid #333; border-radius: 10px;
        padding: 16px 8px; width: 100%; text-align: left; cursor: pointer;
    }
    div[data-testid="column"] button[kind="secondary"]:hover { border-color: #888; }
    </style>
    """, unsafe_allow_html=True)

    col1, col2, col3, col4, col5 = st.columns(5)
    cartoes = [
        (col1, "Todos",           f"**{total_c}**\nTotal"),
        (col2, "✅ Aceito",       f"**{aceitos}**\n✅ Aceitos"),
        (col3, "⏳ Pendente",     f"**{pendentes_c}**\n⏳ Pendentes"),
        (col4, "📤 Não enviado",  f"**{nao_enviados}**\n📤 Não enviados"),
        (col5, "🚫 Suspensa",     f"**{suspensas_c}**\n🚫 Suspensas"),
    ]
    for col, valor, label in cartoes:
        ativo = st.session_state.filtro_contas_ativo == valor
        with col:
            st.markdown(f"{'### ' if ativo else '#### '}{label.split(chr(10))[1]}", unsafe_allow_html=False)
            st.markdown(f"# {label.split(chr(10))[0].replace('**','')}")
            if st.button("●" if ativo else "○", key=f"btn_{valor}", help=f"Filtrar: {valor}", use_container_width=True):
                st.session_state.filtro_contas_ativo = valor
                st.rerun()

    st.progress(progresso / 100)
    st.divider()

    busca = st.text_input("Buscar por ID ou nome", key="busca_contas")
    filtro_status = st.session_state.filtro_contas_ativo

    filtradas = [
        c for c in contas
        if (filtro_status == "Todos" or c["status"] == filtro_status)
        and (not busca or busca.lower() in c["id"].lower() or busca.lower() in c["nome"].lower())
    ]

    st.markdown(f"**{len(filtradas)} conta(s) encontrada(s)**")
    st.divider()

    # Marcação em massa de contas suspensas
    with st.expander("🚫 Marcar contas como Suspensas em massa"):
        ids_bulk = st.text_area(
            "Cole os IDs das contas (um por linha):",
            key="ids_suspensas_bulk",
            placeholder="123-456-7890\n234-567-8901\n..."
        )
        if st.button("✔ Aplicar — marcar como 🚫 Suspensa", key="btn_bulk_suspensa"):
            ids_lista = [x.strip() for x in ids_bulk.splitlines() if x.strip()]
            ids_mapa = {c["id"]: c for c in contas}
            marcadas = 0
            for acc_id in ids_lista:
                if acc_id in ids_mapa and ids_mapa[acc_id]["status"] != "🚫 Suspensa":
                    ids_mapa[acc_id]["status"] = "🚫 Suspensa"
                    marcadas += 1
                    key = f"sel_{acc_id}"
                    if key in st.session_state:
                        del st.session_state[key]
            salvar_contas(contas)
            st.success(f"{marcadas} conta(s) marcada(s) como suspensas!")
            st.rerun()

    st.divider()

    # ── Tabela com data_editor ────────────────────────────
    opcoes = ["✅ Aceito", "⏳ Pendente", "📤 Não enviado", "🚫 Suspensa"]
    gastos_col = f"Gastos {'(' + gastos_periodo_label + ')' if gastos_periodo_label else ''}"

    rows = []
    for c in filtradas:
        gasto_val = gastos_map.get(c["id"])
        if gasto_val == "INATIVA":
            g = "⚠️ Inativa"
        elif gasto_val == "ERRO":
            g = "❌ Erro"
        elif isinstance(gasto_val, (int, float)):
            g = f"${gasto_val:,.2f}"
        else:
            g = "—"

        sc = c.get("status_conta", "")
        moeda = c.get("moeda", "")
        if c["status"] == "🚫 Suspensa":
            conta_str = "🔴 Suspensa"
        elif sc:
            lbl, _ = STATUS_CONTA.get(sc, (f"❓ {sc}", "normal"))
            conta_str = f"{lbl} {moeda}".strip()
        else:
            conta_str = "—"

        rows.append({
            "ID":           c["id"],
            "Nome":         c["nome"],
            "Convite":      "— Nulo" if c["status"] == "🚫 Suspensa" else c["status"],
            "Data convite": c.get("data_convite") or "—",
            gastos_col:     g,
            "Conta":        conta_str,
            "Status":       c["status"],
        })

    df_mcc = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["ID", "Nome", "Convite", "Data convite", gastos_col, "Conta", "Status"]
    )

    if "editor_v" not in st.session_state:
        st.session_state["editor_v"] = 0

    edited_df = st.data_editor(
        df_mcc,
        column_config={
            "ID":          st.column_config.TextColumn("ID",           disabled=True, width="medium"),
            "Nome":        st.column_config.TextColumn("Nome",         disabled=True, width="large"),
            "Convite":     st.column_config.TextColumn("Convite",      disabled=True, width="medium"),
            "Data convite":st.column_config.TextColumn("Data convite", disabled=True, width="small"),
            gastos_col:    st.column_config.TextColumn(gastos_col,     disabled=True, width="small"),
            "Conta":       st.column_config.TextColumn("Conta",        disabled=True, width="medium"),
            "Status":      st.column_config.SelectboxColumn(
                               "Alterar status", options=opcoes, width="medium"
                           ),
        },
        hide_index=True,
        use_container_width=True,
        key=f"editor_mcc_{st.session_state['editor_v']}",
    )

    if gastos_map:
        total_gastos = sum(v for c in filtradas if isinstance(v := gastos_map.get(c["id"], 0), (int, float)))
        st.caption(f"Total gastos — **${total_gastos:,.2f}** ({len(filtradas)} contas)")

    st.divider()
    col_info, col_save_btn = st.columns([3, 1])
    col_info.caption("Edite o status na coluna 'Alterar status' e clique em Salvar.")
    if col_save_btn.button("💾 Salvar alterações", use_container_width=True, type="primary", key="btn_salvar_tabela"):
        id_to_novo = dict(zip(edited_df["ID"], edited_df["Status"]))
        alterados = 0
        for conta in contas:
            novo = id_to_novo.get(conta["id"])
            if novo and novo != conta["status"]:
                conta["status"] = novo
                alterados += 1
        salvar_contas(contas)
        st.session_state["editor_v"] += 1
        if alterados:
            st.success(f"{alterados} conta(s) atualizada(s)!")
        else:
            st.info("Nenhuma alteração detectada.")
        st.rerun()


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


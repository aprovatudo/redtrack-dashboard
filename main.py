import os
import requests
import schedule
import time
import tempfile
from datetime import datetime, timedelta
from dotenv import load_dotenv
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches

load_dotenv()

REDTRACK_API_KEY = os.getenv("REDTRACK_API_KEY")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID")
SEND_TIME = os.getenv("SEND_TIME", "08:00")


# Produtos ativos — apenas esses serão buscados no relatório
# Adicione ou remova conforme as ofertas ficarem ativas/inativas
ACTIVE_PRODUCTS = [
    "Prime Pulse",
    "Vigoryn",
    "CocoBurn",
    "BrainHoney",
    "BrainVex",   # alias de BrainHoney — ver PRODUCT_ALIASES
    "JellyFit",
    "NeuroSalt",
    # "Max Brain",
]

# Aliases — campanhas com nomes diferentes que representam o mesmo produto.
# Todos os aliases são agrupados sob o nome canônico (chave).
PRODUCT_ALIASES = {
    "Prime Pulse": ["Vigoryn"],
    "BrainHoney":  ["BrainVex"],
}

BG_DARK    = "#0d0d1a"
BG_ROW     = "#12122a"
BG_ROW_ALT = "#1a1a35"
BG_HEADER  = "#0a0a20"
COLOR_BLUE = "#00bfff"
COLOR_WHITE= "#e8e8f0"
COLOR_GREEN= "#00dd77"
COLOR_RED  = "#ff4455"
COLOR_BORDER = "#2a2a50"


def fetch_campaigns() -> list:
    for attempt in range(3):
        try:
            response = requests.get(
                "https://api.redtrack.io/campaigns",
                params={"api_key": REDTRACK_API_KEY, "status": 1},
                timeout=60,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.Timeout:
            wait = 15 * (attempt + 1)
            print(f"  Timeout ao buscar campanhas, aguardando {wait}s...")
            time.sleep(wait)
    raise Exception("Falha ao buscar campanhas após 3 tentativas")


def fetch_report_by_campaign(campaign_id: str, date: str) -> dict:
    for attempt in range(3):
        try:
            response = requests.get(
                "https://api.redtrack.io/report",
                params={
                    "api_key": REDTRACK_API_KEY,
                    "date_from": date,
                    "date_to": date,
                    "campaign_id": campaign_id,
                },
                timeout=60,
            )
            if response.status_code == 429:
                wait = 10 * (attempt + 1)
                print(f"  Rate limit, aguardando {wait}s...")
                time.sleep(wait)
                continue
            response.raise_for_status()
            data = response.json()
            return data[0] if isinstance(data, list) and data else {}
        except requests.exceptions.Timeout:
            wait = 15 * (attempt + 1)
            print(f"  Timeout campanha {campaign_id}, aguardando {wait}s...")
            time.sleep(wait)
    return {}


OFFER_SOURCES = {"clickbank", "pagamerican"}

def extract_product_name(title: str) -> str | None:
    """Extrai o nome do produto após o identificador da offer source no título da campanha."""
    parts = title.split("|")
    for i, part in enumerate(parts):
        if part.strip().lower() in OFFER_SOURCES and i + 1 < len(parts):
            return parts[i + 1].strip()
    return None


def is_youtube_campaign(title: str) -> bool:
    title_lower = title.lower()
    return "youtube" in title_lower or "| yt |" in title_lower


def _build_product_lookup() -> dict:
    """Constrói mapa lowercase → nome canônico para match case-insensitive."""
    lookup = {}
    for name in ACTIVE_PRODUCTS:
        lookup[name.lower()] = name
    for canonical, aliases in PRODUCT_ALIASES.items():
        for alias in aliases:
            lookup[alias.lower()] = canonical
    return lookup

_PRODUCT_LOOKUP = _build_product_lookup()


def canonical_name(product: str) -> str:
    """Retorna o nome canônico do produto (case-insensitive), resolvendo aliases."""
    return _PRODUCT_LOOKUP.get(product.lower(), product)


def group_campaigns_by_product(campaigns: list, debug: bool = False) -> dict:
    grouped = {}
    skipped_not_yt = []
    skipped_no_product = []
    skipped_inactive = []

    for camp in campaigns:
        title = camp["title"]
        if not is_youtube_campaign(title):
            skipped_not_yt.append(title)
            continue
        product = extract_product_name(title)
        if not product:
            skipped_no_product.append(title)
            continue
        # Match case-insensitive contra ACTIVE_PRODUCTS e aliases
        if product.lower() not in _PRODUCT_LOOKUP:
            skipped_inactive.append(f"{product} ({title})")
            continue
        name = canonical_name(product)
        grouped.setdefault(name, []).append(camp["id"])

    if debug:
        print(f"\n  [debug] Total campanhas recebidas: {len(campaigns)}")
        print(f"  [debug] Não-YouTube filtradas: {len(skipped_not_yt)}")
        print(f"  [debug] Sem produto no título: {len(skipped_no_product)}")
        print(f"  [debug] Produto não ativo (amostra):")
        for s in skipped_inactive[:5]:
            print(f"    → {s}")
        print(f"  [debug] Agrupados: { {k: len(v) for k, v in grouped.items()} }\n")

    return grouped


def aggregate_product(campaign_ids: list, date: str) -> dict:
    totals = {"cost": 0.0, "total_revenue": 0.0, "profit": 0.0, "convtype1": 0, "convtype2": 0, "_cpc_weighted": 0.0}
    for cid in campaign_ids:
        data = fetch_report_by_campaign(cid, date)
        cost = float(data.get("cost", 0))
        if cost > 0:
            totals["cost"] += cost
            totals["total_revenue"] += float(data.get("total_revenue", 0))
            totals["profit"] += float(data.get("profit", 0))
            totals["convtype1"] += int(data.get("convtype1", 0))
            totals["convtype2"] += int(data.get("convtype2", 0))
            totals["_cpc_weighted"] += float(data.get("cpc", 0)) * cost
        time.sleep(1)
    cw = totals.pop("_cpc_weighted", 0.0)
    totals["cpc"] = cw / totals["cost"] if totals["cost"] > 0 else 0.0
    return totals


def fmt_brl(value: float) -> str:
    abs_val = abs(value)
    formatted = f"R$ {abs_val:,.0f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"-{formatted}" if value < 0 else formatted


def fmt_pct(value: float) -> str:
    return f"{value:.2f}%"


def fmt_num(value: float) -> str:
    return f"{value:.2f}"


def generate_report_image(rows: list, date_str: str) -> str:
    columns = ["Oferta", "Gastos", "Faturamento", "Lucro", "Vendas", "IC", "CPC", "CPA", "AOV", "CP/IC", "Purchase CR", "ROAS"]
    col_widths = [0.13, 0.09, 0.09, 0.09, 0.06, 0.05, 0.07, 0.08, 0.08, 0.07, 0.09, 0.06]

    table_data = []
    value_data = []

    for row in rows:
        sales = row["convtype1"]
        ic = row["convtype2"]
        cost = row["cost"]
        revenue = row["total_revenue"]
        profit = row["profit"]
        cpc = row.get("cpc", 0)
        cpa = cost / sales if sales > 0 else 0
        aov = revenue / sales if sales > 0 else 0
        cpic = cost / ic if ic > 0 else 0
        purchase_cr = sales / ic * 100 if ic > 0 else 0
        roas = revenue / cost if cost > 0 else 0

        table_data.append([
            row["title"],
            fmt_brl(cost),
            fmt_brl(revenue),
            fmt_brl(profit),
            str(sales),
            str(ic),
            fmt_brl(cpc),
            fmt_brl(cpa),
            fmt_brl(aov),
            fmt_brl(cpic),
            fmt_pct(purchase_cr),
            fmt_num(roas),
        ])
        value_data.append({"profit": profit, "roas": roas})

    n_rows = len(rows)
    fig_height = 1.6 + n_rows * 0.55
    fig, ax = plt.subplots(figsize=(20, fig_height))
    fig.patch.set_facecolor(BG_DARK)
    ax.set_facecolor(BG_DARK)
    ax.axis("off")

    # Title
    ax.text(
        0.5, 0.97, f"Relatório Google Ads - {date_str}",
        transform=ax.transAxes, ha="center", va="top",
        color=COLOR_BLUE, fontsize=15, fontweight="bold",
        fontfamily="DejaVu Sans",
    )

    # Layout
    margin_left = 0.01
    margin_right = 0.99
    header_y = 0.82
    row_height = 0.13
    header_height = 0.13

    total_width = margin_right - margin_left
    col_starts = []
    x = margin_left
    for w in col_widths:
        col_starts.append(x)
        x += w * total_width

    # Header background
    header_rect = patches.FancyBboxPatch(
        (margin_left, header_y - header_height * 0.05),
        total_width, header_height,
        boxstyle="round,pad=0.005",
        linewidth=0,
        facecolor=BG_HEADER,
        transform=ax.transAxes, clip_on=False,
    )
    ax.add_patch(header_rect)

    # Header text
    for j, (col, cx, cw) in enumerate(zip(columns, col_starts, col_widths)):
        align = "left" if j == 0 else "right"
        offset = 0 if j == 0 else cw * total_width
        ax.text(
            cx + offset * 0.95, header_y + header_height * 0.4,
            col,
            transform=ax.transAxes, ha=align, va="center",
            color=COLOR_BLUE, fontsize=10, fontweight="bold",
        )

    # Rows
    for i, (row_vals, vdata) in enumerate(zip(table_data, value_data)):
        y = header_y - (i + 1) * row_height
        bg = BG_ROW if i % 2 == 0 else BG_ROW_ALT

        row_rect = patches.FancyBboxPatch(
            (margin_left, y - row_height * 0.05),
            total_width, row_height,
            boxstyle="round,pad=0.003",
            linewidth=0,
            facecolor=bg,
            transform=ax.transAxes, clip_on=False,
        )
        ax.add_patch(row_rect)

        for j, (val, cx, cw) in enumerate(zip(row_vals, col_starts, col_widths)):
            align = "left" if j == 0 else "right"
            offset = 0 if j == 0 else cw * total_width

            # Color logic
            color = COLOR_WHITE
            if j == 5:  # Lucro
                color = COLOR_GREEN if vdata["profit"] >= 0 else COLOR_RED
            elif j == 7:  # ROAS
                color = COLOR_GREEN if vdata["roas"] >= 1 else COLOR_RED

            ax.text(
                cx + offset * 0.95, y + row_height * 0.35,
                val,
                transform=ax.transAxes, ha=align, va="center",
                color=color, fontsize=10,
            )

    # Border
    border = patches.FancyBboxPatch(
        (margin_left, header_y - n_rows * row_height - row_height * 0.1),
        total_width, (n_rows + 1) * row_height + row_height * 0.15,
        boxstyle="round,pad=0.005",
        linewidth=1, edgecolor=COLOR_BORDER,
        facecolor="none",
        transform=ax.transAxes, clip_on=False,
    )
    ax.add_patch(border)

    plt.tight_layout(pad=0.5)

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    plt.savefig(tmp.name, dpi=150, bbox_inches="tight", facecolor=BG_DARK)
    plt.close()
    return tmp.name


def upload_to_slack(image_path: str, date_str: str):
    filename = f"relatorio_{date_str.replace('/', '-')}.png"
    file_size = os.path.getsize(image_path)

    # Step 1: get upload URL
    r1 = requests.get(
        "https://slack.com/api/files.getUploadURLExternal",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        params={"filename": filename, "length": file_size},
        timeout=30,
    )
    r1.raise_for_status()
    data1 = r1.json()
    if not data1.get("ok"):
        raise Exception(f"Slack getUploadURL error: {data1.get('error')}")

    upload_url = data1["upload_url"]
    file_id = data1["file_id"]

    # Step 2: upload file
    with open(image_path, "rb") as f:
        r2 = requests.post(upload_url, data=f, timeout=60)
    r2.raise_for_status()

    # Step 3: complete upload and share to channel
    r3 = requests.post(
        "https://slack.com/api/files.completeUploadExternal",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={
            "files": [{"id": file_id, "title": f"Relatório Google Ads - {date_str}"}],
            "channel_id": SLACK_CHANNEL_ID,
        },
        timeout=30,
    )
    r3.raise_for_status()
    data3 = r3.json()
    if not data3.get("ok"):
        raise Exception(f"Slack completeUpload error: {data3.get('error')}")


def run_report():
    yesterday = datetime.now() - timedelta(days=1)
    date_str = yesterday.strftime("%d/%m/%Y")
    date_param = yesterday.strftime("%Y-%m-%d")

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Buscando relatório de {date_str}...")

    try:
        campaigns = fetch_campaigns()
        grouped = group_campaigns_by_product(campaigns)

        rows = []
        for product_name, campaign_ids in grouped.items():
            if not campaign_ids:
                print(f"  {product_name}: nenhuma campanha encontrada")
                continue
            print(f"  {product_name}: {len(campaign_ids)} campanhas...")
            totals = aggregate_product(campaign_ids, date_param)
            if totals["cost"] > 0:
                totals["title"] = product_name
                rows.append(totals)
            else:
                print(f"  {product_name}: sem gasto no período")

        if not rows:
            print("Nenhum produto com gasto no período.")
            return

        print("  Gerando imagem...")
        image_path = generate_report_image(rows, date_str)

        print("  Enviando para o Slack...")
        upload_to_slack(image_path, date_str)
        os.unlink(image_path)

        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Relatório enviado! ({len(rows)} produtos)")
    except Exception as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Erro: {e}")


def main():
    print(f"Agendador iniciado. Relatório será enviado diariamente às {SEND_TIME}.")
    schedule.every().day.at(SEND_TIME).do(run_report)

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        print("Modo teste: enviando relatório agora...")
        run_report()
    else:
        main()

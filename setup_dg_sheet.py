"""
setup_dg_sheet.py — Cria a planilha de Campanhas DG e imprime o ID.

Rode uma vez. O ID gerado deve ser adicionado ao .env como:
  DG_SPREADSHEET_ID=<id>
"""

import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")
TOKEN_FILE       = os.path.join(os.path.dirname(__file__), "token_dg.json")

HEADER = [
    "ACCOUNT_ID", "NOME_CAMPANHA", "ORCAMENTO_DIARIO", "VIDEO_URL",
    "URL_FINAL", "TITULO_1", "TITULO_2", "DESCRICAO", "CTA", "LOGO_URL",
    "PAIS", "ESTRATEGIA", "TARGET_CPA", "STATUS", "CAMPAIGN_ID", "OBSERVACAO",
]


def get_creds():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


def main():
    creds   = get_creds()
    sheets  = build("sheets", "v4", credentials=creds).spreadsheets()

    # Cria a planilha com a aba já nomeada
    spreadsheet = sheets.create(body={
        "properties": {"title": "Campanhas DG — Google Ads"},
        "sheets": [{"properties": {"title": "Campanhas DG"}}],
    }).execute()

    spreadsheet_id = spreadsheet["spreadsheetId"]
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"

    # Escreve o cabeçalho
    sheets.values().update(
        spreadsheetId=spreadsheet_id,
        range="'Campanhas DG'!A1",
        valueInputOption="RAW",
        body={"values": [HEADER]},
    ).execute()

    # Formata o cabeçalho (negrito + fundo cinza)
    sheet_id = spreadsheet["sheets"][0]["properties"]["sheetId"]
    sheets.batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": [
        {"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.26, "green": 0.26, "blue": 0.26},
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat)",
        }},
        {"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
    ]}).execute()

    print(f"\n✅ Planilha criada com sucesso!")
    print(f"   URL: {url}")
    print(f"   ID:  {spreadsheet_id}")
    print(f"\nAdicione ao .env:")
    print(f"   DG_SPREADSHEET_ID={spreadsheet_id}")


if __name__ == "__main__":
    main()

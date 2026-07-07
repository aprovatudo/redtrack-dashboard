"""
Cria contas filhas na MCC do Google Ads com configurações padrão:
- Moeda: USD
- Fuso: America/Sao_Paulo (Brasília)
- Adequação de conteúdo configurada
- Inicia verificação de anunciante

Uso:
    python create_ads_account.py --name="Nome da Conta"
    python create_ads_account.py --name="Conta 1" --name="Conta 2"

Variáveis necessárias no .env:
    GOOGLE_ADS_DEVELOPER_TOKEN
    GOOGLE_ADS_CLIENT_ID
    GOOGLE_ADS_CLIENT_SECRET
    GOOGLE_ADS_REFRESH_TOKEN
    GOOGLE_ADS_MCC_ID  (sem hífens, ex: 1234567890)
"""

import os
import sys
import argparse
from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

MCC_ID = os.getenv("GOOGLE_ADS_MCC_ID", "").replace("-", "")


def build_client() -> GoogleAdsClient:
    config = {
        "developer_token": os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN"),
        "client_id": os.getenv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret": os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token": os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
        "login_customer_id": MCC_ID,
        "use_proto_plus": True,
    }
    missing = [k for k, v in config.items() if not v]
    if missing:
        print(f"Variáveis de ambiente ausentes: {missing}")
        sys.exit(1)
    return GoogleAdsClient.load_from_dict(config)


def create_account(client: GoogleAdsClient, account_name: str) -> str:
    customer_service = client.get_service("CustomerService")

    customer = client.get_type("Customer")
    customer.descriptive_name = account_name
    customer.currency_code = "USD"
    customer.time_zone = "America/Sao_Paulo"

    response = customer_service.create_customer_client(
        customer_id=MCC_ID,
        customer_client=customer,
    )

    # resource_name = "customers/XXXXXXXXXX"
    new_id = response.resource_name.split("/")[1]
    return new_id


def set_content_suitability(client: GoogleAdsClient, customer_id: str):
    """Define adequação de conteúdo: inventário padrão (exclui conteúdo sensível)."""
    customer_service = client.get_service("CustomerService")

    customer = client.get_type("Customer")
    customer.resource_name = customer_service.customer_path(customer_id)
    customer.brand_safety_suitability = (
        client.enums.BrandSafetySuitabilityEnum.STANDARD_INVENTORY
    )

    operation = client.get_type("CustomerOperation")
    operation.update = customer
    operation.update_mask.paths.append("brand_safety_suitability")

    customer_service.mutate_customer(
        customer_id=customer_id,
        operation=operation,
    )


def start_advertiser_verification(client: GoogleAdsClient, customer_id: str):
    """Inicia o processo de verificação de anunciante."""
    verification_service = client.get_service("IdentityVerificationService")
    verification_service.start_identity_verification(
        customer_id=customer_id,
        verification_program=client.enums.IdentityVerificationProgramEnum.ADVERTISER_IDENTITY_VERIFICATION,
    )


def setup_account(client: GoogleAdsClient, account_name: str):
    print(f"\n→ Criando conta: '{account_name}'...")

    try:
        new_id = create_account(client, account_name)
        print(f"  ✓ Conta criada — ID: {new_id}")
    except GoogleAdsException as e:
        print(f"  ✗ Erro ao criar conta: {e.failure}")
        return

    try:
        set_content_suitability(client, new_id)
        print(f"  ✓ Adequação de conteúdo configurada (inventário padrão)")
    except GoogleAdsException as e:
        print(f"  ⚠ Adequação de conteúdo: {e.failure}")

    try:
        start_advertiser_verification(client, new_id)
        print(f"  ✓ Verificação de anunciante iniciada")
        print(f"  ℹ  Acesse a conta {new_id} para completar o envio dos documentos")
    except GoogleAdsException as e:
        print(f"  ⚠ Verificação de anunciante: {e.failure}")

    print(f"\n  Conta '{account_name}' (ID: {new_id}) configurada.")
    print(f"  Próximo passo manual: aceitar termos de serviço e adicionar pagamento.")
    return new_id


def main():
    parser = argparse.ArgumentParser(description="Cria contas no Google Ads via MCC")
    parser.add_argument(
        "--name",
        action="append",
        dest="names",
        required=True,
        help="Nome da conta (pode repetir para múltiplas contas)",
    )
    args = parser.parse_args()

    client = build_client()
    print(f"MCC: {MCC_ID}")
    print(f"Contas a criar: {args.names}")

    for name in args.names:
        setup_account(client, name)

    print("\nConcluído.")


if __name__ == "__main__":
    main()

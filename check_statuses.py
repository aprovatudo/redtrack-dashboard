import os, json, requests
from collections import Counter
from dotenv import load_dotenv

load_dotenv()

DEV_TOKEN     = os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN")
CLIENT_ID     = os.getenv("GOOGLE_ADS_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_ADS_CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("GOOGLE_ADS_REFRESH_TOKEN")
MCC_ID        = os.getenv("GOOGLE_ADS_MCC_ID", "").replace("-", "")

r = requests.post("https://oauth2.googleapis.com/token", data={
    "grant_type": "refresh_token",
    "refresh_token": REFRESH_TOKEN,
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
}, timeout=15)
access_token = r.json()["access_token"]
print(f"Token OK | MCC: {MCC_ID}")

accounts = []
with open("accounts_to_invite.txt", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line.startswith("✅"):
            parts  = line[1:].strip().split("|", 1)
            acc_id = parts[0].strip()
            nome   = parts[1].strip() if len(parts) > 1 else acc_id
            accounts.append((acc_id, nome))

print(f"{len(accounts)} contas ✅ encontradas\n")

headers = {
    "Authorization": f"Bearer {access_token}",
    "developer-token": DEV_TOKEN,
    "login-customer-id": MCC_ID,
    "Content-Type": "application/json",
}

QUERY = "SELECT customer.id, customer.status FROM customer LIMIT 1"

results = {}
for i, (acc_id, nome) in enumerate(accounts, 1):
    cid = acc_id.replace("-", "")
    try:
        resp = requests.post(
            f"https://googleads.googleapis.com/v21/customers/{cid}/googleAds:searchStream",
            headers=headers, json={"query": QUERY}, timeout=10
        )
        if resp.ok:
            status = "UNKNOWN"
            for batch in resp.json():
                for row in batch.get("results", []):
                    status = row.get("customer", {}).get("status", "UNKNOWN")
                    break
            results[acc_id] = {"nome": nome, "status": status}
        elif resp.status_code == 403:
            results[acc_id] = {"nome": nome, "status": "NO_ACCESS"}
        else:
            results[acc_id] = {"nome": nome, "status": f"ERR_{resp.status_code}"}
    except Exception:
        results[acc_id] = {"nome": nome, "status": "TIMEOUT"}

    if i % 10 == 0:
        print(f"  {i}/{len(accounts)} consultadas...")

with open("account_statuses.json", "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False)

counts = Counter(v["status"] for v in results.values())
print("\n=== RESUMO ===")
for status, count in sorted(counts.items(), key=lambda x: -x[1]):
    print(f"  {status:20s} {count}")

enabled = [(k, v["nome"]) for k, v in results.items() if v["status"] == "ENABLED"]
print(f"\nContas ENABLED (ativas): {len(enabled)}")
for acc_id, nome in enabled:
    print(f"  {acc_id} | {nome}")

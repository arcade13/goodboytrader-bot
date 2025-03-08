import requests

url = "https://www.okx.com/api/v5/market/candles?instId=SOL-USDT-SWAP&bar=4H&limit=10"

try:
    response = requests.get(url, timeout=10)
    print("✅ Successfully fetched data:")
    print(response.json())
except requests.exceptions.RequestException as e:
    print(f"❌ Python requests failed: {e}")


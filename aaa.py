import requests

url = "https://www.okx.com/api/v5/market/candles?instId=SOL-USDT-SWAP&bar=4H&limit=10"
response = requests.get(url)
print(response.json())


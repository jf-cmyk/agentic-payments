import urllib.request, json, os, urllib.error
api_key = os.popen('grep BLOCKSIZE_API_KEY .env | cut -d "=" -f 2').read().strip()
data = json.dumps({"jsonrpc":"2.0","id":1,"method":"vwap_latest","params":{"ticker":"BTCUSD"}}).encode()
req = urllib.request.Request("https://data.blocksize.capital/marketdata/v1/api", data=data, headers={"x-api-key": api_key, "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req) as response:
        print(response.read().decode())
except urllib.error.URLError as e:
    print(e.read().decode() if hasattr(e, 'read') else e)

import requests, json
url = "https://api-v2-report.emoneeds.com/api/v1/reports/patient-reports/master-data?page=1&limit=10"
try:
    r = requests.get(url, timeout=30)
    print("STATUS:", r.status_code)
    if r.status_code == 200:
        data = r.json()
        print("TOP-LEVEL KEYS:", list(data.keys()) if isinstance(data, dict) else "list")
        # asli records kahan hain dhoondo
        rows = data.get("data") if isinstance(data, dict) else data
        if isinstance(rows, dict):
            print("data ke andar keys:", list(rows.keys()))
            for k,v in rows.items():
                if isinstance(v, list): print(f"  '{k}' is a list of {len(v)}"); rows=v; break
        if isinstance(rows, list) and rows:
            print("TOTAL in this page:", len(rows))
            print("COLUMNS:", list(rows[0].keys()))
    else:
        print("BODY:", r.text[:300])
except Exception as e:
    print("ERROR:", e)

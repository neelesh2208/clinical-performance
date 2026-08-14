import requests, json
url = "https://api-v2-report.emoneeds.com/api/v1/reports/patient-reports/master-data?page=1&limit=3"
r = requests.get(url, timeout=30)
rows = r.json()["data"]
print("TOTAL META:", r.json().get("meta"))
print("---- FIRST RECORD (full) ----")
print(json.dumps(rows[0], indent=2, default=str)[:1500])

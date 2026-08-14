import requests, json
url = "https://api-v2-report.emoneeds.com/api/v1/reports/patient-reports/master-data?page=400&limit=50"
resp = requests.get(url, timeout=60).json()
rows = resp["data"]
print("page 400 pehla createdAt:", rows[0].get("createdAt") if rows else "empty")
print("page 400 aakhri createdAt:", rows[-1].get("createdAt") if rows else "empty")
for r in rows:
    if r.get("assignments"):
        print("---- assignments bhara ----")
        print(json.dumps(r["assignments"], indent=2, default=str)[:800]); break
else: print("page 400 me assignments nahi")
for r in rows:
    if r.get("package"):
        print("---- package bhara ----")
        print(json.dumps(r["package"], indent=2, default=str)[:500]); break
else: print("page 400 me package nahi")

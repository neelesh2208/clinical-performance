import requests, json
# ek door ka page (purane records) le ke assignment/package dekho
url = "https://api-v2-report.emoneeds.com/api/v1/reports/patient-reports/master-data?page=2000&limit=50"
resp = requests.get(url, timeout=60).json()
rows = resp["data"]
print("page 2000 ka pehla createdAt:", rows[0].get("createdAt") if rows else "empty")
print("page 2000 ka aakhri createdAt:", rows[-1].get("createdAt") if rows else "empty")
for r in rows:
    if r.get("assignments"):
        print("---- assignments bhara ----")
        print(json.dumps(r["assignments"], indent=2, default=str)[:800]); break
for r in rows:
    if r.get("package"):
        print("---- package bhara ----")
        print(json.dumps(r["package"], indent=2, default=str)[:500]); break
# newest-first confirm karne ke liye page 1 ka pehla vs page 2 ka pehla
p1 = requests.get(url.replace("page=2000","page=1"), timeout=60).json()["data"]
print("page 1 pehla createdAt:", p1[0].get("createdAt"))

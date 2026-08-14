import requests, json
# thoda bada sample le ke aisा record dhoondo jisme assignments/package bhara ho
url = "https://api-v2-report.emoneeds.com/api/v1/reports/patient-reports/master-data?page=1&limit=200"
rows = requests.get(url, timeout=60).json()["data"]
# non-empty assignments wala
for r in rows:
    if r.get("assignments"):
        print("---- assignments bhara record ----")
        print(json.dumps(r, indent=2, default=str)[:1200]); break
else:
    print("200 me koi assignments nahi mila")
# non-null package wala
for r in rows:
    if r.get("package"):
        print("---- package bhara record ----")
        print(json.dumps(r.get("package"), indent=2, default=str)[:600]); break
else:
    print("200 me koi package nahi mila")

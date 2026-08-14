import requests, json
url = "https://api-v2-report.emoneeds.com/api/v1/reports/patient-reports/master-data"
resp = requests.get(url, params={"page":1,"limit":5}, timeout=60)
payload = resp.json()
print("payload TYPE:", type(payload).__name__)
if isinstance(payload, dict):
    print("payload KEYS:", list(payload.keys()))
    print("data TYPE:", type(payload.get('data')).__name__)
    print("meta TYPE:", type(payload.get('meta')).__name__)
    print("meta VALUE:", payload.get('meta'))
else:
    print("payload is a LIST, length:", len(payload))

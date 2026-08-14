import requests
url = "https://api-v2-report.emoneeds.com/api/v1/reports/patient-reports/master-data"
for lim in [5, 50, 100, 200, 500, 1000]:
    try:
        p = requests.get(url, params={'page':1,'limit':lim}, timeout=90).json()
        t = type(p).__name__
        n = len(p.get('data',[])) if isinstance(p, dict) else len(p)
        print(f"limit={lim}: type={t}, count={n}" + ("" if t=='dict' else "  <-- LIST, yahi crash karता hai"))
    except Exception as e:
        print(f"limit={lim}: ERROR {e}")

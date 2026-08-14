import requests, traceback
url = "https://api-v2-report.emoneeds.com/api/v1/reports/patient-reports/master-data"
def _flatten(val):
    if val is None or val == "" or val == []: return ""
    if isinstance(val,(str,int,float)): return str(val)
    if isinstance(val,dict):
        for key in ("name","title","packageName","label"):
            if val.get(key): return str(val[key])
        return ", ".join(str(v) for v in val.values() if v not in (None,"",[]))
    if isinstance(val,list):
        return ", ".join(p for p in (_flatten(i) for i in val) if p)
    return str(val)
resp = requests.get(url, params={'page':1,'limit':1000}, timeout=90)
rows = resp.json()['data']
print('testing', len(rows), 'rows through _flatten...')
for idx, r in enumerate(rows):
    try:
        _flatten(r.get('package'))
        _flatten(r.get('assignments'))
    except Exception as e:
        print(f'CRASH at row {idx}:')
        import json; print(json.dumps(r, indent=2, default=str)[:800])
        traceback.print_exc(); break
else:
    print('ALL OK — _flatten me koi crash nahi')

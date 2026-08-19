"""验证告警过滤是否生效"""
import requests

BASE = "http://127.0.0.1:8099"

# Login
resp = requests.post(f"{BASE}/api/auth/login", json={"username": "admin", "password": "admin123"})
if resp.status_code != 200:
    print("Login failed:", resp.status_code, resp.text)
    exit(1)
token = resp.json()["token"]
headers = {"Authorization": f"Bearer {token}"}

# Test 1: No project filter (should only show alerts from configured projects)
print("=== Test 1: No project filter (all projects) ===")
r = requests.get(f"{BASE}/api/alerts?page=1&page_size=30", headers=headers)
data = r.json()
print(f"Total alerts: {data['total']}")
for a in data["rows"]:
    proj = a.get("target_info", {}).get("project", "MISSING")
    print(f"  id={a['id']} proj={proj} subj={a['subject'][:45]}")

# Test 2: Filter by hqb (project_id=1)
print("\n=== Test 2: Filter by hqb (project_id=1) ===")
r = requests.get(f"{BASE}/api/alerts?page=1&page_size=30&project_id=1", headers=headers)
data = r.json()
print(f"Total hqb alerts: {data['total']}")
for a in data["rows"]:
    proj = a.get("target_info", {}).get("project", "MISSING")
    print(f"  id={a['id']} proj={proj} subj={a['subject'][:45]}")

# Test 3: Filter by mall (project_id=2)
print("\n=== Test 3: Filter by mall (project_id=2) ===")
r = requests.get(f"{BASE}/api/alerts?page=1&page_size=30&project_id=2", headers=headers)
data = r.json()
print(f"Total mall alerts: {data['total']}")
for a in data["rows"]:
    proj = a.get("target_info", {}).get("project", "MISSING")
    print(f"  id={a['id']} proj={proj} subj={a['subject'][:45]}")

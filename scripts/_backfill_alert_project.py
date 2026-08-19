"""一次性脚本：回填现有告警 target_info 中的 project 字段
策略：1) 精确匹配 services 表  2) 服务名前缀匹配项目编码"""
import sqlite3, json

DB = "data/platform.db"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# 1. Build service_name -> project_code map (精确匹配)
svcs = conn.execute(
    "SELECT s.name, p.code FROM services s JOIN projects p ON s.project_id=p.id"
).fetchall()
svc_map = {r["name"]: r["code"] for r in svcs}
print("Service->Project map:", svc_map)

# 2. Build project code list for prefix matching
project_codes = [r["code"] for r in conn.execute("SELECT code FROM projects").fetchall()]
print("Project codes for prefix match:", project_codes)


def infer_project(service_name):
    """精确匹配 → 前缀推断"""
    if not service_name:
        return ""
    code = svc_map.get(service_name, "")
    if code:
        return code
    # 前缀匹配：服务名以项目编码开头 (如 mall-admin → mall, hqb-system → hqb)
    for pc in project_codes:
        if service_name.startswith(pc + "-") or service_name.startswith(pc + "_"):
            return pc
    return ""


# 3. Get all alerts missing project field
alerts = conn.execute("SELECT id, target_info FROM alerts").fetchall()
updated = 0
for a in alerts:
    ti = json.loads(a["target_info"]) if a["target_info"] else {}
    if "project" not in ti or not ti["project"]:
        svc = ti.get("service", "")
        code = infer_project(svc)
        if code:
            ti["project"] = code
            conn.execute(
                "UPDATE alerts SET target_info=? WHERE id=?",
                (json.dumps(ti, ensure_ascii=False), a["id"])
            )
            updated += 1
            print(f"  Alert {a['id']}: service={svc} -> project={code}")
        else:
            print(f"  Alert {a['id']}: service={svc} -> NO MATCH (orphan)")

conn.commit()
print(f"\nUpdated {updated} alerts with project field")

# 4. Verify
print("\n=== After backfill ===")
rows = conn.execute(
    "SELECT id, status, substr(subject,1,40) as subj, target_info FROM alerts ORDER BY id"
).fetchall()
for r in rows:
    ti = json.loads(r["target_info"]) if r["target_info"] else {}
    proj = ti.get("project", "NONE")
    print(f"  id={r['id']} status={r['status']} proj={proj} subj={r['subj']}")

conn.close()

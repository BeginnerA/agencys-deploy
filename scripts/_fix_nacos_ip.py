"""修复服务器 nacos_ip 配置错误：172.30.170.151 -> 172.30.17.151"""
import sqlite3

conn = sqlite3.connect('data/platform.db')

# 查看修复前
print("修复前:")
for r in conn.execute('SELECT id, host, nacos_ip FROM servers').fetchall():
    print(f"  id={r[0]} host={r[1]} nacos_ip={r[2]}")

# 修复 server id=4 (47.95.172.17) 的 nacos_ip
conn.execute("UPDATE servers SET nacos_ip='172.30.17.151' WHERE id=4")
conn.commit()

# 查看修复后
print("\n修复后:")
for r in conn.execute('SELECT id, host, nacos_ip FROM servers').fetchall():
    print(f"  id={r[0]} host={r[1]} nacos_ip={r[2]}")

conn.close()

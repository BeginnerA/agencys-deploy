"""重置 op1 和 view1 的密码为 test123"""
import sqlite3
from werkzeug.security import generate_password_hash

DB = "data/platform.db"
conn = sqlite3.connect(DB)
h = generate_password_hash("test123")
conn.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE username IN (?, ?)",
             (h, "op1", "view1"))
conn.commit()
print("Updated op1 and view1 passwords")

# Verify
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT username, substr(password_hash,1,40) as ph FROM users").fetchall()
for r in rows:
    print(f"  {r['username']}: {r['ph']}...")
conn.close()

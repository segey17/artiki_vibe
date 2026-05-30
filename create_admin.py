#!/usr/bin/env python3
"""
Создать первого администратора.
Запуск: python3 create_admin.py <username> <password>
"""
import sys, sqlite3, bcrypt, os

DB = os.getenv("DB_PATH", "./data/db.sqlite3")

if len(sys.argv) != 3:
    print("Использование: python3 create_admin.py <username> <password>")
    sys.exit(1)

username, password = sys.argv[1], sys.argv[2]

if len(username) < 3 or len(password) < 4:
    print("Ошибка: username ≥ 3 символа, пароль ≥ 4 символа")
    sys.exit(1)

os.makedirs(os.path.dirname(DB) if os.path.dirname(DB) else ".", exist_ok=True)
conn = sqlite3.connect(DB)
pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
try:
    conn.execute(
        "INSERT INTO users (username, password_hash, is_admin) VALUES (?,?,1)",
        (username, pw_hash)
    )
    conn.commit()
    print(f"✓ Администратор '{username}' создан.")
except sqlite3.IntegrityError:
    conn.execute("UPDATE users SET is_admin=1, password_hash=? WHERE username=?", (pw_hash, username))
    conn.commit()
    print(f"✓ Пользователь '{username}' обновлён до администратора.")
finally:
    conn.close()

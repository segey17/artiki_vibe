# PhotoRank 📸

Сайт для коллективного оценивания фотографий с тир-листом.

## Стек
- **Бэкенд**: Python + FastAPI
- **БД**: SQLite (WAL-режим, держит 20к+ записей легко)
- **Аутентификация**: JWT + bcrypt
- **Деплой**: Docker / docker-compose

---

## Быстрый старт

### 1. Установите Docker и docker-compose
```bash
# Ubuntu/Debian
curl -fsSL https://get.docker.com | sh
sudo apt install docker-compose-plugin
```

### 2. Клонируйте / распакуйте проект
```bash
cd photorank
```

### 3. Задайте секретный ключ
Откройте `docker-compose.yml` и замените `JWT_SECRET` на случайную строку:
```yaml
JWT_SECRET: СУПЕР_СЕКРЕТНАЯ_СТРОКА_МИНИМУМ_32_СИМВОЛА
```

### 4. Запустите
```bash
docker compose up -d --build
```

Сайт будет доступен на `http://localhost:8000`

---

## Создание администратора

После первого запуска:
```bash
docker compose exec app python3 create_admin.py admin ВАШ_ПАРОЛЬ
```

Или напрямую (если запускаете без Docker):
```bash
pip install bcrypt
python3 create_admin.py admin ВАШ_ПАРОЛЬ
```

---

## Загрузка фотографий

**Вариант 1 — через интерфейс (рекомендуется для < 1000 фото)**
1. Войдите как администратор
2. Нажмите иконку ⚙️ → Панель администратора
3. Перетащите файлы в зону загрузки (поддерживается выбор тысяч файлов)

**Вариант 2 — напрямую в папку (для 20к фото)**
```bash
# Скопируйте все фото в папку photos/
cp /ваша/папка/*.jpg ./photos/
cp /ваша/папка/*.jpg ./photos/

# Затем зарегистрируйте их в БД одной командой:
docker compose exec app python3 -c "
import os, sqlite3, uuid
db = sqlite3.connect('/app/data/db.sqlite3')
photos_dir = '/app/photos'
files = [f for f in os.listdir(photos_dir) if f.lower().endswith(('.jpg','.jpeg','.png','.webp'))]
existing = {r[0] for r in db.execute('SELECT filename FROM photos')}
added = 0
for i, f in enumerate(sorted(files)):
    if f not in existing:
        db.execute('INSERT INTO photos (filename, original_name, position) VALUES (?,?,?)', (f, f, i))
        added += 1
db.commit()
print(f'Добавлено {added} фотографий')
"
```

---

## Процесс работы

1. Пользователи **регистрируются** на сайте
2. Все видят **одну и ту же** фотографию в данный момент
3. Каждый ставит оценку **от 1 до 5 звёзд**
4. **Администратор** переключает фото кнопками «Вперёд / Назад»
5. После оценки всех фото открывается **тир-лист**:
   - **S** — средняя 4.5–5.0 ★
   - **A** — средняя 3.5–4.5 ★
   - **B** — средняя 2.5–3.5 ★
   - **C** — средняя 1.5–2.5 ★
   - **D** — средняя 0–1.5 ★

---

## Деплой на сервер

```bash
# На VPS (Ubuntu)
git clone / scp проект на сервер
cd photorank
docker compose up -d --build

# С доменом — добавьте nginx reverse proxy:
# proxy_pass http://localhost:8000;
```

## Переменные окружения

| Переменная | Описание | По умолчанию |
|---|---|---|
| `JWT_SECRET` | Секрет для подписи токенов | `change-me-in-production-please` |

---

## Структура проекта
```
photorank/
├── backend/
│   ├── main.py          # FastAPI приложение
│   └── requirements.txt
├── frontend/
│   ├── index.html
│   └── static/
│       ├── css/style.css
│       └── js/app.js
├── photos/              # Сюда кладёте фотографии
├── data/                # SQLite БД (создаётся автоматически)
├── create_admin.py      # Скрипт создания админа
├── Dockerfile
└── docker-compose.yml
```

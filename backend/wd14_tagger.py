"""
WD14 auto-tagger — автоматическое проставление тегов на фото с помощью
локальной ONNX-модели SmilingWolf/wd-v1-4-convnextv2-tagger-v2.

Работает полностью офлайн, на CPU, без обращения к каким-либо внешним API.
Модель грузится один раз при первом вызове predict_tag_ids() (lazy load) —
старт сервера не блокируется и не замедляется, если автотегирование вообще
не используется.

Если файлы модели не найдены в MODEL_DIR (см. ниже) или инференс по какой-то
причине не удался — функции тихо возвращают пустой список/None, не бросая
исключение. Вызывающий код (main.py) сам решает, что делать дальше (там это
оборачивается в try/except и просто пропускает тегирование, как договорились:
загрузка фото не должна зависеть от того, доступна модель или нет).
"""
import os
import threading

MODEL_DIR = os.path.join(os.path.dirname(__file__), "wd14_model")
MODEL_PATH = os.path.join(MODEL_DIR, "model.onnx")
TAGS_CSV_PATH = os.path.join(MODEL_DIR, "selected_tags.csv")

# Порог уверенности для general-тегов (category 0). Стандартное значение
# для этой модели — около 0.35; немного завышаем, чтобы автотеги были
# консервативнее ручных (лучше поставить тегов поменьше, но точных).
GENERAL_THRESHOLD = 0.40
# Порог для тегов персонажей (category 4) — у этой категории обычно
# выше уверенность при реальном совпадении, так что порог можно держать строже.
CHARACTER_THRESHOLD = 0.50
# Максимальное число тегов на одно фото — защита от случая, когда модель
# "разрядилась" по шуму и насыпала сотни низкокачественных совпадений.
# Договорились "без жёсткого лимита, сколько подходит" — ставим щедрый
# потолок просто как защиту от патологического случая, а не как реальный cap.
MAX_TAGS_PER_PHOTO = 40

_lock = threading.Lock()
_session = None          # onnxruntime.InferenceSession, грузится лениво
_input_name = None
_input_size = None       # (width, height) ожидаемые моделью
_tag_rows = None         # list[(tag_id:int, name:str, category:int)] из selected_tags.csv
_load_failed = False     # если True — больше не пытаемся грузить повторно в этом процессе


def _try_load_model():
    """Ленивая загрузка модели и её tags-словаря. Возвращает True при успехе."""
    global _session, _input_name, _input_size, _tag_rows, _load_failed

    if _session is not None:
        return True
    if _load_failed:
        return False

    if not os.path.exists(MODEL_PATH) or not os.path.exists(TAGS_CSV_PATH):
        print(f"[wd14_tagger] Модель не найдена в {MODEL_DIR} "
              f"(ожидались model.onnx и selected_tags.csv) — автотегирование отключено.")
        _load_failed = True
        return False

    # Защита от типичной ошибки скачивания: на HuggingFace эти файлы хранятся
    # через Xet/Git-LFS, и при скачивании "не той" ссылкой вместо ~388 МБ можно
    # получить крошечный файл-указатель (символическую ссылку/pointer-файл).
    # Явно проверяем размер, чтобы не упасть на загадочной ошибке ONNX Runtime,
    # а сразу сказать в лог, в чём дело.
    MIN_MODEL_SIZE_BYTES = 50 * 1024 * 1024  # реальная модель весит ~388 МБ, ставим запас вниз
    model_size = os.path.getsize(MODEL_PATH)
    tags_size = os.path.getsize(TAGS_CSV_PATH)
    if model_size < MIN_MODEL_SIZE_BYTES or tags_size < 1024:
        print(f"[wd14_tagger] Файлы модели в {MODEL_DIR} похожи на LFS/Xet-указатели, "
              f"а не на реальные данные (model.onnx={model_size} байт, "
              f"selected_tags.csv={tags_size} байт). Похоже, файлы скачались "
              f"неправильной ссылкой. Скачайте их заново по прямой ссылке вида "
              f".../resolve/main/model.onnx — автотегирование отключено.")
        _load_failed = True
        return False

    try:
        import onnxruntime as ort
        import csv

        sess = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
        inp = sess.get_inputs()[0]
        # ожидается NHWC: (1, H, W, 3)
        _, h, w, _ = inp.shape
        _input_size = (int(w), int(h))
        _input_name = inp.name

        with open(TAGS_CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = [(int(r["tag_id"]), r["name"], int(r["category"])) for r in reader]

        _session = sess
        _tag_rows = rows
        print(f"[wd14_tagger] Модель загружена ({len(rows)} тегов, input {_input_size}).")
        return True
    except Exception as e:
        print(f"[wd14_tagger] Не удалось загрузить модель: {e}")
        _load_failed = True
        return False


def _preprocess(image_path: str):
    """Готовит изображение под вход модели: паддинг до квадрата, ресайз, BGR, NHWC."""
    from PIL import Image
    import numpy as np

    img = Image.open(image_path).convert("RGB")

    # Паддинг до квадрата белым фоном (стандартная подготовка для WD14).
    w, h = img.size
    side = max(w, h)
    padded = Image.new("RGB", (side, side), (255, 255, 255))
    padded.paste(img, ((side - w) // 2, (side - h) // 2))

    target_w, target_h = _input_size
    padded = padded.resize((target_w, target_h), Image.BICUBIC)

    arr = np.asarray(padded, dtype=np.float32)
    arr = arr[:, :, ::-1]  # RGB -> BGR, как ожидает модель
    arr = np.expand_dims(arr, axis=0)  # (1, H, W, 3)
    return arr


def predict_tag_ids(image_path: str) -> list[int]:
    """
    Прогоняет изображение через WD14 и возвращает список tag_id (тех же ID,
    что используются в таблице `tags` — она загружена из идентичного по
    структуре tags.csv, поэтому сопоставление прямое, без маппинга).

    При любой ошибке (модель не скачана, повреждённый файл, и т.п.)
    возвращает пустой список — никогда не бросает исключение наружу.
    """
    with _lock:
        if not _try_load_model():
            return []

    try:
        inp = _preprocess(image_path)
        outputs = _session.run(None, {_input_name: inp})
        probs = outputs[0][0]  # (num_tags,)

        results = []
        for (tag_id, name, category), p in zip(_tag_rows, probs):
            if category == 0 and p >= GENERAL_THRESHOLD:
                results.append((tag_id, float(p)))
            elif category == 4 and p >= CHARACTER_THRESHOLD:
                results.append((tag_id, float(p)))
            # category 9 (rating: general/sensitive/questionable/explicit) сознательно
            # пропускаем — это не предметный тег, а возрастной рейтинг контента.

        results.sort(key=lambda x: x[1], reverse=True)
        return [tag_id for tag_id, _ in results[:MAX_TAGS_PER_PHOTO]]
    except Exception as e:
        print(f"[wd14_tagger] Ошибка инференса для {image_path}: {e}")
        return []


def is_available() -> bool:
    """Не пытается загрузить модель в ONNX Runtime — но проверяет не только
    наличие файлов на диске, а и их реальный размер (см. комментарий в
    _try_load_model про LFS/Xet-указатели). Используется в /api/admin для
    показа статуса в админ-панели."""
    if not (os.path.exists(MODEL_PATH) and os.path.exists(TAGS_CSV_PATH)):
        return False
    try:
        return os.path.getsize(MODEL_PATH) >= 50 * 1024 * 1024 and os.path.getsize(TAGS_CSV_PATH) >= 1024
    except OSError:
        return False

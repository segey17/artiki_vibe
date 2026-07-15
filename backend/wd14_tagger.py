"""
WD14 auto-tagger — автоматическое проставление тегов на фото с помощью
локальной ONNX-модели семейства SmilingWolf/wd-*-tagger-v3 (ViT/SwinV2/
EVA02 — любая модель этого семейства, скачанная в MODEL_DIR, подходит:
формат входа/выхода и csv со списком тегов одинаковы у всех). v3-серия
обучена на более новом датасете с актуальным на 2024 год словарём тегов —
если раньше стояла модель v2 (ConvNextV2), после замены файлов модели
на v3 код продолжает работать без изменений.

Работает полностью офлайн, на CPU, без обращения к каким-либо внешним API.
Модель грузится один раз при первом вызове predict_tag_ids()/predict_tag_ids_batch()
(lazy load) — старт сервера не блокируется и не замедляется, если
автотегирование вообще не используется.

Массовая обработка (импорт/синхронизация с диска, сканирование папки) идёт
через predict_tag_ids_batch() — вместо того чтобы прогонять фото по одному
через модель (что на CPU почти целиком уходит в накладные расходы на каждый
вызов), картинки собираются в батч и прогоняются через модель одним проходом
— на типичном сервере это заметно быстрее суммарного времени на партию из
многих фото.

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
# Можно переопределить через переменные окружения — разным моделям семейства
# (ViT/SwinV2/EVA02) авторы рекомендуют слегка разные пороги.
GENERAL_THRESHOLD = float(os.getenv("WD14_GENERAL_THRESHOLD", "0.40"))
# Порог для тегов персонажей (category 4) — у этой категории обычно
# выше уверенность при реальном совпадении, так что порог можно держать строже.
CHARACTER_THRESHOLD = float(os.getenv("WD14_CHARACTER_THRESHOLD", "0.50"))
# Максимальное число тегов на одно фото — защита от случая, когда модель
# "разрядилась" по шуму и насыпала сотни низкокачественных совпадений.
# Договорились "без жёсткого лимита, сколько подходит" — ставим щедрый
# потолок просто как защиту от патологического случая, а не как реальный cap.
MAX_TAGS_PER_PHOTO = 40
# Сколько фото прогонять за один проход через модель при массовой обработке
# (импорт, синхронизация, сканирование папки). Больше — эффективнее использует
# CPU за проход, но и больше оперативной памяти на батч; 8 — разумный баланс
# для типичного сервера без GPU.
BATCH_SIZE = int(os.getenv("WD14_BATCH_SIZE", "8"))

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
    # через Xet/Git-LFS, и при скачивании "не той" ссылкой вместо ~380-1300 МБ
    # (зависит от конкретной модели — ViT легче, EVA02-Large тяжелее) можно
    # получить крошечный файл-указатель (символическую ссылку/pointer-файл).
    # Явно проверяем размер, чтобы не упасть на загадочной ошибке ONNX Runtime,
    # а сразу сказать в лог, в чём дело.
    MIN_MODEL_SIZE_BYTES = 50 * 1024 * 1024  # даже самая лёгкая модель весит на порядок больше
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

        sess_options = ort.SessionOptions()
        # Явно отдаём модели все доступные ядра под внутрипроходный параллелизм
        # (по умолчанию ONNX Runtime тоже старается использовать все ядра, но
        # явное значение надёжнее — особенно в контейнере, где auto-detect числа
        # ядер иногда работает не так, как хотелось бы) + включаем полный набор
        # графовых оптимизаций (слияние узлов и т.п.), что даёт небольшой, но
        # бесплатный прирост скорости инференса.
        sess_options.intra_op_num_threads = max(1, os.cpu_count() or 1)
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        sess = ort.InferenceSession(MODEL_PATH, sess_options=sess_options, providers=["CPUExecutionProvider"])
        inp = sess.get_inputs()[0]
        # ожидается NHWC: (batch, H, W, 3); batch может быть динамическим (None/-1)
        # у моделей v3 — в отличие от v2, где он был жёстко зафиксирован как 1,
        # что и позволяет прогонять несколько фото за один проход (см. predict_tag_ids_batch).
        _, h, w, _ = inp.shape
        _input_size = (int(w), int(h))
        _input_name = inp.name

        with open(TAGS_CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = [(int(r["tag_id"]), r["name"], int(r["category"])) for r in reader]

        _session = sess
        _tag_rows = rows
        print(f"[wd14_tagger] Модель загружена ({len(rows)} тегов, input {_input_size}, "
              f"потоков: {sess_options.intra_op_num_threads}).")
        return True
    except Exception as e:
        print(f"[wd14_tagger] Не удалось загрузить модель: {e}")
        _load_failed = True
        return False


def _preprocess(image_path: str):
    """Готовит изображение под вход модели: паддинг до квадрата, ресайз, BGR, HWC (без batch-оси)."""
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
    return arr  # (H, W, 3), без batch-оси — её добавляет вызывающий код


def _tags_from_probs(probs) -> list[int]:
    """Общая логика отбора тегов по порогу — используется и для одиночного,
    и для батчевого инференса, чтобы не дублировать её дважды."""
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


def predict_tag_ids(image_path: str) -> list[int]:
    """
    Прогоняет ОДНО изображение через WD14 и возвращает список tag_id (тех же
    ID, что используются в таблице `tags` — она загружена из идентичного по
    структуре tags.csv, поэтому сопоставление прямое, без маппинга).

    Для массовой обработки нескольких фото используйте predict_tag_ids_batch —
    один вызов на пачку картинок эффективнее, чем эта функция в цикле.

    При любой ошибке (модель не скачана, повреждённый файл и т.п.)
    возвращает пустой список — никогда не бросает исключение наружу.
    """
    return predict_tag_ids_batch([image_path])[0]


def predict_tag_ids_batch(image_paths: list[str]) -> list[list[int]]:
    """
    Прогоняет СПИСОК изображений через WD14 одним (или несколькими, см.
    BATCH_SIZE) проходами модели вместо вызова модели по одному фото за раз —
    на CPU это существенно быстрее суммарно для партии из многих фото, так
    как амортизирует накладные расходы на вызов модели по всей пачке сразу.

    Важно: и предобработка, и сам инференс идут ПОРЦИЯМИ по BATCH_SIZE
    изображений за раз (не грузим в память сразу все изображения партии).
    При сканировании папки/импорте с тысячами новых файлов предобработка
    "всё и сразу" держала бы в памяти по несколько мегабайт на каждое
    изображение одновременно — на партии в тысячи файлов это легко уходит
    за пределы доступной памяти сервера и роняет процесс. Потоковая обработка
    по чанкам держит в памяти не больше одной порции сразу, независимо от
    того, сколько всего фото в партии — хоть 50, хоть 50000.

    Возвращает список результатов в ТОМ ЖЕ порядке, что и image_paths;
    для файла, который не удалось прочитать как изображение, элемент —
    пустой список (а не пропуск позиции — длина результата всегда равна
    длине image_paths).
    """
    with _lock:
        if not _try_load_model():
            return [[] for _ in image_paths]

    import numpy as np

    results = [[] for _ in image_paths]

    for chunk_start in range(0, len(image_paths), BATCH_SIZE):
        chunk_paths = image_paths[chunk_start:chunk_start + BATCH_SIZE]

        # Предобработка — только текущей порции, а не всей партии разом.
        prepared = []  # (index_in_input, np.array HWC) — только успешно прочитанные
        for offset, path in enumerate(chunk_paths):
            try:
                prepared.append((chunk_start + offset, _preprocess(path)))
            except Exception as e:
                print(f"[wd14_tagger] Не удалось прочитать {path}: {e}")

        if not prepared:
            continue

        try:
            batch_arr = np.stack([arr for _, arr in prepared], axis=0)  # (N, H, W, 3)
            outputs = _session.run(None, {_input_name: batch_arr})
            probs_batch = outputs[0]  # (N, num_tags)
            for (orig_idx, _), probs in zip(prepared, probs_batch):
                results[orig_idx] = _tags_from_probs(probs)
        except Exception as e:
            print(f"[wd14_tagger] Ошибка батч-инференса ({len(prepared)} фото): {e}")
            # оставляем results[...] пустыми для этого чанка — уже не хуже,
            # чем если бы автотегирование было недоступно вовсе

    return results


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

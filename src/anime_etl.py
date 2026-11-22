import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import psycopg2
import requests
from dotenv import dotenv_values
from psycopg2.extras import Json, execute_values
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("anime_etl")


@dataclass(frozen=True)
class Settings:
    dbname: str
    user: str
    password: str = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 5433
    api_base_url: str = "https://api.nekosapi.com/v4"
    page_size: int = 100
    max_records: int = 500
    delay: float = 0.5
    timeout: float = 30
    user_agent: str = "etl-anime (https://github.com/hikkamisama/etl-anime)"

    def __post_init__(self):
        if not 1 <= self.page_size <= 100 or self.max_records < 1:
            raise ValueError("API_PAGE_SIZE должен быть 1–100; API_MAX_RECORDS > 0.")
        if self.delay < 0 or self.timeout <= 0 or not 1 <= self.port <= 65535:
            raise ValueError("Проверьте задержку, timeout и порт в .env.")
        if not valid_url(self.api_base_url) or not self.password:
            raise ValueError("Проверьте API_BASE_URL и POSTGRES_PASSWORD в .env.")

    @classmethod
    def from_env(cls, path: Path):
        if not path.is_file():
            raise FileNotFoundError("Создайте .env из .env.example в корне проекта.")
        values = dotenv_values(path)
        required = ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
        missing = [key for key in required if not values.get(key)]
        if missing:
            raise ValueError(f"В .env не заданы: {', '.join(missing)}")
        return cls(
            dbname=values["POSTGRES_DB"],
            user=values["POSTGRES_USER"],
            password=values["POSTGRES_PASSWORD"],
            host=values.get("POSTGRES_HOST", "127.0.0.1"),
            port=int(values.get("POSTGRES_PORT", "5433")),
            api_base_url=values.get("API_BASE_URL", cls.api_base_url).rstrip("/"),
            page_size=int(values.get("API_PAGE_SIZE", "100")),
            max_records=int(values.get("API_MAX_RECORDS", "500")),
            delay=float(values.get("API_REQUEST_DELAY", "0.5")),
            timeout=float(values.get("API_TIMEOUT", "30")),
            user_agent=values.get("API_USER_AGENT", cls.user_agent),
        )


def valid_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def make_session(settings):
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.headers.update(
        {"User-Agent": settings.user_agent, "Accept": "application/json"}
    )
    return session


def extract(settings, raw_dir, session):
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    records, offset = [], 0
    seen_ids = set()
    while len(records) < settings.max_records:
        params = {
            "rating": "safe",
            "limit": min(settings.page_size, settings.max_records - len(records)),
            "offset": offset,
        }
        try:
            response = session.get(
                f"{settings.api_base_url}/images",
                params=params,
                timeout=settings.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.error(
                "Extract: не удалось получить offset=%s (%s)",
                offset,
                type(exc).__name__,
            )
            raise RuntimeError(
                f"Ошибка API на offset={offset}; проверьте доступность источника."
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise TypeError("Схема API изменилась: ожидается объект с массивом items.")
        items = payload["items"]
        if not all(
            isinstance(item, dict) and type(item.get("id")) is int for item in items
        ):
            raise ValueError("Схема API изменилась: нужна другая обработка айтема")
        (raw_dir / f"offset_{offset:06d}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(
            "Extract: offset=%s, получено=%s, доступно=%s",
            offset,
            len(items),
            payload.get("count"),
        )
        if not items:
            break
        ids = {item["id"] for item in items}
        if ids <= seen_ids:
            raise RuntimeError(
                "API повторяет уже полученную страницу; загрузка остановлена."
            )
        seen_ids.update(ids)
        records.extend(items[: settings.max_records - len(records)])
        offset += len(items)
        count = payload.get("count")
        if type(count) is int and offset >= count:
            break
        if len(records) < settings.max_records:
            time.sleep(settings.delay)
    if not records:
        raise ValueError("API вернул пустую выборку; таблица не изменена.")
    return records


def rgb_valid(value):
    return (
        isinstance(value, list)
        and len(value) == 3
        and all(type(channel) is int and 0 <= channel <= 255 for channel in value)
    )


def optional_text(value):
    if isinstance(value, str):
        return value.strip() or None
    return None


def transform(records, extracted_at):
    rows = []
    for record in records:
        image_id = record.get("id")
        if (
            type(image_id) is not int
            or image_id <= 0
            or not valid_url(record.get("url"))
        ):
            continue
        tags = record.get("tags")
        tags = sorted(
            {
                tag.strip().lower()
                for tag in (tags if isinstance(tags, list) else [])
                if isinstance(tag, str) and tag.strip()
            }
        )
        dominant = record.get("color_dominant")
        palette = record.get("color_palette")
        palette = [
            color
            for color in (palette if isinstance(palette, list) else [])
            if rgb_valid(color)
        ]
        rows.append(
            {
                "image_id": image_id,
                "image_url": record["url"],
                "rating": record["rating"],
                "artist_name": optional_text(record.get("artist_name")),
                "source_url": record.get("source_url")
                if valid_url(record.get("source_url"))
                else None,
                "tags": tags,
                "dominant_color_hex": "#{:02x}{:02x}{:02x}".format(*dominant)
                if rgb_valid(dominant)
                else None,
                "color_palette": palette,
                "extracted_at": extracted_at,
            }
        )
    if not rows:
        raise ValueError(
            "После очистки не осталось корректных записей; таблица не изменена."
        )
    frame = (
        pd.DataFrame(rows)
        .drop_duplicates("image_id", keep="last")
        .reset_index(drop=True)
    )
    frame["image_id"] = frame["image_id"].astype("int64")
    frame["extracted_at"] = pd.to_datetime(frame["extracted_at"], utc=True)
    logger.info(
        "Transform: вход=%s, результат=%s, отброшено/дубли=%s",
        len(records),
        len(frame),
        len(records) - len(frame),
    )
    return frame


@contextmanager
def connect(settings):
    connection = None
    try:
        connection = psycopg2.connect(
            dbname=settings.dbname,
            user=settings.user,
            password=settings.password,
            host=settings.host,
            port=settings.port,
            connect_timeout=10,
        )
        yield connection
    except psycopg2.Error as exc:
        logger.error("PostgreSQL: %s (SQLSTATE=%s)", type(exc).__name__, exc.pgcode)
        raise RuntimeError(
            "Ошибка PostgreSQL: проверьте docker compose ps, .env и init/init.sql."
        ) from exc
    finally:
        if connection is not None:
            connection.close()


def load(frame, settings):
    values = [
        (
            int(row.image_id),
            row.image_url,
            row.rating,
            row.artist_name,
            row.source_url,
            row.tags,
            row.dominant_color_hex,
            Json(row.color_palette),
            row.extracted_at.to_pydatetime(),
        )
        for row in frame.itertuples(index=False)
    ]
    with connect(settings) as connection, connection, connection.cursor() as cursor:
        inserted = execute_values(
            cursor,
            """
                INSERT INTO anime.images
                    (image_id, image_url, rating, artist_name, source_url, tags,
                     dominant_color_hex, color_palette, extracted_at)
                VALUES %s ON CONFLICT (image_id) DO NOTHING RETURNING image_id
            """,
            values,
            page_size=100,
            fetch=True,
        )
    logger.info(
        "Load: вставлено=%s, уже существовало=%s",
        len(inserted),
        len(values) - len(inserted),
    )
    return len(inserted)


def verify(settings):
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*), COUNT(DISTINCT image_id) FROM anime.images")
        total, unique = cursor.fetchone()
        cursor.execute("""
            SELECT image_id, rating, artist_name, tags, dominant_color_hex, image_url
            FROM anime.images ORDER BY image_id LIMIT 5
        """)
        sample = pd.DataFrame(
            cursor.fetchall(), columns=[col.name for col in cursor.description]
        )
    if not total or total != unique:
        raise AssertionError("Проверка количества/уникальности записей не прошла.")
    return total, sample

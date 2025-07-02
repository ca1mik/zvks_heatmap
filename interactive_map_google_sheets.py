"""
Скрипт: interactive_map_google_sheets.py

• Ежедневно забирает данные из Google Sheets
• Кэширует геокодирование, чтобы не вызывать Nominatim лишний раз
• Будет строить интерактивную карту (Folium) с:
    – температурной картой (HeatMap)
    – кластерными маркерами по категориям (вода, канализация и т.д.)
• На вход принимает диапазон дат и список категорий — можно вызвать из cron / GitHub Actions.

Перед использованием:
1. Создайте сервис‑аккаунт в Google Cloud, скачайте JSON‑ключ и назовите его service_account.json.
2. Поделитесь таблицей с адресом сервис‑аккаунта (share -> Editor).
3. Укажите ID таблицы в переменной GOOGLE_SHEET_ID.
4. Убедитесь, что в таблице есть колонки:
   "created_at" (дата/время заявки),
   "category" (тип работ),
   "Улица", "Дом", "Всего" (кол‑во заявок).  При необходимости переименуйте ниже.
5. pip install -U gspread gspread_dataframe oauth2client pandas geopy folium

Запуск примера:
python interactive_map_google_sheets.py \
       --from 2025-06-01 --to 2025-06-30 --cat вода канализация

Создаст файл map_2025-06-01_2025-06-30.html рядом со скриптом.
"""

import argparse
import datetime as dt
import os
import pickle
from pathlib import Path
from typing import List, Optional

import folium
import gspread
import pandas as pd
from folium.plugins import HeatMap, MarkerCluster, MiniMap, Fullscreen
from geopy.distance import geodesic
from geopy.extra.rate_limiter import RateLimiter
from geopy.geocoders import Nominatim
from gspread_dataframe import get_as_dataframe

# ─── 1) Настройки — поправьте под себя ─────────────────────────────────────────
GOOGLE_SHEET_ID = "PASTE_SHEET_ID_HERE"  # 👉 Ваш ID таблицы (последняя часть URL)
SERVICE_ACCOUNT_JSON = "service_account.json"  # 👉 Путь к JSON‑ключу
WORKSHEET_NAME: Optional[str] = None  # None = первый лист

CENTER = (55.8437, 48.5066)            # Центр карты (Зеленодольск)
MAX_DIST_KM = 10                        # Отсечка от чужих адресов
GEOCACHE_FILE = "geocache.pkl"         # Куда сохраняем координаты
OUTPUT_PATTERN = "map_{from_d}_{to_d}.html"  # Шаблон имени файла

# Названия колонок в Google Sheets (можно поменять, если у вас другие)
COL_STREET = "Улица"
COL_HOUSE = "Дом"
COL_COUNT = "Всего"
COL_DATE = "created_at"
COL_CAT = "category"

# ─── 2) Подключение к Google Sheets ────────────────────────────────────────────

def fetch_sheet_df(sheet_id: str, cred_json: str, worksheet_name: Optional[str]) -> pd.DataFrame:
    """Считываем лист Google Sheets в DataFrame"""
    gc = gspread.service_account(filename=cred_json)
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(worksheet_name) if worksheet_name else sh.sheet1
    df = get_as_dataframe(ws, evaluate_formulas=True, header=0)
    return df

# ─── 3) Геокодирование с кэшем ────────────────────────────────────────────────

def load_cache(path: str):
    if Path(path).exists():
        with open(path, "rb") as f:
            return pickle.load(f)
    return {}

def save_cache(cache: dict, path: str):
    with open(path, "wb") as f:
        pickle.dump(cache, f)

def smart_geocode(street: str, house: str, geocode_fn, cache: dict):
    key = f"{street}_{house}"
    if key in cache:
        return cache[key]

    address = f"{street}, {house}, Зеленодольск, Республика Татарстан, Россия"
    loc = geocode_fn(address)
    if loc and geodesic(CENTER, (loc.latitude, loc.longitude)).km <= MAX_DIST_KM:
        cache[key] = (loc.latitude, loc.longitude)
    else:
        cache[key] = None
    return cache[key]

# ─── 4) Подготовка данных ─────────────────────────────────────────────────────

def prepare_dataframe(df_raw: pd.DataFrame) -> pd.DataFrame:
    # убираем пустые улицы/дома
    df = df_raw.copy()
    df = df[df[COL_STREET].notna() & df[COL_HOUSE].notna()]

    # приведение типов
    df[COL_STREET] = df[COL_STREET].astype(str)
    df[COL_HOUSE] = df[COL_HOUSE].astype(str)
    df[COL_COUNT] = df[COL_COUNT].fillna(0).astype(int)
    df[COL_DATE] = pd.to_datetime(df[COL_DATE])
    df[COL_CAT] = df[COL_CAT].fillna("Не указано")
    return df

# ─── 5) Фильтрация по дате и категориям ───────────────────────────────────────

def filter_df(df: pd.DataFrame, date_from: dt.date, date_to: dt.date, cats: Optional[List[str]]):
    mask = (df[COL_DATE] >= pd.Timestamp(date_from)) & (df[COL_DATE] <= pd.Timestamp(date_to) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1))
    if cats:
        mask &= df[COL_CAT].isin(cats)
    return df[mask].reset_index(drop=True)

# ─── 6) Построение карты ─────────────────────────────────────────────────────

def build_map(df: pd.DataFrame) -> folium.Map:
    m = folium.Map(location=CENTER, zoom_start=13, tiles="CartoDB Positron", control_scale=True)
    MiniMap(toggle_display=True).add_to(m)
    Fullscreen().add_to(m)

    # Тепловая карта
    if not df.empty:
        heat_data = df[["lat", "lon", COL_COUNT]].values.tolist()
        HeatMap(heat_data, radius=20, blur=15, min_opacity=0.3).add_to(folium.FeatureGroup("🔥 HeatMap", show=True).add_to(m))

    # Кластеры по категориям
    clusters = {}
    for cat in df[COL_CAT].unique():
        fg = folium.FeatureGroup(cat, show=False).add_to(m)
        clusters[cat] = MarkerCluster().add_to(fg)

    for _, r in df.iterrows():
        mc = clusters[r[COL_CAT]]
        folium.Marker(
            [r.lat, r.lon],
            popup=f"{r[COL_STREET]} {r[COL_HOUSE]}<br>{r[COL_CAT]}<br>{r[COL_COUNT]} шт.<br>{r[COL_DATE].date()}",
            icon=folium.Icon(color="blue", icon="info-sign"),
        ).add_to(mc)

    # Подгоняем зум под все точки
    if not df.empty:
        sw = [df.lat.min(), df.lon.min()]
        ne = [df.lat.max(), df.lon.max()]
        m.fit_bounds([sw, ne], padding=(50, 50))

    folium.LayerControl(collapsed=False).add_to(m)
    return m

# ─── 7) Основной процесс ──────────────────────────────────────────────────────

def generate_map(date_from: str, date_to: str, categories: Optional[List[str]] = None, worksheet_name: Optional[str] = WORKSHEET_NAME):
    raw_df = fetch_sheet_df(GOOGLE_SHEET_ID, SERVICE_ACCOUNT_JSON, worksheet_name)
    df = prepare_dataframe(raw_df)

    # Геокодируем с кэшем
    cache = load_cache(GEOCACHE_FILE)
    geolocator = Nominatim(user_agent="zelenod_map")
    geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1)
    coords = df.apply(lambda r: smart_geocode(r[COL_STREET], r[COL_HOUSE], geocode, cache), axis=1)
    df["lat"] = coords.apply(lambda c: c[0] if c else None)
    df["lon"] = coords.apply(lambda c: c[1] if c else None)
    save_cache(cache, GEOCACHE_FILE)

    df = df.dropna(subset=["lat", "lon"])

    df_filt = filter_df(df, dt.date.fromisoformat(date_from), dt.date.fromisoformat(date_to), categories)

    m = build_map(df_filt)
    out_name = OUTPUT_PATTERN.format(from_d=date_from, to_d=date_to)
    m.save(out_name)
    print(f"✅ Карта сохранена в {out_name} — {len(df_filt)} точек")

# ─── 8) CLI ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Генерация интерактивной карты заявок из Google Sheets")
    parser.add_argument("--from", "-f", dest="d_from", required=True, help="Начальная дата YYYY-MM-DD")
    parser.add_argument("--to", "-t", dest="d_to", required=True, help="Конечная дата YYYY-MM-DD")
    parser.add_argument("--cat", "-c", dest="cats", nargs="*", help="Список категорий через пробел (по умолчанию все)")
    parser.add_argument("--sheet", "-s", dest="sheet", help="Название листа (по умолчанию первый)")

    args = parser.parse_args()
    generate_map(args.d_from, args.d_to, args.cats, args.sheet)

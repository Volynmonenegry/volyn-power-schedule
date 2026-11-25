import argparse
import datetime
import io
import json
import os
import re
import sys
import time
import requests
from PIL import Image, ImageDraw, ImageFont

try:
    from zoneinfo import ZoneInfo
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except Exception:
    KYIV_TZ = None

# --- НАЛАШТУВАННЯ TELEGRAM ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "9095679047:AAHM6iBir2yhMWtro88qQ7TlbR0xWEkVNRk").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1004351416914").strip()
STATE_FILE = "state.json"

# --- НАЛАШТУВАННЯ API ВОЛИНЬОБЛЕНЕРГО ---
API_BASE_URL = "https://api-voe-poweron.inneti.net"
GPV_OPTION_KEYS = [
    "pw_gpv_image_today",
    "pw_gpv_image_tomorrow",
    "gpv_image_today",
    "gpv_image_tomorrow",
]

# --- НАЛАШТУВАННЯ ТАБЛИЦІ ---
CELL_WIDTH = 28
CELL_HEIGHT = 36
NUM_QUEUES = 12
NUM_INTERVALS = 48

MARGIN_LEFT = 90
MARGIN_TOP = 110
MARGIN_RIGHT = 140
MARGIN_BOTTOM = 250

IMG_WIDTH = MARGIN_LEFT + (NUM_INTERVALS * CELL_WIDTH) + MARGIN_RIGHT
IMG_HEIGHT = MARGIN_TOP + (NUM_QUEUES * CELL_HEIGHT) + MARGIN_BOTTOM

# Початкові кольори
COLOR_BG = (255, 255, 255)
COLOR_GRID = (200, 200, 200)
COLOR_TEXT = (30, 30, 30)
COLOR_TEXT_LIGHT = (120, 120, 120)
COLOR_POWER_ON = (232, 245, 233)
COLOR_POWER_OFF = (76, 175, 80)

# Кольори штрихування для змін
COLOR_ADDED_OFF = (56, 142, 60)
COLOR_REMOVED_OFF = (200, 230, 201)
COLOR_HATCH_DARK = (27, 94, 32)
COLOR_HATCH_LIGHT = (129, 199, 132)

# Смужки оновлення та поточного часу
COLOR_SITE_UPDATE_LINE = (234, 179, 8)
COLOR_NOW_LINE = (34, 197, 94)

queues = ["1.1", "1.2", "2.1", "2.2", "3.1", "3.2", "4.1", "4.2", "5.1", "5.2", "6.1", "6.2"]

DEBUG_QUEUES = {
    "1.1": "08:00-11:00, 18:00-21:00",
    "2.2": "12:00-15:30",
    "4.1": "00:00-02:00, 14:00-16:00",
}


def get_now():
    if KYIV_TZ is not None:
        return datetime.datetime.now(KYIV_TZ).replace(tzinfo=None)
    return datetime.datetime.now()


def parse_iso_to_kyiv_naive(iso_str):
    if not iso_str:
        return None
    try:
        dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            if KYIV_TZ is not None:
                dt = dt.astimezone(KYIV_TZ)
            return dt.replace(tzinfo=None)
        return dt
    except Exception:
        return None


def serialize_data(data_dict):
    if data_dict is None:
        return None
    return {f"{r}:{c}": bool(v) for (r, c), v in data_dict.items() if v}


def deserialize_data(raw_dict):
    if raw_dict is None:
        return None
    result = {}
    for key, val in raw_dict.items():
        if val:
            r_str, c_str = key.split(":")
            result[(int(r_str), int(c_str))] = True
    return result


def load_state(path=STATE_FILE, schedule_date=None):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        target_date = (schedule_date or get_now().date()).isoformat()
        if payload.get("date") != target_date:
            return None
        return deserialize_data(payload.get("data"))
    except Exception as e:
        print(f"⚠️ Не вдалося прочитати {path}: {e}")
        return None


def save_state(data_dict, path=STATE_FILE, schedule_date=None):
    target_date = (schedule_date or get_now().date()).isoformat()
    payload = {
        "date": target_date,
        "data": serialize_data(data_dict),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def draw_hatched_rect(draw, coords, base_color, hatch_color, step=8):
    x1, y1, x2, y2 = coords
    draw.rectangle([x1, y1, x2, y2], fill=base_color, outline=COLOR_GRID, width=1)

    width = int(x2 - x1)
    height = int(y2 - y1)
    if width <= 0 or height <= 0:
        return

    hatch_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    h_draw = ImageDraw.Draw(hatch_img)
    rgba_hatch = (*hatch_color, 210)

    for offset in range(-height, width + height, step):
        h_draw.line(
            [(offset, height), (offset + height, 0)],
            fill=rgba_hatch,
            width=2,
        )

    base_img = Image.new("RGBA", (width, height), (*base_color, 255))
    combined = Image.alpha_composite(base_img, hatch_img).convert("RGB")
    draw._image.paste(combined, (int(x1), int(y1)))
    draw.rectangle([x1, y1, x2, y2], outline=COLOR_GRID, width=1)


def time_to_x(dt_obj, schedule_date):
    if dt_obj.date() < schedule_date:
        return float(MARGIN_LEFT)
    if dt_obj.date() > schedule_date:
        return float(MARGIN_LEFT + NUM_INTERVALS * CELL_WIDTH)

    minutes_from_midnight = dt_obj.hour * 60 + dt_obj.minute + dt_obj.second / 60.0
    slots = minutes_from_midnight / 30.0
    slots = max(0.0, min(float(NUM_INTERVALS), slots))
    return MARGIN_LEFT + slots * CELL_WIDTH


def classify_cells(current_data, prev_data):
    cell_states = {}
    for row in range(NUM_QUEUES):
        for col in range(NUM_INTERVALS):
            key = (row, col)
            now_off = bool(current_data.get(key, False))

            if prev_data is None:
                cell_states[key] = "off" if now_off else "on"
            else:
                was_off = bool(prev_data.get(key, False))
                if now_off and not was_off:
                    cell_states[key] = "added_off"
                elif not now_off and was_off:
                    cell_states[key] = "removed_off"
                elif now_off:
                    cell_states[key] = "off"
                else:
                    cell_states[key] = "on"
    return cell_states


def count_main_queues_off_in_slot(cell_states, col_idx):
    active_groups = set()
    for row in range(NUM_QUEUES):
        if cell_states[(row, col_idx)] in ("off", "added_off"):
            main_group = queues[row].split(".")[0]
            active_groups.add(main_group)
    return len(active_groups)


def load_fonts():
    font_candidates = [
        ("segoeui.ttf", "segoeuib.ttf"),
        ("arial.ttf", "arialbd.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for reg_path, bold_path in font_candidates:
        try:
            return (
                ImageFont.truetype(reg_path, 14),
                ImageFont.truetype(bold_path, 16),
                ImageFont.truetype(bold_path, 20),
                ImageFont.truetype(bold_path, 28),
                ImageFont.truetype(bold_path, 34),
            )
        except IOError:
            continue
    default_font = ImageFont.load_default()
    return default_font, default_font, default_font, default_font, default_font


def create_schedule_image(
    power_off_data,
    prev_data=None,
    site_update_dt=None,
    schedule_date=None,
    output_path=None,
    no_outages=False,
):
    print("Починаємо малювати картинку...")
    now = get_now()
    schedule_date = schedule_date or now.date()
    update_dt = site_update_dt or now

    img = Image.new("RGB", (IMG_WIDTH, IMG_HEIGHT), COLOR_BG)
    draw =

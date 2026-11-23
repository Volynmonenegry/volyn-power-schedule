import argparse
import datetime
import json
import os
import re
import sys
import time
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

try:
    from zoneinfo import ZoneInfo
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except Exception:
    KYIV_TZ = None

# --- НАЛАШТУВАННЯ TELEGRAM ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "9095679047:AAHM6iBir2yhMWtro88qQ7TlbR0xWEkVNRk").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1004351416914").strip()
STATE_FILE = "state.json"

# --- НАЛАШТУВАННЯ ДЛЯ ПОШУКУ ---
USER_CITY = "Луцьк"
USER_STREET = ""

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

# Кольори штрихування для змін протягом дня (лише коли є попередній графік prev_data)
COLOR_ADDED_OFF = (56, 142, 60)       # Додано відключення
COLOR_REMOVED_OFF = (200, 230, 201)   # Прибрано відключення
COLOR_HATCH_DARK = (27, 94, 32)
COLOR_HATCH_LIGHT = (129, 199, 132)

# Смужки оновлення та поточного часу
COLOR_SITE_UPDATE_LINE = (234, 179, 8)  # Жовта мітка/смужка — останнє оновлення на сайті
COLOR_NOW_LINE = (34, 197, 94)          # Зелена смужка — поточний момент часу

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


def load_state(path=STATE_FILE):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        today_str = get_now().date().isoformat()
        if payload.get("date") != today_str:
            return None
        return deserialize_data(payload.get("data"))
    except Exception as e:
        print(f"⚠️ Не вдалося прочитати {path}: {e}")
        return None


def save_state(data_dict, path=STATE_FILE):
    payload = {
        "date": get_now().date().isoformat(),
        "data": serialize_data(data_dict),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def draw_hatched_rect(draw, coords, base_color, hatch_color, step=8):
    """Малює клітинку з діагональним штрихуванням."""
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
    """Переводить час доби у координату X на сітці (0..48 інтервалів)."""
    if dt_obj.date() < schedule_date:
        return float(MARGIN_LEFT)
    if dt_obj.date() > schedule_date:
        return float(MARGIN_LEFT + NUM_INTERVALS * CELL_WIDTH)

    minutes_from_midnight = dt_obj.hour * 60 + dt_obj.minute + dt_obj.second / 60.0
    slots = minutes_from_midnight / 30.0
    slots = max(0.0, min(float(NUM_INTERVALS), slots))
    return MARGIN_LEFT + slots * CELL_WIDTH


def classify_cells(current_data, prev_data):
    """
    Повертає словник {(row, col): status}:
    - 'off'
    - 'on'
    - 'added_off'   (тільки коли prev_data існує і відключення додалося)
    - 'removed_off' (тільки коли prev_data існує і відключення прибрали)
    """
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
    """
    Рахує кількість основних черг (за першою цифрою: 1..6), які вимкнені у вказаному слоті.
    Наприклад, якщо вимкнені 5.1 та 5.2 (або лише 5.1) — це рахується як 1 черга (група '5').
    """
    active_groups = set()
    for row in range(NUM_QUEUES):
        if cell_states[(row, col_idx)] in ("off", "added_off"):
            main_group = queues[row].split(".")[0]
            active_groups.add(main_group)
    return len(active_groups)


def load_fonts():
    font_candidates = [
        ("segoeui.ttf", "segoeuib.ttf"),
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
    draw = ImageDraw.Draw(img)

    font_small, font_bold, font_title, font_logo, font_banner = load_fonts()

    effective_data = {} if no_outages else (power_off_data or {})
    cell_states = classify_cells(effective_data, prev_data)

    # Малюємо клітинки
    for row in range(NUM_QUEUES):
        for col in range(NUM_INTERVALS):
            x1 = MARGIN_LEFT + (col * CELL_WIDTH)
            y1 = MARGIN_TOP + (row * CELL_HEIGHT)
            x2 = x1 + CELL_WIDTH
            y2 = y1 + CELL_HEIGHT

            state = cell_states[(row, col)]
            if state == "off":
                draw.rectangle([x1, y1, x2, y2], fill=COLOR_POWER_OFF, outline=COLOR_GRID, width=1)
            elif state == "added_off":
                draw_hatched_rect(draw, (x1, y1, x2, y2), COLOR_ADDED_OFF, COLOR_HATCH_DARK)
            elif state == "removed_off":
                draw_hatched_rect(draw, (x1, y1, x2, y2), COLOR_REMOVED_OFF, COLOR_HATCH_LIGHT)
            else:
                draw.rectangle([x1, y1, x2, y2], fill=COLOR_POWER_ON, outline=COLOR_GRID, width=1)

    # Логотип і дата/час оновлення на самій картинці
    logo_text = "Волинь Вимкнення"
    draw.text((25, 35), logo_text, fill=COLOR_POWER_OFF, font=font_logo, anchor="lm")

    bbox = draw.textbbox((0, 0), logo_text, font=font_logo)
    bolt_x = 25 + (bbox[2] - bbox[0]) + 12
    bolt_points = [
        (bolt_x + 10, 18), (bolt_x + 20, 18), (bolt_x + 10, 30),
        (bolt_x + 22, 30), (bolt_x, 48), (bolt_x + 8, 34), (bolt_x - 2, 34),
    ]
    draw.polygon(bolt_points, fill=COLOR_POWER_OFF)

    update_badge_text = f"Оновлено: {update_dt.strftime('%d.%m.%Y %H:%M')}"
    draw.text((IMG_WIDTH - 25, 35), update_badge_text, fill=COLOR_TEXT_LIGHT, font=font_title, anchor="rm")

    # Години та вертикальні лінії сітки
    grid_bottom_y = MARGIN_TOP + (NUM_QUEUES * CELL_HEIGHT)
    grid_right_x = MARGIN_LEFT + (NUM_INTERVALS * CELL_WIDTH)
    for hour in range(24):
        x_start = MARGIN_LEFT + (hour * 2 * CELL_WIDTH)
        draw.text((x_start, 75), f"{hour:02d}", fill=COLOR_TEXT, font=font_bold, anchor="lm")
        draw.line([(x_start, MARGIN_TOP), (x_start, grid_bottom_y)], fill=COLOR_GRID, width=1)
        x_half = x_start + CELL_WIDTH
        draw.line([(x_half, MARGIN_TOP - 15), (x_half, MARGIN_TOP)], fill=COLOR_TEXT, width=2)

    x_last_col_start = MARGIN_LEFT + (47 * CELL_WIDTH)
    draw.text((x_last_col_start + (CELL_WIDTH / 2), 75), "24", fill=COLOR_TEXT, font=font_bold, anchor="mm")

    # Права колонка "год"
    right_col_x1 = grid_right_x + 25
    right_col_x2 = IMG_WIDTH - 15
    right_col_center_x = right_col_x1 + ((right_col_x2 - right_col_x1) / 2)

    draw.rectangle([right_col_x1, MARGIN_TOP - 35, right_col_x2, MARGIN_TOP - 5], outline=COLOR_GRID, width=1)
    draw.text((right_col_center_x, MARGIN_TOP - 20), "год", fill=COLOR_TEXT, font=font_small, anchor="mm")

    for row in range(NUM_QUEUES):
        y1 = MARGIN_TOP + (row * CELL_HEIGHT)
        y_center = y1 + (CELL_HEIGHT / 2)
        draw.rectangle([right_col_x1, y1, right_col_x2, y_center + (CELL_HEIGHT / 2)], outline=COLOR_GRID, width=1)

        f = font_bold if queues[row].endswith(".1") else font_small
        draw.text((30, y_center), queues[row], fill=COLOR_TEXT, font=f, anchor="lm")

        off_slots = sum(
            1 for c in range(NUM_INTERVALS)
            if cell_states[(row, c)] in ("off", "added_off")
        )
        total_h = off_slots * 0.5
        sum_text = f"{total_h:.1f}"
        draw.text(
            (right_col_center_x, y_center),
            sum_text,
            fill=COLOR_TEXT,
            font=font_bold if total_h > 0 else font_small,
            anchor="mm",
        )

    # Рядок "Черг:" під таблицею (5.1 та 5.2 рахуються як одна черга '5')
    summary_y = grid_bottom_y + 18
    draw.text((25, summary_y), "Черг:", fill=COLOR_TEXT, font=font_bold, anchor="lm")
    for hour in range(24):
        c1 = hour * 2
        c2 = c1 + 1
        q_count_first = count_main_queues_off_in_slot(cell_states, c1)
        q_count_second = count_main_queues_off_in_slot(cell_states, c2)
        q_val = max(q_count_first, q_count_second) * 1.0
        x_hour_center = MARGIN_LEFT + (hour * 2 * CELL_WIDTH) + CELL_WIDTH
        draw.text(
            (x_hour_center, summary_y),
            f"{q_val:.1f}",
            fill=COLOR_TEXT if q_val > 0 else COLOR_TEXT_LIGHT,
            font=font_bold if q_val > 0 else font_small,
            anchor="mm",
        )

    # Напис "БЕЗ ВІДКЛЮЧЕНЬ!", якщо графіків немає
    if no_outages or len(effective_data) == 0:
        banner_w = 420
        banner_h = 66
        bx1 = MARGIN_LEFT + ((grid_right_x - MARGIN_LEFT) - banner_w) / 2
        by1 = MARGIN_TOP + ((grid_bottom_y - MARGIN_TOP) - banner_h) / 2
        bx2 = bx1 + banner_w
        by2 = by1 + banner_h

        draw.rectangle([bx1, by1, bx2, by2], fill=(255, 255, 255), outline=(46, 125, 50), width=3)
        draw.text(
            ((bx1 + bx2) / 2, (by1 + by2) / 2),
            "БЕЗ ВІДКЛЮЧЕНЬ!",
            fill=(27, 94, 32),
            font=font_banner,
            anchor="mm",
        )

    # Мітка оновлення — вгорі над сіткою; поточний момент часу — вертикальна лінія по сітці
    site_x = time_to_x(update_dt, schedule_date)
    now_x = time_to_x(now, schedule_date)

    top_bar_y1 = MARGIN_TOP - 28
    top_bar_y2 = MARGIN_TOP - 6
    draw.line([(site_x, top_bar_y1), (site_x, top_bar_y2)], fill=COLOR_SITE_UPDATE_LINE, width=4)
    draw.polygon(
        [
            (site_x - 6, top_bar_y1 - 6),
            (site_x + 6, top_bar_y1 - 6),
            (site_x, top_bar_y1 + 2),
        ],
        fill=COLOR_SITE_UPDATE_LINE,
    )

    draw.line([(now_x, MARGIN_TOP - 8), (now_x, grid_bottom_y + 6)], fill=COLOR_NOW_LINE, width=3)

    # Легенда
    leg_y1 = summary_y + 32
    leg_y2 = leg_y1 + 26

    draw.rectangle([MARGIN_LEFT, leg_y1, MARGIN_LEFT + 30, leg_y1 + 20], fill=COLOR_POWER_OFF)
    draw.text((MARGIN_LEFT + 40, leg_y1 + 2), "Відсутнє електропостачання", fill=COLOR_TEXT, font=font_small)

    draw_hatched_rect(draw, (MARGIN_LEFT, leg_y2, MARGIN_LEFT + 30, leg_y2 + 20), COLOR_ADDED_OFF, COLOR_HATCH_DARK)
    draw.text((MARGIN_LEFT + 40, leg_y2 + 2), "Відсутнє електропостачання (додано відключення)", fill=COLOR_TEXT, font=font_small)

    col2_x = MARGIN_LEFT + 360
    draw.rectangle([col2_x, leg_y1, col2_x + 30, leg_y1 + 20], fill=COLOR

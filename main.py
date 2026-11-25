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
    draw = ImageDraw.Draw(img)

    font_small, font_bold, font_title, font_logo, font_banner = load_fonts()

    effective_data = {} if no_outages else (power_off_data or {})
    cell_states = classify_cells(effective_data, prev_data)

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

    logo_text = "Волинь Вимкнення"
    draw.text((25, 35), logo_text, fill=COLOR_POWER_OFF, font=font_logo, anchor="lm")

    bbox = draw.textbbox((0, 0), logo_text, font=font_logo)
    bolt_x = 25 + (bbox[2] - bbox[0]) + 12
    bolt_points = [
        (bolt_x + 10, 18), (bolt_x + 20, 18), (bolt_x + 10, 30),
        (bolt_x + 22, 30), (bolt_x, 48), (bolt_x + 8, 34), (bolt_x - 2, 34),
    ]
    draw.polygon(bolt_points, fill=COLOR_POWER_OFF)

    sched_label = schedule_date.strftime("%d.%m.%Y")
    upd_label = update_dt.strftime("%d.%m.%Y %H:%M")
    update_badge_text = f"Графік: {sched_label} | Оновлено: {upd_label}"
    draw.text((IMG_WIDTH - 25, 35), update_badge_text, fill=COLOR_TEXT_LIGHT, font=font_title, anchor="rm")

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

        off_slots = sum(1 for c in range(NUM_INTERVALS) if cell_states[(row, c)] in ("off", "added_off"))
        total_h = off_slots * 0.5
        sum_text = f"{total_h:.1f}"
        draw.text((right_col_center_x, y_center), sum_text, fill=COLOR_TEXT, font=font_bold if total_h > 0 else font_small, anchor="mm")

    summary_y = grid_bottom_y + 18
    draw.text((25, summary_y), "Черг:", fill=COLOR_TEXT, font=font_bold, anchor="lm")
    for hour in range(24):
        c1 = hour * 2
        c2 = c1 + 1
        q_count_first = count_main_queues_off_in_slot(cell_states, c1)
        q_count_second = count_main_queues_off_in_slot(cell_states, c2)
        q_val = max(q_count_first, q_count_second) * 1.0
        x_hour_center = MARGIN_LEFT + (hour * 2 * CELL_WIDTH) + CELL_WIDTH
        draw.text((x_hour_center, summary_y), f"{q_val:.1f}", fill=COLOR_TEXT if q_val > 0 else COLOR_TEXT_LIGHT, font=font_bold if q_val > 0 else font_small, anchor="mm")

    if no_outages or len(effective_data) == 0:
        banner_w = 420
        banner_h = 66
        bx1 = MARGIN_LEFT + ((grid_right_x - MARGIN_LEFT) - banner_w) / 2
        by1 = MARGIN_TOP + ((grid_bottom_y - MARGIN_TOP) - banner_h) / 2
        bx2 = bx1 + banner_w
        by2 = by1 + banner_h

        draw.rectangle([bx1, by1, bx2, by2], fill=(255, 255, 255), outline=(46, 125, 50), width=3)
        draw.text(((bx1 + bx2) / 2, (by1 + by2) / 2), "БЕЗ ВІДКЛЮЧЕНЬ!", fill=(27, 94, 32), font=font_banner, anchor="mm")

    site_x = time_to_x(update_dt, schedule_date)
    now_x = time_to_x(now, schedule_date)
    draw.line([(site_x, MARGIN_TOP - 8), (site_x, grid_bottom_y + 6)], fill=COLOR_SITE_UPDATE_LINE, width=3)
    if now.date() == schedule_date:
        draw.line([(now_x, MARGIN_TOP - 8), (now_x, grid_bottom_y + 6)], fill=COLOR_NOW_LINE, width=3)

    leg_y1 = summary_y + 32
    leg_y2 = leg_y1 + 26

    draw.rectangle([MARGIN_LEFT, leg_y1, MARGIN_LEFT + 30, leg_y1 + 20], fill=COLOR_POWER_OFF)
    draw.text((MARGIN_LEFT + 40, leg_y1 + 2), "Відсутнє електропостачання", fill=COLOR_TEXT, font=font_small)

    draw_hatched_rect(draw, (MARGIN_LEFT, leg_y2, MARGIN_LEFT + 30, leg_y2 + 20), COLOR_ADDED_OFF, COLOR_HATCH_DARK)
    draw.text((MARGIN_LEFT + 40, leg_y2 + 2), "Відсутнє електропостачання (додано відключення)", fill=COLOR_TEXT, font=font_small)

    col2_x = MARGIN_LEFT + 360
    draw.rectangle([col2_x, leg_y1, col2_x + 30, leg_y1 + 20], fill=COLOR_POWER_ON, outline=COLOR_GRID)
    draw.text((col2_x + 40, leg_y1 + 2), "Наявне електропостачання", fill=COLOR_TEXT, font=font_small)

    draw_hatched_rect(draw, (col2_x, leg_y2, col2_x + 30, leg_y2 + 20), COLOR_REMOVED_OFF, COLOR_HATCH_LIGHT)
    draw.text((col2_x + 40, leg_y2 + 2), "Наявне електропостачання (прибрано відключення)", fill=COLOR_TEXT, font=font_small)

    col3_x = IMG_WIDTH - 340
    draw.line([(col3_x, leg_y1 - 2), (col3_x, leg_y1 + 20)], fill=COLOR_SITE_UPDATE_LINE, width=3)
    draw.text((col3_x + 12, leg_y1 + 9), "Останнє оновлення на сайті", fill=COLOR_TEXT, font=font_small, anchor="lm")
    draw.text((IMG_WIDTH - 25, leg_y1 + 9), update_dt.strftime("%d.%m %H:%M"), fill=COLOR_TEXT_LIGHT, font=font_small, anchor="rm")

    draw.line([(col3_x, leg_y2 - 2), (col3_x, leg_y2 + 20)], fill=COLOR_NOW_LINE, width=3)
    draw.text((col3_x + 12, leg_y2 + 9), "Поточний момент часу", fill=COLOR_TEXT, font=font_small, anchor="lm")
    draw.text((IMG_WIDTH - 25, leg_y2 + 9), now.strftime("%d.%m %H:%M"), fill=COLOR_TEXT_LIGHT, font=font_small, anchor="rm")

    if not output_path:
        output_path = f"schedule_{schedule_date.isoformat()}_{update_dt.strftime('%H-%M')}.png"

    img.save(output_path)
    print(f"✅ Картинку успішно згенеровано та збережено як {output_path}!")
    return output_path


def _is_cell_fill(rgb):
    r, g, b = [int(v) for v in rgb[:3]]
    if r >= 242 and g >= 242 and b >= 242:
        return False
    return max(r, g, b) - min(r, g, b) >= 3 or (r + g + b) < 710


def _is_outage_pixel(rgb):
    """
    Розпізнає тёмно-сині клітинки відключень на картинках Волиньобленерго.
    Світлі сіро-блакитні клітинки наявного світла мають високу яскравість (~185-215) і малу різницю B-R,
    тоді як клітинки відключення помітно темніші та синіші.
    """
    r, g, b = [int(v) for v in rgb[:3]]
    brightness = (r + g + b) / 3.0
    return brightness < 135 and (b - r) >= 22


def _find_grid_in_band(pixels, width, height, y_min, y_max):
    # Починаємо після стовпців "Черга" та "Підчерга"
    x_scan_min = int(width * 0.19) if width >= 1000 else int(width * 0.20)
    x_scan_max = int(width * 0.98)
    step_x = max(2, (x_scan_max - x_scan_min) // 140)
    sample_xs = list(range(x_scan_min, x_scan_max, step_x))
    if not sample_xs:
        return None

    row_scores = []
    for y in range(max(0, y_min), min(height, y_max)):
        hits = sum(1 for x in sample_xs if _is_cell_fill(pixels[x, y]))
        row_scores.append((y, hits / float(len(sample_xs))))

    good_ys = [y for y, ratio in row_scores if ratio >= 0.45]
    if not good_ys:
        return None

    segments = []
    s = p = good_ys[0]
    for y in good_ys[1:]:
        if y - p <= 4:
            p = y
        else:
            segments.append((s, p))
            s = p = y
    segments.append((s, p))

    top_y, bot_y = max(segments, key=lambda seg: seg[1] - seg[0])
    if bot_y - top_y < 80:
        return None

    left_vals = []
    right_vals = []
    for frac in (0.15, 0.35, 0.50, 0.65, 0.85):
        sy = int(top_y + (bot_y - top_y) * frac)
        if 0 <= sy < height:
            xs = [x for x in range(x_scan_min, int(width * 0.995)) if _is_cell_fill(pixels[x, sy])]
            if len(xs) >= 120:
                left_vals.append(xs[0])
                right_vals.append(xs[-1])

    if not left_vals or not right_vals:
        return None

    left_vals.sort()
    right_vals.sort()
    x1 = left_vals[len(left_vals) // 2]
    x2 = right_vals[len(right_vals) // 2]
    if x2 - x1 < 240:
        return None

    return x1, top_y, x2, bot_y


def _sample_cell_is_outage(pixels, width, height, cx, cy, slot_w, row_h):
    offsets = (-0.22, 0.0, 0.22)
    outage_votes = 0
    total_votes = 0

    for ox in offsets:
        for oy in offsets:
            px = int(cx + ox * slot_w)
            py = int(cy + oy * row_h)
            if 0 <= px < width and 0 <= py < height:
                total_votes += 1
                if _is_outage_pixel(pixels[px, py]):
                    outage_votes += 1

    return total_votes > 0 and outage_votes > (total_votes / 2.0)


def parse_gpv_image_to_data(image_bytes):
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    width, height = img.size
    pixels = img.load()

    if width == 1750 and height == 2250:
        top_bounds = _find_grid_in_band(pixels, width, height, 400, 1020)
        bot_bounds = _find_grid_in_band(pixels, width, height, 1300, 1950)
    else:
        mid_y = height // 2
        top_bounds = _find_grid_in_band(pixels, width, height, int(height * 0.14), mid_y)
        bot_bounds = _find_grid_in_band(pixels, width, height, mid_y, int(height * 0.96))

    if not top_bounds or not bot_bounds:
        print(f"⚠️ Не вдалося визначити межі сітки (розмір: {width}x{height}, top={top_bounds}, bot={bot_bounds}).")
        return {}, False

    power_off_data = {}
    blocks = [
        (top_bounds, 0),
        (bot_bounds, 24),
    ]

    for (bx1, by1, bx2, by2), col_offset in blocks:
        block_w = max(1, bx2 - bx1)
        block_h = max(1, by2 - by1)
        slot_w = block_w / 24.0
        row_h = block_h / float(NUM_QUEUES)

        for r in range(NUM_QUEUES):
            cy = by1 + (r + 0.5) * row_h
            for c in range(24):
                cx = bx1 + (c + 0.5) * slot_w
                if _sample_cell_is_outage(pixels, width, height, cx, cy, slot_w, row_h):
                    power_off_data[(r, col_offset + c)] = True

    return power_off_data, True


def _fetch_single_option(option_key, headers):
    api_url = f"{API_BASE_URL}/api/options"
    resp = requests.get(api_url, params={"option_key": option_key}, headers=headers, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    return payload.get("hydra:member") or []


def _extract_schedule_from_member(item, default_date, save_as_name=None, headers=None):
    media_path = (item.get("value") or "").strip()
    site_update_dt = parse_iso_to_kyiv_naive(item.get("date"))
    sched_date = default_date

    if not media_path:
        return {}, site_update_dt, sched_date, False, True

    img_url = media_path if media_path.startswith("http") else f"{API_BASE_URL}{media_path}"
    print(f"Завантажуємо таблицю графіка: {img_url}")

    img_resp = requests.get(img_url, headers={"User-Agent": headers["User-Agent"]}, timeout=25)
    img_resp.raise_for_status()

    if save_as_name:
        with open(save_as_name, "wb") as f:
            f.write(img_resp.content)
        print(f"💾 Збережено оригінальну картинку як {save_as_name}")

    power_off_data, parsed_ok = parse_gpv_image_to_data(img_resp.content)
    if not parsed_ok:
        return {}, site_update_dt, sched_date, False, False

    return power_off_data, site_update_dt, sched_date, True, True


def fetch_schedules_from_site(save_source_gpv=False, prefer_tomorrow=False):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    now = get_now()
    today_date = now.date()
    tomorrow_date = today_date + datetime.timedelta(days=1)

    results = {}
    any_ok = False

    # 1. Сьогодні
    found_today = None
    for k in ["pw_gpv_image_today", "gpv_image_today"]:
        try:
            print(f"Запитуємо API: {API_BASE_URL}/api/options?option_key={k} ...")
            members = _fetch_single_option(k, headers)
            any_ok = True
            if members:
                found_today = members[-1]
                break
        except Exception as e:
            print(f"⚠️ Помилка запиту ключа {k}: {e}")

    if found_today is None:
        results["today"] = {
            "data": {},
            "update_dt": None,
            "date": today_date,
            "found": False,
            "ok": any_ok,
        }
    else:
        try:
            data, upd_dt, s_date, found_sched, parsed_ok = _extract_schedule_from_member(
                found_today,
                default_date=today_date,
                save_as_name=("source_gpv_today.png" if save_source_gpv else None),
                headers=headers,
            )
            if save_source_gpv and os.path.exists("source_gpv_today.png"):
                with open("source_gpv_today.png", "rb") as src, open("source_gpv.png", "wb") as dst:
                    dst.write(src.read())

            results["today"] = {
                "data": data if parsed_ok else {},
                "update_dt": upd_dt,
                "date": s_date,
                "found": found_sched if parsed_ok else False,
                "ok": parsed_ok,
            }
            if parsed_ok:
                print(f"✅ [today] Розпізнано слотів відключень: {len(data)}")
        except Exception as e:
            print(f"❌ Помилка завантаження/парсингу today: {e}")
            results["today"] = {
                "data": {},
                "update_dt": None,
                "date": today_date,
                "found": False,
                "ok": False,
            }

    # 2. Завтра — підключаємо лише якщо в API реально задане media_path і воно не зі старої дати
    found_tomorrow = None
    for k in ["pw_gpv_image_tomorrow", "gpv_image_tomorrow"]:
        try:
            print(f"Запитуємо API: {API_BASE_URL}/api/options?option_key={k} ...")
            members = _fetch_single_option(k, headers)
            if members:
                candidate = members[-1]
                cand_val = (candidate.get("value") or "").strip()
                cand_dt = parse_iso_to_kyiv_naive(candidate.get("date"))
                # Ігноруємо пусті або явно прострочені записи з минулих днів
                is_stale = cand_dt is not None and cand_dt.date() < today_date
                if cand_val and not is_stale:
                    found_tomorrow = candidate
                    break
        except Exception as e:
            print(f"⚠️ Помилка запиту ключа {k}: {e}")

    if found_tomorrow is None:
        results["tomorrow"] = None
    else:
        try:
            data, upd_dt, s_date, found_sched, parsed_ok = _extract_schedule_from_member(
                found_tomorrow,
                default_date=tomorrow_date,
                save_as_name=("source_gpv_tomorrow.png" if save_source_gpv else None),
                headers=headers,
            )
            if parsed_ok and found_sched:
                print(f"✅ [tomorrow] Розпізнано слотів відключень: {len(data)}")
                results["tomorrow"] = {
                    "data": data,
                    "update_dt": upd_dt,
                    "date": s_date,
                    "found": True,
                    "ok": True,
                }
            else:
                results["tomorrow"] = None
        except Exception as e:
            print(f"⚠️ Помилка завантаження/парсингу tomorrow: {e}")
            results["tomorrow"] = None

    return results


def fetch_data_from_site(save_source_gpv=False, tomorrow=False):
    schedules = fetch_schedules_from_site(save_source_gpv=save_source_gpv, prefer_tomorrow=tomorrow)
    chosen_key = "tomorrow" if (tomorrow and schedules.get("tomorrow")) else "today"
    entry = schedules.get(chosen_key) or {
        "data": {},
        "update_dt": None,
        "date": get_now().date(),
        "found": False,
        "ok": False,
    }
    return entry["data"], entry["update_dt"], entry["found"], entry["ok"]


def parse_intervals_into_data(data_dict, queue_name, time_ranges):
    if queue_name not in queues:
        print(f"⚠️ Чергу '{queue_name}' не знайдено у списку черг.")
        return

    row_idx = queues.index(queue_name)
    cleaned = time_ranges.replace("\xa0", " ").replace("\n", " ")
    intervals = re.findall(r"(\d{2}):(\d{2})-(\d{2}):(\d{2})", cleaned)

    for start_h, start_m, end_h, end_m in intervals:
        start_h, start_m, end_h, end_m = map(int, (start_h, start_m, end_h, end_m))
        start_col = start_h * 2 + (1 if start_m >= 30 else 0)
        end_col = end_h * 2 + (1 if end_m >= 30 else 0)
        if end_h == 24:
            end_col = 48

        for c in range(start_col, end_col):
            if 0 <= c < 48:
                data_dict[(row_idx, c)] = True


def build_debug_schedule(debug_queues):
    debug_data = {}
    for q_name, ranges_str in debug_queues.items():
        parse_intervals_into_data(debug_data, q_name, ranges_str)
    return debug_data


def apply_queue_overrides(base_data, overrides):
    data = dict(base_data)
    for queue_name, ranges_str in overrides:
        if queue_name not in queues:
            print(f"⚠️ Чергу '{queue_name}' не знайдено у списку черг.")
            continue

        row_idx = queues.index(queue_name)
        for key in [k for k in data if k[0] == row_idx]:
            del data[key]

        if ranges_str.strip():
            parse_intervals_into_data(data, queue_name, ranges_str)
            print(f"🛠️ Черга {queue_name}: задано інтервали '{ranges_str}'")
        else:
            print(f"🧹 Черга {queue_name}: інтервали очищено")

    return data


def generate_caption(current_data, prev_data, is_empty, schedule_date=None):
    target_date = schedule_date or get_now().date()
    date_str = target_date.strftime("%d.%m.%Y")
    caption = f"📅 <b>{date_str} - Оновлено ГПВ</b>\n\n"

    if is_empty:
        return caption + "✅ <b>БЕЗ ВІДКЛЮЧЕНЬ!</b>"

    if prev_data is None:
        return caption + "⚡ <b>Оновлений графік відключень</b>"

    added_slots = []
    removed_slots = []

    for row in range(NUM_QUEUES):
        for col in range(NUM_INTERVALS):
            now_off = bool(current_data.get((row, col), False))
            was_off = bool(prev_data.get((row, col), False))

            if now_off and not was_off:
                added_slots.append((row, col))
            elif not now_off and was_off:
                removed_slots.append((row, col))

    if not added_slots and not removed_slots:
        return caption + "🤷‍♂️ Змін немає."

    def get_intervals(slots):
        by_row = {}
        for r, c in slots:
            by_row.setdefault(r, []).append(c)

        result = []
        for r, cols in by_row.items():
            cols.sort()
            start = cols[0]
            prev = cols[0]
            for c in cols[1:]:
                if c == prev + 1:
                    prev = c
                else:
                    result.append((r, start, prev))
                    start = c
                    prev = c
            result.append((r, start, prev))
        return result

    if added_slots:
        total_h = len(added_slots) * 0.5
        caption += f"🟢 <b>Додано відключення (+{total_h:.1f}):</b>\n"

        groups = {}
        for r, st, en in get_intervals(added_slots):
            sh, sm = st // 2, 30 if st % 2 else 0
            eh, em = (en + 1) // 2, 30 if (en + 1) % 2 else 0
            if eh == 24:
                em = 0
            time_str = f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"
            dur = (en - st + 1) * 0.5
            key = (time_str, dur)
            groups.setdefault(key, []).append(queues[r])

        for (time_str, dur), q_list in groups.items():
            q_str = ", ".join(q_list)
            caption += f"◾️ {q_str}: +{dur:.1f}  {time_str}\n"
        caption += "\n"

    if removed_slots:
        total_h = len(removed_slots) * 0.5
        caption += f"🔴 <b>Прибрано відключення (-{total_h:.1f}):</b>\n"

        groups = {}
        for r, st, en in get_intervals(removed_slots):
            sh, sm = st // 2, 30 if st % 2 else 0
            eh, em = (en + 1) // 2, 30 if (en + 1) % 2 else 0
            if eh == 24:
                em = 0
            time_str = f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"
            dur = (en - st + 1) * 0.5
            key = (time_str, dur)
            groups.setdefault(key, []).append(queues[r])

        for (time_str, dur), q_list in groups.items():
            q_str = ", ".join(q_list)
            caption += f"◾️ {q_str}: -{dur:.1f}  {time_str}\n"

    return caption.strip()


def send_to_telegram(image_path, caption="Оновлений графік відключень ⚡"):
    if not BOT_TOKEN or not CHANNEL_ID:
        print("⚠️ Не задано BOT_TOKEN або CHANNEL_ID — пропускаємо відправку в Telegram.")
        return

    print(f"Відправляємо {image_path} в Telegram...")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    try:
        with open(image_path, "rb") as photo:
            files = {"photo": photo}
            data = {
                "chat_id": CHANNEL_ID,
                "caption": caption,
                "parse_mode": "HTML",
            }
            response = requests.post(url, files=files, data=data, timeout=30)
            if response.status_code == 200:
                print("✅ Картинку успішно відправлено в канал!")
            else:
                print(f"❌ Помилка відправки в Telegram: {response.text}")
    except Exception as e:
        print(f"Помилка відправки: {e}")


def parse_cli_args():
    parser = argparse.ArgumentParser(description="Моніторинг графіків вимкнень + дебаг черг")
    parser.add_argument("--once", action="store_true", help="Виконати одну перевірку та завершитися")
    parser.add_argument("--debug", action="store_true", help="Тестовий режим на основі DEBUG_QUEUES та --set")
    parser.add_argument("--live-override", action="store_true", help="Взяти графік з API та накласти --set")
    parser.add_argument("--with-prev", action="store_true", help="Порівняти зі збереженим станом")
    parser.add_argument("--tomorrow", action="store_true", help="Обрати графік на завтра (якщо є)")
    parser.add_argument(
        "--set",
        nargs=2,
        metavar=("ЧЕРГА", "ІНТЕРВАЛИ"),
        action="append",
        default=[],
        help='Задати інтервали для черги, наприклад: --set 1.1 "08:00-11:00, 18:00-21:00"',
    )
    parser.add_argument(
        "--no-outages",
        action="store_true",
        help="Режим БЕЗ ВІДКЛЮЧЕНЬ! (малює графік із написом БЕЗ ВІДКЛЮЧЕНЬ!)",
    )
    parser.add_argument("--send", action="store_true", help="Відправити згенеровану картинку в Telegram")
    parser.add_argument("--save-source-gpv", action="store_true", help="Зберегти вхідну картинку _GPV.png як source_gpv.png")
    return parser.parse_args()


def _process_slot_update(slot_name, entry, state_path, save_state_file=True):
    if not entry or not entry.get("ok"):
        print(f"⚠️ [{slot_name}] Помилка отримання даних — пропускаємо.")
        return None

    sched_date = entry.get("date") or get_now().date()
    site_update_dt = entry.get("update_dt")
    found_schedule = entry.get("found", False)
    current_data = entry.get("data") or {}

    if not found_schedule or len(current_data) == 0:
        current_data = {}

    last_data = load_state(state_path, schedule_date=sched_date)

    if current_data != last_data:
        is_empty = len(current_data) == 0
        print(f"🔄 [{slot_name}] Виявлено зміни в графіку ({sched_date})! Малюємо та відправляємо...")
        img_file = create_schedule_image(
            current_data,
            prev_data=last_data,
            site_update_dt=site_update_dt,
            schedule_date=sched_date,
            no_outages=is_empty,
        )
        caption = generate_caption(current_data, last_data, is_empty, schedule_date=sched_date)
        send_to_telegram(img_file, caption=caption)
        if save_state_file:
            save_state(current_data, state_path, schedule_date=sched_date)
        return current_data

    print(f"[{slot_name}] Змін немає ({sched_date}).")
    if save_state_file:
        save_state(current_data, state_path, schedule_date=sched_date)
    return current_data


def run_single_check(save_source_gpv=False):
    print(f"\n[{get_now().strftime('%H:%M:%S')}] Перевіряємо оновлення (режим --once)...")
    schedules = fetch_schedules_from_site(save_source_gpv=save_source_gpv)
    _process_slot_update("today", schedules.get("today"), STATE_FILE, save_state_file=True)
    if schedules.get("tomorrow"):
        _process_slot_update("tomorrow", schedules.get("tomorrow"), "state_tomorrow.json", save_state_file=True)


if __name__ == "__main__":
    args = parse_cli_args()

    if args.debug:
        print("🛠️ Режим DEBUG...")
        if args.no_outages:
            img_file = create_schedule_image({}, prev_data=None, no_outages=True)
            if args.send:
                send_to_telegram(img_file, caption=generate_caption({}, None, True))
            sys.exit(0)

        base_debug = build_debug_schedule(DEBUG_QUEUES)
        updated_debug = apply_queue_overrides(base_debug, args.set)
        prev_for_debug = base_debug if args.set else None
        is_empty = len(updated_debug) == 0
        img_file = create_schedule_image(
            updated_debug,
            prev_data=prev_for_debug,
            no_outages=is_empty,
        )
        if args.send:
            caption = generate_caption(updated_debug, prev_for_debug, is_empty)
            send_to_telegram(img_file, caption=caption)
        sys.exit(0)

    if args.live_override:
        print("🧪 Режим LIVE-OVERRIDE: беремо дані з API та застосовуємо --set...")
        schedules = fetch_schedules_from_site(save_source_gpv=args.save_source_gpv, prefer_tomorrow=args.tomorrow)
        chosen = "tomorrow" if (args.tomorrow and schedules.get("tomorrow")) else "today"
        entry = schedules.get(chosen) or {}
        if not entry.get("ok"):
            print("⚠️ Не вдалося отримати дані з API.")
            sys.exit(1)

        site_data = entry.get("data") or {}
        site_update_dt = entry.get("update_dt")
        sched_date = entry.get("date") or get_now().date()
        override_data = {} if args.no_outages else apply_queue_overrides(site_data, args.set)

        if args.with_prev:
            st_file = "state_tomorrow.json" if chosen == "tomorrow" else STATE_FILE
            prev_for_live = load_state(st_file, schedule_date=sched_date)
        else:
            prev_for_live = site_data if (args.set or args.no_outages) else None

        is_empty = len(override_data) == 0
        img_file = create_schedule_image(
            override_data,
            prev_data=prev_for_live,
            site_update_dt=site_update_dt,
            schedule_date=sched_date,
            no_outages=is_empty,
        )
        if args.send:
            caption = generate_caption(override_data, prev_for_live, is_empty, schedule_date=sched_date)
            send_to_telegram(img_file, caption=caption)
        sys.exit(0)

    if args.once:
        run_single_check(save_source_gpv=args.save_source_gpv)
        sys.exit(0)

    print("Бот запущений. Починаємо моніторинг у циклі...")
    while True:
        try:
            print(f"\n[{get_now().strftime('%H:%M:%S')}] Перевіряємо оновлення...")
            schedules = fetch_schedules_from_site(save_source_gpv=args.save_source_gpv)
            _process_slot_update("today", schedules.get("today"), STATE_FILE, save_state_file=True)
            if schedules.get("tomorrow"):
                _process_slot_update("tomorrow", schedules.get("tomorrow"), "state_tomorrow.json", save_state_file=True)
        except Exception as e:
            print(f"Сталася помилка під час циклу: {e}")

        print("Наступна перевірка через 1 хвилину...")
        time.sleep(60)

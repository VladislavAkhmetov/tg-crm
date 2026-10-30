import io
import re
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
import httpx

STATUS_NAMES: Dict[str, str] = {
    "available": "Свободна",
    "in_progress": "В работе",
    "internal_review": "Внутреннее ревью",
    "client_review": "Клиентское ревью",
    "export_ready": "Выгрузка",
    "completed": "Готово",
}

STATUS_COLORS: Dict[str, Tuple[str, str]] = {
    # status: (background_hex, text_hex)
    "completed": ("DCFCE7", "166534"),       # soft green
    "in_progress": ("FEF3C7", "92400E"),     # soft yellow
    "internal_review": ("E0E7FF", "3730A3"), # soft indigo
    "client_review": ("F3E8FF", "6B21A8"),   # soft purple
    "export_ready": ("CFFAFE", "155E75"),    # soft cyan
    "available": ("F3F4F6", "4B5563"),       # soft gray
}

def format_iso_datetime(dt_str: Optional[str]) -> str:
    if not dt_str:
        return "—"
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt_str)[:16]

def calculate_lead_time(start_str: Optional[str], end_str: Optional[str]) -> str:
    if not start_str or not end_str:
        return "—"
    try:
        start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        delta = end_dt - start_dt
        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            return "—"
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60
        if days > 0:
            return f"{days} д. {hours} ч."
        if hours > 0:
            return f"{hours} ч. {minutes} мин."
        return f"{minutes} мин."
    except Exception:
        return "—"

def sanitize_sheet_title(title: str, existing_titles: Optional[set] = None) -> str:
    clean = re.sub(r'[\\/*?:\[\]]', '_', title).strip()
    clean = clean or "Пул"
    if len(clean) > 28:
        clean = clean[:28]
    if existing_titles is not None:
        candidate = clean
        counter = 1
        while candidate.lower() in existing_titles:
            suffix = f"_{counter}"
            candidate = clean[:31 - len(suffix)] + suffix
            counter += 1
        existing_titles.add(candidate.lower())
        return candidate
    return clean

def generate_excel(tasks: List[Dict[str, Any]], pool_names: Optional[List[str]] = None) -> bytes:
    wb = openpyxl.Workbook()
    # Remove default sheet
    wb.remove(wb.active)

    existing_titles = set()

    # Font definitions
    font_header = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    font_bold = Font(name="Calibri", size=11, bold=True, color="000000")
    font_regular = Font(name="Calibri", size=11, color="1F2937")
    font_muted = Font(name="Calibri", size=10, color="6B7280", italic=True)
    font_title = Font(name="Calibri", size=15, bold=True, color="1E293B")
    font_section = Font(name="Calibri", size=12, bold=True, color="1E293B")
    font_link = Font(name="Calibri", size=11, color="2563EB", underline="single")

    # Fills
    fill_header = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    fill_summary_header = PatternFill(start_color="334155", end_color="334155", fill_type="solid")
    fill_total = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")

    # Borders
    thin_border_side = Side(border_style="thin", color="E2E8F0")
    cell_border = Border(left=thin_border_side, right=thin_border_side, top=thin_border_side, bottom=thin_border_side)
    thick_top_side = Side(border_style="medium", color="64748B")
    double_bottom_side = Side(border_style="double", color="64748B")
    total_border = Border(left=thin_border_side, right=thin_border_side, top=thick_top_side, bottom=double_bottom_side)

    # Alignments
    align_center = Alignment(horizontal="center", vertical="center")
    align_left = Alignment(horizontal="left", vertical="center")
    align_right = Alignment(horizontal="right", vertical="center")

    # Group tasks by pool
    pools_map: Dict[str, List[Dict[str, Any]]] = {}
    for t in tasks:
        p_name = (t.get("pool_name") or "Без пула").strip()
        pools_map.setdefault(p_name, []).append(t)

    # 1. Sheet: СВОДКА (Summary)
    ws_summary = wb.create_sheet(title=sanitize_sheet_title("Сводка", existing_titles))
    ws_summary.views.sheetView[0].showGridLines = True

    # Title & generated time
    now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    ws_summary["A1"] = "ОТЧЕТ ПО ЗАДАЧАМ И ПУЛАМ"
    ws_summary["A1"].font = font_title
    ws_summary["A2"] = f"Дата выгрузки: {now_str}"
    ws_summary["A2"].font = font_muted

    # Overall KPIs
    total_tasks_count = len(tasks)
    completed_count = sum(1 for t in tasks if t.get("status") == "completed")
    in_progress_count = sum(1 for t in tasks if t.get("status") in ("in_progress", "internal_review", "client_review", "export_ready"))
    available_count = sum(1 for t in tasks if t.get("status") == "available")
    total_images_count = sum((t.get("images_count") or 0) for t in tasks)
    pct_completed = (completed_count / total_tasks_count * 100) if total_tasks_count > 0 else 0.0

    ws_summary["A4"] = "ОБЩАЯ СТАТИСТИКА"
    ws_summary["A4"].font = font_section

    kpi_headers = ["Показатель", "Значение"]
    ws_summary.append([]) # row 5 will be empty
    ws_summary.cell(row=5, column=1, value=kpi_headers[0]).font = font_header
    ws_summary.cell(row=5, column=1).fill = fill_summary_header
    ws_summary.cell(row=5, column=1).alignment = align_left
    ws_summary.cell(row=5, column=2, value=kpi_headers[1]).font = font_header
    ws_summary.cell(row=5, column=2).fill = fill_summary_header
    ws_summary.cell(row=5, column=2).alignment = align_right

    kpi_rows = [
        ("Всего пулов в отчете", len(pools_map)),
        ("Всего задач (артикулов)", total_tasks_count),
        ("Выполнено задач", f"{completed_count} ({pct_completed:.1f}%)"),
        ("В работе / на проверке", in_progress_count),
        ("Свободно", available_count),
        ("Отрисовано изображений (всего фото)", total_images_count),
    ]

    curr_row = 6
    for label, val in kpi_rows:
        c1 = ws_summary.cell(row=curr_row, column=1, value=label)
        c2 = ws_summary.cell(row=curr_row, column=2, value=val)
        c1.font = font_regular
        c1.border = cell_border
        c2.font = font_bold if "изображений" in label or "артикулов" in label else font_regular
        c2.border = cell_border
        c2.alignment = align_right
        curr_row += 1

    # Designer summary table
    curr_row += 2
    ws_summary.cell(row=curr_row, column=1, value="ВЫРАБОТКА ДИЗАЙНЕРОВ").font = font_section
    curr_row += 1

    designer_headers = ["Дизайнер", "Telegram", "Выполнено задач", "В работе", "Всего изображений (фото)"]
    for col_idx, h in enumerate(designer_headers, 1):
        cell = ws_summary.cell(row=curr_row, column=col_idx, value=h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center if col_idx > 2 else align_left
        cell.border = cell_border
    curr_row += 1

    # Aggregate by designer
    designers_map: Dict[str, Dict[str, Any]] = {}
    for t in tasks:
        d_name = t.get("assigned_to_name")
        if not d_name:
            continue
        uname = f"@{t['assigned_to_username']}" if t.get("assigned_to_username") else "—"
        key = d_name.strip()
        rec = designers_map.setdefault(key, {
            "name": d_name,
            "username": uname,
            "completed": 0,
            "in_progress": 0,
            "images": 0
        })
        st = t.get("status")
        if st == "completed":
            rec["completed"] += 1
            rec["images"] += (t.get("images_count") or 0)
        elif st in ("in_progress", "internal_review", "client_review", "export_ready"):
            rec["in_progress"] += 1
            rec["images"] += (t.get("images_count") or 0)

    d_start_row = curr_row
    sorted_designers = sorted(designers_map.values(), key=lambda x: (x["images"], x["completed"]), reverse=True)
    if not sorted_designers:
        ws_summary.cell(row=curr_row, column=1, value="Назначенных исполнителей пока нет").font = font_muted
        curr_row += 1
    else:
        for d in sorted_designers:
            ws_summary.cell(row=curr_row, column=1, value=d["name"]).alignment = align_left
            ws_summary.cell(row=curr_row, column=2, value=d["username"]).alignment = align_left
            ws_summary.cell(row=curr_row, column=3, value=d["completed"]).alignment = align_center
            ws_summary.cell(row=curr_row, column=4, value=d["in_progress"]).alignment = align_center
            ws_summary.cell(row=curr_row, column=5, value=d["images"]).alignment = align_right

            for c in range(1, 6):
                cell = ws_summary.cell(row=curr_row, column=c)
                cell.font = font_regular
                cell.border = cell_border
            curr_row += 1

        # Total designer row
        ws_summary.cell(row=curr_row, column=1, value="ИТОГО").font = font_bold
        ws_summary.cell(row=curr_row, column=2, value="").font = font_bold
        c3 = ws_summary.cell(row=curr_row, column=3, value=f"=SUM(C{d_start_row}:C{curr_row-1})")
        c4 = ws_summary.cell(row=curr_row, column=4, value=f"=SUM(D{d_start_row}:D{curr_row-1})")
        c5 = ws_summary.cell(row=curr_row, column=5, value=f"=SUM(E{d_start_row}:E{curr_row-1})")
        for c in range(1, 6):
            cell = ws_summary.cell(row=curr_row, column=c)
            cell.font = font_bold
            cell.fill = fill_total
            cell.border = total_border
            if c > 2:
                cell.alignment = align_center if c < 5 else align_right
        curr_row += 1

    # Pool overview table
    curr_row += 2
    ws_summary.cell(row=curr_row, column=1, value="СТАТУС ПО ПУЛАМ").font = font_section
    curr_row += 1

    pool_headers = ["Пул", "Всего задач", "Выполнено", "В работе", "Свободно", "Всего изображений", "Готовность"]
    for col_idx, h in enumerate(pool_headers, 1):
        cell = ws_summary.cell(row=curr_row, column=col_idx, value=h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center if col_idx > 1 else align_left
        cell.border = cell_border
    curr_row += 1

    p_start_row = curr_row
    for p_name, p_tasks in pools_map.items():
        p_total = len(p_tasks)
        p_comp = sum(1 for t in p_tasks if t.get("status") == "completed")
        p_act = sum(1 for t in p_tasks if t.get("status") in ("in_progress", "internal_review", "client_review", "export_ready"))
        p_free = sum(1 for t in p_tasks if t.get("status") == "available")
        p_img = sum((t.get("images_count") or 0) for t in p_tasks)
        p_pct = f"{(p_comp / p_total * 100):.1f}%" if p_total > 0 else "0.0%"

        ws_summary.cell(row=curr_row, column=1, value=p_name).alignment = align_left
        ws_summary.cell(row=curr_row, column=2, value=p_total).alignment = align_center
        ws_summary.cell(row=curr_row, column=3, value=p_comp).alignment = align_center
        ws_summary.cell(row=curr_row, column=4, value=p_act).alignment = align_center
        ws_summary.cell(row=curr_row, column=5, value=p_free).alignment = align_center
        ws_summary.cell(row=curr_row, column=6, value=p_img).alignment = align_right
        ws_summary.cell(row=curr_row, column=7, value=p_pct).alignment = align_center

        for c in range(1, 8):
            cell = ws_summary.cell(row=curr_row, column=c)
            cell.font = font_regular
            cell.border = cell_border
        curr_row += 1

    # Auto-adjust column widths for summary sheet
    for col in ws_summary.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            val_str = str(cell.value or "")
            if val_str.startswith("="):
                val_str = "12345"
            max_len = max(max_len, len(val_str))
        ws_summary.column_dimensions[col_letter].width = max(max_len + 3, 14)

    # 2. Function to build a Task Data Sheet
    def build_tasks_sheet(ws, sheet_tasks: List[Dict[str, Any]], show_pool_col: bool = False):
        ws.views.sheetView[0].showGridLines = True
        ws.freeze_panes = "A2"

        headers = [
            "№",
            "Артикул (SKU)",
        ]
        if show_pool_col:
            headers.append("Пул")
        headers.extend([
            "Статус",
            "Дизайнер",
            "Изображений (шт.)",
            "Дата выдачи",
            "Дата завершения",
            "Время в работе",
            "Исходники (Облако)",
            "Внутренняя Figma",
            "Клиентская Figma",
            "Яндекс.Диск (Финал)",
            "Задача в Битрикс24",
            "Примечания / Правки"
        ])

        # Header row
        for col_idx, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=h)
            cell.font = font_header
            cell.fill = fill_header
            cell.alignment = align_center
            cell.border = cell_border

        img_col_idx = 4 if not show_pool_col else 5 # 1-based index for "Изображений (шт.)"

        row_idx = 2
        for num, t in enumerate(sheet_tasks, 1):
            status_code = t.get("status") or "available"
            status_display = STATUS_NAMES.get(status_code, status_code)

            designer_parts = []
            if t.get("assigned_to_name"):
                designer_parts.append(t["assigned_to_name"])
            if t.get("assigned_to_username"):
                designer_parts.append(f"(@{t['assigned_to_username']})")
            designer_display = " ".join(designer_parts) if designer_parts else "—"

            images_count = t.get("images_count")
            assigned_at_str = format_iso_datetime(t.get("assigned_at"))
            completed_at_str = format_iso_datetime(t.get("updated_at")) if status_code == "completed" else "—"
            lead_time = calculate_lead_time(t.get("assigned_at"), t.get("updated_at")) if status_code == "completed" else "—"

            row_data = [
                num,
                t.get("sku") or "—",
            ]
            if show_pool_col:
                row_data.append(t.get("pool_name") or "—")
            row_data.extend([
                status_display,
                designer_display,
                images_count if images_count is not None else "—",
                assigned_at_str,
                completed_at_str,
                lead_time,
                t.get("source_url") or "",
                t.get("internal_figma_url") or "",
                t.get("client_figma_url") or "",
                t.get("yandex_disk_url") or "",
                t.get("bitrix_url") or "",
                t.get("notes") or ""
            ])

            for col_idx, val in enumerate(row_data, 1):
                cell = ws.cell(row=row_idx, column=col_idx)
                cell.font = font_regular
                cell.border = cell_border

                # Handle hyperlinks
                if col_idx in (len(row_data) - 5, len(row_data) - 4, len(row_data) - 3, len(row_data) - 2, len(row_data) - 1) and str(val).startswith("http"):
                    cell.value = "Открыть ссылку"
                    cell.hyperlink = str(val)
                    cell.font = font_link
                    cell.alignment = align_center
                else:
                    cell.value = val
                    if col_idx == 1:
                        cell.alignment = align_center
                    elif col_idx == (3 if not show_pool_col else 4): # Status column
                        cell.alignment = align_center
                        if status_code in STATUS_COLORS:
                            bg_c, text_c = STATUS_COLORS[status_code]
                            cell.fill = PatternFill(start_color=bg_c, end_color=bg_c, fill_type="solid")
                            cell.font = Font(name="Calibri", size=11, bold=True, color=text_c)
                    elif col_idx == (4 if not show_pool_col else 5): # Images column
                        cell.alignment = align_right
                        if isinstance(val, (int, float)):
                            cell.font = font_bold
                    elif col_idx in ((5, 6, 7) if not show_pool_col else (6, 7, 8)):
                        cell.alignment = align_center
                    else:
                        cell.alignment = align_left

            row_idx += 1

        # Summary total row
        if len(sheet_tasks) > 0:
            last_data_row = row_idx - 1
            ws.cell(row=row_idx, column=1, value="ИТОГО").alignment = align_center
            ws.cell(row=row_idx, column=2, value=f"{len(sheet_tasks)} задач").alignment = align_left

            # Images column sum formula
            col_letter = get_column_letter(img_col_idx + 1)
            sum_formula = f"=SUM({col_letter}2:{col_letter}{last_data_row})"
            ws.cell(row=row_idx, column=img_col_idx + 1, value=sum_formula).alignment = align_right

            for c in range(1, len(headers) + 1):
                cell = ws.cell(row=row_idx, column=c)
                cell.font = font_bold
                cell.fill = fill_total
                cell.border = total_border

        # Auto width
        for col in ws.columns:
            max_len = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col:
                val_str = str(cell.value or "")
                if val_str.startswith("="):
                    val_str = "12345"
                max_len = max(max_len, len(val_str))
            ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 40)

    # 3. Create Pool Sheets
    # If there are multiple pools, create an "Все задачи" sheet first
    if len(pools_map) > 1:
        ws_all = wb.create_sheet(title=sanitize_sheet_title("Все задачи", existing_titles))
        build_tasks_sheet(ws_all, tasks, show_pool_col=True)

    # Create individual pool sheets
    for p_name, p_tasks in pools_map.items():
        sheet_title = sanitize_sheet_title(p_name, existing_titles)
        ws_pool = wb.create_sheet(title=sheet_title)
        build_tasks_sheet(ws_pool, p_tasks, show_pool_col=False)

    # Save to BytesIO
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output.getvalue()


def prepare_google_sheets_payload(tasks: List[Dict[str, Any]], pool_names: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Prepares a clean JSON structure with sheets and 2D arrays of values
    ready for Google Apps Script Webhook.
    """
    now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    pools_map: Dict[str, List[Dict[str, Any]]] = {}
    for t in tasks:
        p_name = (t.get("pool_name") or "Без пула").strip()
        pools_map.setdefault(p_name, []).append(t)

    # 1. Summary sheet
    total_tasks_count = len(tasks)
    completed_count = sum(1 for t in tasks if t.get("status") == "completed")
    in_progress_count = sum(1 for t in tasks if t.get("status") in ("in_progress", "internal_review", "client_review", "export_ready"))
    available_count = sum(1 for t in tasks if t.get("status") == "available")
    total_images_count = sum((t.get("images_count") or 0) for t in tasks)
    pct_completed = f"{(completed_count / total_tasks_count * 100):.1f}%" if total_tasks_count > 0 else "0.0%"

    summary_rows = [
        ["ОТЧЕТ ПО ЗАДАЧАМ И ПУЛАМ", ""],
        [f"Дата выгрузки: {now_str}", ""],
        ["", ""],
        ["ОБЩАЯ СТАТИСТИКА", ""],
        ["Показатель", "Значение"],
        ["Всего пулов в отчете", len(pools_map)],
        ["Всего задач (артикулов)", total_tasks_count],
        ["Выполнено задач", f"{completed_count} ({pct_completed})"],
        ["В работе / на проверке", in_progress_count],
        ["Свободно", available_count],
        ["Всего изображений (фото)", total_images_count],
        ["", ""],
        ["ВЫРАБОТКА ДИЗАЙНЕРОВ", "", "", "", ""],
        ["Дизайнер", "Telegram", "Выполнено задач", "В работе", "Всего изображений (фото)"]
    ]

    # Group designers
    designers_map: Dict[str, Dict[str, Any]] = {}
    for t in tasks:
        d_name = t.get("assigned_to_name")
        if not d_name:
            continue
        uname = f"@{t['assigned_to_username']}" if t.get("assigned_to_username") else "—"
        key = d_name.strip()
        rec = designers_map.setdefault(key, {
            "name": d_name,
            "username": uname,
            "completed": 0,
            "in_progress": 0,
            "images": 0
        })
        st = t.get("status")
        if st == "completed":
            rec["completed"] += 1
            rec["images"] += (t.get("images_count") or 0)
        elif st in ("in_progress", "internal_review", "client_review", "export_ready"):
            rec["in_progress"] += 1
            rec["images"] += (t.get("images_count") or 0)

    sorted_designers = sorted(designers_map.values(), key=lambda x: (x["images"], x["completed"]), reverse=True)
    for d in sorted_designers:
        summary_rows.append([d["name"], d["username"], d["completed"], d["in_progress"], d["images"]])

    summary_rows.extend([
        ["", "", "", "", "", "", ""],
        ["СТАТУС ПО ПУЛАМ", "", "", "", "", "", ""],
        ["Пул", "Всего задач", "Выполнено", "В работе", "Свободно", "Всего изображений", "Готовность"]
    ])

    for p_name, p_tasks in pools_map.items():
        p_total = len(p_tasks)
        p_comp = sum(1 for t in p_tasks if t.get("status") == "completed")
        p_act = sum(1 for t in p_tasks if t.get("status") in ("in_progress", "internal_review", "client_review", "export_ready"))
        p_free = sum(1 for t in p_tasks if t.get("status") == "available")
        p_img = sum((t.get("images_count") or 0) for t in p_tasks)
        p_pct = f"{(p_comp / p_total * 100):.1f}%" if p_total > 0 else "0.0%"
        summary_rows.append([p_name, p_total, p_comp, p_act, p_free, p_img, p_pct])

    def pad_to_rectangular_matrix(raw_rows: List[List[Any]]) -> List[List[Any]]:
        if not raw_rows:
            return []
        max_cols = max((len(r) for r in raw_rows), default=0)
        if max_cols == 0:
            return []
        padded = []
        for r in raw_rows:
            r_list = list(r)
            if len(r_list) < max_cols:
                r_list.extend([""] * (max_cols - len(r_list)))
            padded.append(r_list)
        return padded

    sheets = [
        {
            "title": "Сводка",
            "rows": pad_to_rectangular_matrix(summary_rows)
        }
    ]

    # Helper for task sheet rows
    def make_task_rows(sheet_tasks: List[Dict[str, Any]], with_pool: bool = False) -> List[List[Any]]:
        headers = ["№", "Артикул (SKU)"]
        if with_pool:
            headers.append("Пул")
        headers.extend([
            "Статус", "Дизайнер", "Изображений (шт.)", "Дата выдачи", "Дата завершения",
            "Время в работе", "Исходники", "Внутренняя Figma", "Клиентская Figma",
            "Яндекс.Диск", "Задача в Битрикс24", "Примечания"
        ])
        rows = [headers]
        for num, t in enumerate(sheet_tasks, 1):
            st = t.get("status") or "available"
            st_name = STATUS_NAMES.get(st, st)

            d_name = t.get("assigned_to_name") or "—"
            if t.get("assigned_to_username"):
                d_name += f" (@{t['assigned_to_username']})"

            r = [num, t.get("sku") or "—"]
            if with_pool:
                r.append(t.get("pool_name") or "—")
            r.extend([
                st_name,
                d_name,
                t.get("images_count") if t.get("images_count") is not None else 0,
                format_iso_datetime(t.get("assigned_at")),
                format_iso_datetime(t.get("updated_at")) if st == "completed" else "—",
                calculate_lead_time(t.get("assigned_at"), t.get("updated_at")) if st == "completed" else "—",
                t.get("source_url") or "",
                t.get("internal_figma_url") or "",
                t.get("client_figma_url") or "",
                t.get("yandex_disk_url") or "",
                t.get("bitrix_url") or "",
                t.get("notes") or ""
            ])
            rows.append(r)
        return rows

    if len(pools_map) > 1:
        sheets.append({
            "title": "Все задачи",
            "rows": pad_to_rectangular_matrix(make_task_rows(tasks, with_pool=True))
        })

    for p_name, p_tasks in pools_map.items():
        clean_title = re.sub(r'[\\/*?:\[\]]', '_', p_name).strip()[:30] or "Пул"
        sheets.append({
            "title": clean_title,
            "rows": pad_to_rectangular_matrix(make_task_rows(p_tasks, with_pool=False))
        })

    return {
        "generated_at": now_str,
        "sheets": sheets
    }


async def export_to_google_apps_script(
    webhook_url: str,
    tasks: List[Dict[str, Any]],
    pool_names: Optional[List[str]] = None
) -> Dict[str, Any]:
    url = webhook_url.strip()
    if not url.startswith("https://script.google.com/"):
        return {"ok": False, "error": "Ссылка должна начинаться с https://script.google.com/"}

    payload = prepare_google_sheets_payload(tasks, pool_names)

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            resp = await client.post(url, json=payload)
            if resp.status_code == 401:
                return {
                    "ok": False,
                    "status_code": 401,
                    "error": (
                        "Доступ к таблице отклонен (Google HTTP 401).\n\n"
                        "Как исправить в Google Apps Script:\n"
                        "1. Нажмите «Развернуть» ➔ «Управление развертываниями».\n"
                        "2. Нажмите иконку карандаша ✏️ (Редактировать).\n"
                        "3. В поле «У кого есть доступ» выберите «Все» (Anyone), а не «Только я».\n"
                        "4. В поле «Версия» выберите «Новая версия» и нажмите «Развернуть»."
                    )
                }
            elif resp.status_code == 403:
                return {
                    "ok": False,
                    "status_code": 403,
                    "error": (
                        "Доступ запрещен политиками безопасности Google (HTTP 403).\n"
                        "Убедитесь, что в настройках развертывания выбрано: «У кого есть доступ: Все» (Anyone)."
                    )
                }
            elif resp.status_code == 404:
                return {
                    "ok": False,
                    "status_code": 404,
                    "error": (
                        "Скрипт не найден (Google HTTP 404).\n"
                        "Проверьте ссылку: URL веб-приложения должен заканчиваться на /exec (а не /edit или /dev)."
                    )
                }
            elif resp.status_code >= 400:
                body_snippet = resp.text[:120].strip()
                return {
                    "ok": False,
                    "status_code": resp.status_code,
                    "error": f"Ошибка от Google: HTTP {resp.status_code}" + (f" ({body_snippet})" if body_snippet else "")
                }

            try:
                data = resp.json()
                if isinstance(data, dict):
                    if data.get("ok") is False:
                        return {
                            "ok": False,
                            "error": f"Ошибка внутри скрипта таблицы: {data.get('error', 'Неизвестная ошибка')}"
                        }
                    if data.get("ok") is True:
                        return {
                            "ok": True,
                            "sheets_count": len(payload["sheets"]),
                            "tasks_count": len(tasks),
                            "data": data
                        }
                return {
                    "ok": False,
                    "error": f"Скрипт Google Таблицы вернул непредвиденный ответ: {resp.text[:150]}"
                }
            except Exception:
                text_resp = resp.text
                if "doPost" in text_resp:
                    return {
                        "ok": False,
                        "error": (
                            "В проекте Google Apps Script не найдена функция doPost.\n\n"
                            "Как исправить:\n"
                            "1. Откройте редактор Apps Script в вашей Google Таблице.\n"
                            "2. Полностью удалите весь код (включая функцию myFunction) и вставьте скопированный код интеграции.\n"
                            "3. Обязательно нажмите иконку 💾 «Сохранить» (Ctrl+S).\n"
                            "4. Нажмите «Развернуть» ➔ «Новое развертывание» (тип: «Веб-приложение», доступ: «Все» / Anyone) и вставьте новую ссылку."
                        )
                    }
                return {
                    "ok": False,
                    "error": (
                        "Google вернул веб-страницу вместо JSON-ответа.\n"
                        "Убедитесь, что в «Управление развертываниями» для веб-приложения выбрано: «У кого есть доступ: Все» (Anyone)."
                    )
                }
        except httpx.RequestError as e:
            return {"ok": False, "error": f"Не удалось подключиться к Google: {str(e)}"}


GOOGLE_APPS_SCRIPT_TEMPLATE = """// ========================================================
// СКРИПТ ИНТЕГРАЦИИ CREATIVE UGC CRM -> GOOGLE ТАБЛИЦЫ
// ========================================================
// ИНСТРУКЦИЯ ПО УСТАНОВКЕ (1 МИНУТА):
// 1. В вашей Google Таблице откройте меню: Расширения -> Apps Script
// 2. Полностью удалите весь существующий код и вставьте этот текст.
// 3. Нажмите иконку 💾 «Сохранить проект» (Ctrl+S).
// 4. Нажмите синюю кнопку «Развернуть» (в правом верхнем углу) -> «Новое развертывание».
// 5. В типе развертывания выберите: «Веб-приложение».
// 6. В поле «У кого есть доступ» выберите: «Все» (Anyone).
// 7. Нажмите «Развернуть», разрешите доступ Google и скопируйте полученную ссылку (URL веб-приложения).
// 8. Вставьте эту ссылку в CRM в окно экспорта. Готово!

function doGet(e) {
  return ContentService
    .createTextOutput(JSON.stringify({ ok: true, message: "Creative UGC CRM Google Sheets webhook is active!" }))
    .setMimeType(ContentService.MimeType.JSON);
}

function doPost(e) {
  try {
    var payload = JSON.parse(e.postData.contents);
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var sheetsData = payload.sheets || [];
    
    sheetsData.forEach(function(sData) {
      var title = sData.title;
      var rawRows = sData.rows;
      if (!rawRows || rawRows.length === 0) return;
      
      // Находим максимальное число колонок
      var maxCols = 0;
      for (var r = 0; r < rawRows.length; r++) {
        if (rawRows[r] && rawRows[r].length > maxCols) {
          maxCols = rawRows[r].length;
        }
      }
      if (maxCols === 0) return;
      
      // Дополняем каждую строку до одинакового прямоугольного диапазона
      var rows = rawRows.map(function(row) {
        var rowArr = Array.isArray(row) ? row.slice() : [];
        while (rowArr.length < maxCols) {
          rowArr.push("");
        }
        return rowArr;
      });

      var sheet = ss.getSheetByName(title);
      if (!sheet) {
        sheet = ss.insertSheet(title);
      } else {
        sheet.clear();
      }
      
      // Запись данных
      var numRows = rows.length;
      var range = sheet.getRange(1, 1, numRows, maxCols);
      range.setValues(rows);
      
      // Форматирование первой строки (заголовок)
      var headerRange = sheet.getRange(1, 1, 1, maxCols);
      headerRange.setFontWeight("bold");
      headerRange.setBackground("#1E293B");
      headerRange.setFontColor("#FFFFFF");
      
      // Авто-ширина колонок
      for (var c = 1; c <= maxCols; c++) {
        sheet.autoResizeColumn(c);
      }
    });
    
    return ContentService
      .createTextOutput(JSON.stringify({ ok: true, sheets_updated: sheetsData.length }))
      .setMimeType(ContentService.MimeType.JSON);
  } catch(err) {
    return ContentService
      .createTextOutput(JSON.stringify({ ok: false, error: err.toString() }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}
"""

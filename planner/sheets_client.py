"""
واجهة مبسطة للتعامل مع Google Sheets عبر Service Account (GOOGLE_SERVICE_ACCOUNT_JSON).
تُستخدم من كل مكوّنات التخطيط (weekly_planner / stats_updater / run_from_plan)،
ومنفصلة تمامًا عن خط إنتاج الفيديو.

ملاحظة إعداد ضرورية (لمرة واحدة): لازم تشارك الـ Google Sheet نفسه مع بريد
الـ service account (الموجود داخل GOOGLE_SERVICE_ACCOUNT_JSON تحت المفتاح
"client_email") بصلاحية "Editor"، وإلا كل الاستدعاءات هنا سترجع 403.
"""

import os
import json
import logging

from google.oauth2 import service_account
from googleapiclient.discovery import build

from planner.config_planner import (
    SPREADSHEET_ID_ENV, SERVICE_ACCOUNT_JSON_ENV, TAB_CONFIG, TAB_PLAN, TAB_STATS,
)

logger = logging.getLogger("planner.sheets_client")

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_HEADERS = {
    TAB_CONFIG: [
        "week_number", "active_categories", "locked", "last_updated_utc", "tested_history",
        # ===== أعمدة التعلّم المستمر بعد القفل (جديدة) =====
        "last_reevaluation_utc",   # آخر مرة قورنت فيها الفئات التجريبية بالمقفلة
        "challenger_rotation_index",  # مؤشر دوّار على الفئات غير المقفلة (لتوزيع الاستكشاف بالتساوي بينها)
        "swap_log",                # سجل مختصر لأي استبدال فئة تم تلقائيًا (للتتبّع اليدوي)
        "current_challenger",      # الفئة التجريبية الحالية - تبقى ثابتة حتى تجمع عيّنة كافية حديثة قبل الانتقال للتالية
    ],
    TAB_PLAN: [
        "row_id", "category", "topic", "scheduled_date", "scheduled_time_et",
        "scheduled_datetime_utc", "status", "video_id", "notes",
        # ===== أعمدة جديدة لتغذية التعلّم لاحقًا =====
        "slot_bucket",     # أي نافذة زمنية مرشّحة استُخدمت فعليًا (لتعلّم أفضل وقت نشر)
        "is_exploration",  # TRUE إن كانت هذه الفئة "تجريبية" خارج الثلاث المقفلة
        "angle",           # زاوية/نمط الهوك الذي اقترحه جمناي لهذا الموضوع (لتعلّم أي الأنماط تنجح)
    ],
    TAB_STATS: [
        "video_id", "category", "topic", "title", "script", "published_at_utc",
        "check_24h_due_utc", "views_24h", "likes_24h", "comments_24h",
        "check_48h_due_utc", "views_48h", "likes_48h", "comments_48h",
        "check_72h_due_utc", "views_72h", "likes_72h", "comments_72h", "stats_complete",
        # ===== أعمدة جديدة: retention حقيقي + بيانات تعلّم =====
        "avg_view_percentage_72h",   # عبر YouTube Analytics API - المقياس الحقيقي لجودة السكربت/الهوك
        "avg_view_duration_sec_72h",
        "slot_bucket", "is_exploration", "angle",
    ],
}


def get_sheets_service():
    raw = os.environ.get(SERVICE_ACCOUNT_JSON_ENV)
    if not raw:
        raise EnvironmentError(f"{SERVICE_ACCOUNT_JSON_ENV} غير موجود في البيئة.")
    info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
    return build("sheets", "v4", credentials=creds)


def get_spreadsheet_id() -> str:
    sid = os.environ.get(SPREADSHEET_ID_ENV)
    if not sid:
        raise EnvironmentError(f"{SPREADSHEET_ID_ENV} غير موجود في البيئة.")
    return sid


def ensure_sheet_structure():
    """يتأكد من وجود التبويبات الثلاثة (Config/Plan/Stats) برؤوس أعمدة صحيحة،
    وينشئها تلقائيًا إن لم تكن موجودة (يحدث فعليًا في أول تشغيل فقط)."""
    service = get_sheets_service()
    sid = get_spreadsheet_id()
    meta = service.spreadsheets().get(spreadsheetId=sid).execute()
    existing = {s["properties"]["title"] for s in meta.get("sheets", [])}

    missing = [name for name in _HEADERS if name not in existing]
    if missing:
        requests = [{"addSheet": {"properties": {"title": name}}} for name in missing]
        service.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": requests}).execute()
        logger.info("تم إنشاء تبويبات جديدة في الشيت: %s", missing)

    for name, cols in _HEADERS.items():
        header_result = service.spreadsheets().values().get(
            spreadsheetId=sid, range=f"{name}!A1:ZZ1"
        ).execute()
        existing_header = header_result.get("values", [[]])
        existing_header = existing_header[0] if existing_header else []

        if not existing_header:
            service.spreadsheets().values().update(
                spreadsheetId=sid, range=f"{name}!A1",
                valueInputOption="USER_ENTERED", body={"values": [cols]},
            ).execute()
            logger.info("تم كتابة صف العناوين لتبويب %s.", name)
            continue

        # ترحيل آمن: لو التبويب موجود من نسخة سابقة وينقصه أعمدة جديدة (مثل
        # avg_view_percentage_72h/slot_bucket/angle)، نُلحقها في نهاية صف
        # العناوين فقط - لا نغيّر ترتيب/موضع الأعمدة القديمة أبدًا، حتى لا
        # تنزاح بيانات الصفوف المكتوبة سابقًا عن أعمدتها الصحيحة.
        missing = [c for c in cols if c not in existing_header]
        if missing:
            new_header = existing_header + missing
            end_col = _col_letter(len(new_header))
            service.spreadsheets().values().update(
                spreadsheetId=sid, range=f"{name}!A1:{end_col}1",
                valueInputOption="USER_ENTERED", body={"values": [new_header]},
            ).execute()
            logger.info("تمت إضافة أعمدة جديدة لتبويب %s (ترحيل تلقائي): %s", name, missing)


def read_all(tab_name: str) -> list[dict]:
    """يقرأ كل صفوف التبويب ويعيدها كقائمة قواميس حسب صف العناوين (الصف الأول).
    كل قاموس يحمل أيضًا _row_number (رقم الصف الفعلي في الشيت) لتسهيل التحديث لاحقًا."""
    service = get_sheets_service()
    sid = get_spreadsheet_id()
    result = service.spreadsheets().values().get(
        spreadsheetId=sid, range=f"{tab_name}!A1:Z20000"
    ).execute()
    values = result.get("values", [])
    if not values:
        return []
    headers = values[0]
    rows = []
    for i, row in enumerate(values[1:], start=2):  # start=2: أول صف بيانات فعلي بعد العناوين
        row = row + [""] * (len(headers) - len(row))  # إكمال الأعمدة الناقصة (Sheets يقصّ الفراغات)
        record = dict(zip(headers, row))
        record["_row_number"] = i
        rows.append(record)
    return rows


def append_rows(tab_name: str, rows: list[list]):
    """يضيف صفوف جديدة في نهاية التبويب."""
    if not rows:
        return
    service = get_sheets_service()
    sid = get_spreadsheet_id()
    service.spreadsheets().values().append(
        spreadsheetId=sid, range=f"{tab_name}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def update_row(tab_name: str, row_number: int, values: list, start_col: str = "A"):
    """يحدّث صفًا (أو جزءًا منه بدءًا من start_col) برقم صف معروف مسبقًا."""
    service = get_sheets_service()
    sid = get_spreadsheet_id()
    end_col = _col_letter(_col_index(start_col) + len(values) - 1)
    range_ = f"{tab_name}!{start_col}{row_number}:{end_col}{row_number}"
    service.spreadsheets().values().update(
        spreadsheetId=sid, range=range_,
        valueInputOption="USER_ENTERED",
        body={"values": [values]},
    ).execute()


def update_cell(tab_name: str, row_number: int, col_letter: str, value):
    update_row(tab_name, row_number, [value], start_col=col_letter)


def _col_index(letter: str) -> int:
    idx = 0
    for ch in letter:
        idx = idx * 26 + (ord(ch.upper()) - ord("A") + 1)
    return idx


def _col_letter(idx: int) -> str:
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters

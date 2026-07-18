"""
sheets_client.py
واجهة موحدة للتعامل مع Google Sheets (Current_Plan, Daily_Log, Trend_Log, System_Control).
يستخدم Service Account (GOOGLE_SERVICE_ACCOUNT_JSON) بدل OAuth تفاعلي.
"""
import json
import gspread
import traceback
import base64
from google.oauth2.service_account import Credentials
from scripts import config

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _client():
    config.require("GOOGLE_SERVICE_ACCOUNT_JSON")
    
    # تنظيف النص من أي مسافات زائدة
    raw_data = config.GOOGLE_SERVICE_ACCOUNT_JSON.strip()
    
    # محاولة فك التشفير إذا كان النص Base64، وإلا قراءته كنص عادي مباشر
    try:
        decoded_bytes = base64.b64decode(raw_data)
        decoded_str = decoded_bytes.decode("utf-8")
        creds_dict = json.loads(decoded_str)
    except Exception:
        # مسار احتياطي في حال كان النص عادي وليس Base64
        creds_dict = json.loads(raw_data)
        
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def get_sheet(spreadsheet_id: str, worksheet_name: str):
    gc = _client()
    sh = gc.open_by_key(spreadsheet_id)
    return sh.worksheet(worksheet_name)


def append_row(spreadsheet_id: str, worksheet_name: str, row: list):
    ws = get_sheet(spreadsheet_id, worksheet_name)
    ws.append_row(row, value_input_option="USER_ENTERED")


def get_all_records(spreadsheet_id: str, worksheet_name: str) -> list[dict]:
    ws = get_sheet(spreadsheet_id, worksheet_name)
    return ws.get_all_records()


def update_cell_by_row_match(spreadsheet_id: str, worksheet_name: str,
                              match_column: str, match_value: str,
                              target_column: str, new_value: str):
    """يبحث عن صف بقيمة معينة بعمود معين ويحدّث عمود آخر بنفس الصف."""
    ws = get_sheet(spreadsheet_id, worksheet_name)
    headers = ws.row_values(1)
    match_idx = headers.index(match_column) + 1
    target_idx = headers.index(target_column) + 1

    cell = ws.find(match_value, in_column=match_idx)
    if cell is None:
        raise ValueError(f"لم يتم العثور على '{match_value}' بعمود '{match_column}'")
    ws.update_cell(cell.row, target_idx, new_value)


def is_system_enabled(spreadsheet_id: str) -> bool:
    """يقرأ زر System_Control — لو 'OFF' يوقف كل العمليات فوراً."""
    try:
        ws = get_sheet(spreadsheet_id, config.Paths().sheets_system_control)
        status = ws.acell("A1").value
        return (status or "ON").strip().upper() != "OFF"
    except Exception as e:
        print("\n" + "="*50)
        print("🚨 الخطأ الحقيقي الذي أوقف النظام:")
        print(f"نوع الخطأ: {e}")
        print("تفاصيل الخطأ الكاملة:")
        traceback.print_exc()
        print("="*50 + "\n")
        # لو فشل القراءة، الأفضل نوقف احتياطاً بدل ما نكمل بشكل أعمى
        return False

"""
weekly_backup.py
يعمل كل يوم أحد. يسحب كل الجداول ويحفظها كـ CSV داخل مجلد backups/ بالريبو
نفسه (commit تلقائي)، لحمايتها من الضياع في حال حدث خطأ بجدول Sheets نفسه.
"""
import os
import csv
from datetime import date
from scripts import config, sheets_client

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
BACKUP_DIR = "backups"


def backup_sheet(worksheet_name: str):
    records = sheets_client.get_all_records(SPREADSHEET_ID, worksheet_name)
    if not records:
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    out_path = f"{BACKUP_DIR}/{worksheet_name}_{date.today().isoformat()}.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=records[0].keys())
        writer.writeheader()
        writer.writerows(records)
    print(f"تم النسخ الاحتياطي: {out_path}")


def run():
    paths = config.Paths()
    for sheet in [paths.sheets_current_plan, paths.sheets_daily_log, paths.sheets_trend_log]:
        backup_sheet(sheet)
    # الـ commit والـ push يتم عبر خطوة git بملف الـ workflow نفسه (weekly_backup.yml)


if __name__ == "__main__":
    run()

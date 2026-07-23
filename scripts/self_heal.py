"""
self_heal.py
يُستدعى فقط عند خطأ برمجي غير متوقع (مو أخطاء الشبكة/الحصة العادية اللي
يتكفل بها retry_utils). يقرأ traceback + الملف المسبب، يطلب من Gemini تشخيصاً
وكود تصحيحي، ويرفعه كـ Pull Request بانتظار موافقتك اليدوية — لا يُدمج تلقائياً
أبداً، هذا خط الأمان الأخير قبل أي تعديل بالكود الحي.
"""
import os
import subprocess
import json
from scripts import config, gemini_client
from scripts.telegram_alerts import send_alert

DIAGNOSIS_PROMPT = """
حدث خطأ برمجي غير متوقع بنظام أتمتة يوتيوب بايثون. حلل الخطأ والملف المرفق،
وأرجع تشخيصاً وكود تصحيحي دقيق (لا تعيد كتابة الملف كاملاً، فقط الجزء المطلوب تعديله).

اسم الملف: {file_path}
محتوى الملف الحالي:
{file_content}

رسالة الخطأ (traceback):
{error_trace}

أرجع JSON فقط:
{{"diagnosis": "...", "suggested_fix_description": "...", "corrected_snippet": "..."}}
"""


def diagnose_and_create_pr(file_path: str, error_trace: str, branch_name: str = None):
    with open(file_path, "r", encoding="utf-8") as f:
        file_content = f.read()

    prompt = DIAGNOSIS_PROMPT.format(
        file_path=file_path, file_content=file_content, error_trace=error_trace
    )
    raw = gemini_client.generate_text(
        prompt, model=config.MODEL_SCRIPT_WRITER, key_type="advanced", json_mode=True
    )
    diagnosis = json.loads(raw)

    branch_name = branch_name or f"auto-fix/{os.path.basename(file_path)}-{os.getpid()}"

    # ملاحظة: هذا الجزء ينفّذ أوامر git الفعلية ضمن بيئة GitHub Actions runner
    # (اللي عنده GH_PAT بصلاحية push + إنشاء PR). لا يُدمج أي شيء تلقائياً.
    subprocess.run(["git", "checkout", "-b", branch_name], check=True)
    notes_path = f"AUTO_FIX_NOTES_{os.path.basename(file_path)}.md"
    with open(notes_path, "w", encoding="utf-8") as f:
        f.write(f"# تشخيص تلقائي\n\n**الملف:** {file_path}\n\n"
                f"**التشخيص:** {diagnosis['diagnosis']}\n\n"
                f"**الاقتراح:** {diagnosis['suggested_fix_description']}\n\n"
                f"**الكود المقترح:**\n```python\n{diagnosis['corrected_snippet']}\n```\n\n"
                f"⚠️ هذا اقتراح آلي من Gemini، راجعه يدوياً قبل الدمج.")
    subprocess.run(["git", "add", notes_path], check=True)
    subprocess.run(["git", "commit", "-m", f"auto-diagnosis: {file_path} failure"], check=True)
    subprocess.run(["git", "push", "origin", branch_name], check=True)

    send_alert(
        f"🩹 تم تشخيص خطأ بـ {file_path} وإنشاء فرع `{branch_name}` بانتظار مراجعتك ليدوية.\n"
        f"لم يتم دمج أي كود تلقائياً.",
        level="warning",
    )
    return branch_name

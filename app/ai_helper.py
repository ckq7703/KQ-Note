import json
import os
import re
import threading
import urllib.request
import urllib.error

# Default fallback free Gemini API Key (if user has not set their own in settings)
DEFAULT_GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")

# Priority list of known Gemini models
MODELS_TO_TRY = [
    "gemini-1.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-pro",
    "gemini-2.5-flash",
    "gemini-2.0-pro-exp-0205",
]

SYSTEM_INSTRUCTION = (
    "Bạn là KQ AI Assistant - Trợ lý soạn thảo và chuẩn hóa định dạng Markdown cho ứng dụng KQ Note.\n\n"
    "CÁC QUY TẮC BẮT BUỘC VỀ ĐỊNH DẠNG MARKDOWN (STRICT MARKDOWN RULES):\n"
    "1. LUÔN LUÔN BẢO TOÀN VÀ SỬ DỤNG CÚ PHÁP MARKDOWN CHUẨN:\n"
    "   - Tiêu đề (Headings) BẮT BUỘC phải dùng dấu #: `# Tiêu đề chính`, `## Tiêu đề phụ`, `### Tiêu đề cấp 3`.\n"
    "   - Tất cả các câu lệnh, lệnh SSH/Linux/Git/Nmap/SQL/Docker BẮT BUỘC phải bọc trong khối codeblock Markdown (ví dụ: ```bash ... ``` hoặc ```text ... ```).\n"
    "   - Danh sách dùng `- ` hoặc `1. `.\n"
    "   - Checkbox dùng `- [ ] ` hoặc `- [x] `.\n"
    "2. TUYỆT ĐỐI KHÔNG BIẾN ĐỔI TIÊU ĐỀ HOẶC CÂU LỆNH THÀNH VĂN BẢN THƯỜNG (PLAIN TEXT). Không được xóa bỏ dấu `#` hoặc các khối code ```.\n"
    "3. KHI SỬA HOẶC XÓA NỘI DUNG, BẠN PHẢI GIỮ NGUYÊN 100% CẤU TRÚC MARKDOWN CỦA CÁC ĐOẠN CÒN LẠI (bao gồm thẻ ảnh `![](kqnote-image:...)`).\n"
    "4. KHÔNG KÈM LỜI CHÀO, KHÔNG KÈM LỜI DẪN (như 'Dưới đây là...', 'Đây là...'). CHỈ TRẢ VỀ ĐÚNG NỘI DUNG MARKDOWN HOÀN CHỈNH."
)


def clean_markdown_output(text):
    """
    Sanitize AI output: strip conversational intros/outros and ONLY outer ```markdown wrapper.
    """
    if not text:
        return ""
    s = text.strip()

    # Remove intro conversational phrases
    intro_patterns = [
        r"^Dưới đây là[^\n]*:\n*",
        r"^Đây là[^\n]*:\n*",
        r"^Sau khi[^\n]*:\n*",
        r"^Dưới đây là nội dung[^\n]*:\n*",
        r"^Here is[^\n]*:\n*",
        r"^Dưới đây là bài viết[^\n]*:\n*",
    ]
    for pat in intro_patterns:
        s = re.sub(pat, "", s, flags=re.IGNORECASE).strip()

    # ONLY strip wrapper if the top line is strictly ```markdown or ```md (NOT ```bash, ```python, etc.)
    lines = s.split("\n")
    if len(lines) >= 2:
        first_line = lines[0].strip().lower()
        last_line = lines[-1].strip()
        if first_line in ("```markdown", "```md") and last_line == "```":
            s = "\n".join(lines[1:-1]).strip()

    return s


def get_available_models(api_key=None):
    """
    Fetch all models supporting generateContent from Google API.
    Returns list of model names.
    """
    key = api_key or DEFAULT_GEMINI_KEY
    if not key:
        return []

    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "KQNote/1.3"})
        with urllib.request.urlopen(req, timeout=10) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            models = res_data.get("models", [])
            valid_models = []
            for m in models:
                methods = m.get("supportedGenerationMethods", [])
                if "generateContent" in methods:
                    name = m.get("name", "").replace("models/", "")
                    valid_models.append(name)
            return valid_models
    except Exception:
        return []


def query_gemini_api(prompt, context="", api_key=None, preferred_model=None, chat_history=None):
    """
    Call Google Gemini REST API with multi-turn chat history and automatic model fallback.
    Returns (success: bool, text_response: str, model_used: str)
    """
    key = api_key or DEFAULT_GEMINI_KEY
    if not key:
        return False, "Chưa cấu hình Gemini API Key. Vui lòng nhấp nút ⚙️ Key để nhập API Key từ Google AI Studio.", ""

    dynamic_models = get_available_models(key)
    models_queue = []

    if preferred_model:
        models_queue.append(preferred_model)

    for m in dynamic_models:
        if "flash" in m and m not in models_queue:
            models_queue.append(m)

    for m in dynamic_models:
        if m not in models_queue:
            models_queue.append(m)

    for m in MODELS_TO_TRY:
        if m not in models_queue:
            models_queue.append(m)

    contents = []

    # Include multi-turn conversation history
    if chat_history and isinstance(chat_history, list):
        for turn in chat_history:
            role = turn.get("role", "user")
            text = turn.get("text", "")
            if text:
                contents.append({
                    "role": "user" if role == "user" else "model",
                    "parts": [{"text": text}]
                })

    # Prepare current prompt turn
    current_prompt = SYSTEM_INSTRUCTION
    if context.strip():
        current_prompt += f"\n\n[NỘI DUNG GHI CHÚ HIỆN TẠI IN EDITOR]:\n{context.strip()}"
    current_prompt += f"\n\n[YÊU CẦU NGUYÊN BẢN CỦA NGƯỜI DÙNG]:\n{prompt.strip()}"

    contents.append({
        "role": "user",
        "parts": [{"text": current_prompt}]
    })

    payload = {"contents": contents}
    json_data = json.dumps(payload).encode("utf-8")

    last_err_msg = ""
    for idx, model in enumerate(models_queue):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        try:
            req = urllib.request.Request(
                url,
                data=json_data,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=25) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                candidates = res_data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        raw_ans = parts[0].get("text", "")
                        clean_ans = clean_markdown_output(raw_ans)
                        return True, clean_ans, model
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            try:
                err_json = json.loads(err_body)
                msg = err_json.get("error", {}).get("message", str(e))
            except Exception:
                msg = str(e)
            last_err_msg = f"Model {model} (HTTP {e.code}): {msg}"
            if idx < len(models_queue) - 1:
                continue
            return False, f"Lỗi Gemini API ({e.code}): {msg}", model
        except Exception as e:
            last_err_msg = f"Model {model}: {str(e)}"
            if idx < len(models_queue) - 1:
                continue
            return False, f"Lỗi kết nối AI: {str(e)}", model

    return False, f"Không thể kết nối Gemini API. {last_err_msg}", ""


def ask_gemini_async(prompt, context="", callback=None, api_key=None, preferred_model=None, chat_history=None):
    """
    Run query_gemini_api in a background thread.
    callback(success: bool, result_text: str, model_used: str) will be called when completed.
    """
    def _worker():
        success, text, model_used = query_gemini_api(prompt, context, api_key, preferred_model, chat_history)
        if callback:
            callback(success, text, model_used)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

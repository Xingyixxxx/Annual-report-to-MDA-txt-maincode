import os
import glob
import json
import time
import requests
import pdfplumber
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

# --- Configuration ---
INPUT_FOLDER = "Input"
OUTPUT_FOLDER = "Output"
ERROR_LOG_FILE = os.path.join(OUTPUT_FOLDER, "extraction_errors.json")

# API Configuration
# Replace these with your actual API keys and URL
API_KEYS = [
<<<<<<< HEAD
    "XXXXXXXX",
=======
    "sk-fxtczrxvvtxozyfywwbrmdneqtwgkplggvjlbvcbjzirivdw",
>>>>>>> b753144 (feat: 实现使用LLM从年度报告中提取管理层讨论与分析(MDA)部分的功能，支持PDF文本高级清洗、两阶段扫描及API限流。)
]
API_URL = "https://api.siliconflow.cn/v1" # Example: OpenAI Endpoint
MODEL_NAME = "Pro/deepseek-ai/DeepSeek-V3.2" # Example Model

# Rate Limiting
REQUESTS_PER_MINUTE = 20 # Conservative limit as requested
TOKENS_PER_MINUTE_LIMIT = 2000000 # Not strictly enforced in this simple implementation, relying on Req/Min

# --- Helper Functions ---

def remove_duplicate_chars(text):
    """
    Removes duplicate characters unless they are alphanumeric (A-Z, a-z, 0-9).
    """
    if not text: return ""
    result = []
    if len(text) > 0: result.append(text[0])
    for i in range(1, len(text)):
        char = text[i]
        if char != text[i-1]:
            result.append(char)
        elif ('a' <= char <= 'z') or ('A' <= char <= 'Z') or ('0' <= char <= '9'):
            result.append(char)
    return "".join(result)

def clean_special_chars(text):
    if not text: return ""
    return "".join(ch for ch in text if ch.isprintable() or ch in '\n\t')

def extract_text_for_content(page, aggressive_crop=True):
    width = page.width
    height = page.height
    
    # 1. Crop (Top 10%, Bottom 10%)
    if aggressive_crop:
        try:
            cropped = page.crop((0, height * 0.1, width, height * 0.9))
        except ValueError:
            cropped = page
    else:
        cropped = page

    # 2. Table Filtering
    tables = cropped.find_tables()
    def not_within_tables(obj):
        obj_x0 = obj.get("x0", 0)
        obj_top = obj.get("top", 0)
        obj_x1 = obj.get("x1", 0)
        obj_bottom = obj.get("bottom", 0)
        cx = (obj_x0 + obj_x1) / 2
        cy = (obj_top + obj_bottom) / 2
        for table in tables:
            bbox = table.bbox
            if (bbox[0] <= cx <= bbox[2]) and (bbox[1] <= cy <= bbox[3]):
                return False
        return True

    try:
        if tables:
            filtered = cropped.filter(not_within_tables)
            text = filtered.extract_text()
        else:
            text = cropped.extract_text()
    except:
        text = cropped.extract_text()
        
    if not text: return ""
    text = clean_special_chars(text)
    text = remove_duplicate_chars(text) # Always dedup content
    return text

def read_pdf_pages(pdf_path, start_page=1, end_page=50):
    """
    Reads text from specific pages of a PDF using advanced cleaning.
    Pages are 1-indexed.
    """
    text_content = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            total_pages = len(pdf.pages)
            for i in range(start_page - 1, min(end_page, total_pages)):
                page = pdf.pages[i]
                # Use the robust extraction function
                page_text = extract_text_for_content(page)
                text_content.append(page_text)
    except Exception as e:
        print(f"Error reading PDF {pdf_path}: {e}")
        return None
    return "\n".join(text_content)

# --- API Interaction & Rate Limiting ---

class APIKeyManager:
    def __init__(self, api_keys, requests_per_min):
        self.api_keys = api_keys
        self.requests_per_min = requests_per_min
        self.request_timestamps = []
        self.lock = Lock()
        self.key_index = 0

    def get_key(self):
        """Rotates through API keys."""
        with self.lock:
            key = self.api_keys[self.key_index]
            self.key_index = (self.key_index + 1) % len(self.api_keys)
            return key

    def wait_for_rate_limit(self):
        """Blocks until a request can be made within the rate limit."""
        width = 60.0 # Window size in seconds
        with self.lock:
            now = time.time()
            # Remove timestamps older than the window
            self.request_timestamps = [t for t in self.request_timestamps if now - t < width]
            
            if len(self.request_timestamps) >= self.requests_per_min:
                # Calculate sleep time needed
                oldest = self.request_timestamps[0]
                sleep_time = width - (now - oldest) + 0.1 # Add buffer
                if sleep_time > 0:
                    time.sleep(sleep_time)
            
            self.request_timestamps.append(time.time())

api_manager = APIKeyManager(API_KEYS, REQUESTS_PER_MINUTE)

def call_llm_api(file_name, pdf_text):
    """
    Calls the LLM API to identify MDA section coordinates.
    """
    prompt = f"""
请帮我在以下A股公司年报中定位"管理层讨论与分析"(MDA)部分的精确位置。
这部分标题可能是："管理层讨论与分析|董事会报告|董事会工作报告|董事局报告|经营情况讨论与分析等。可能是繁体字。

请严格按照以下格式返回位置信息:
{{
  "start_page": 数字(第几页开始，1-based),
  "end_page": 数字(第几页结束，1-based),
  "start_keyword": "找到的标题完整文本",
  "confidence": 0.0到1.0之间的数值，表示对结果的置信度
}}

只返回上述格式的位置信息JSON，不要添加其他内容。

文件名: {file_name}

年报文本:
{pdf_text[:100000]} 
""" 
# Note: Truncating text to avoid hitting context limits blindly, though 50 pages should fit in 128k context models.
# Adjust per model capability.

    api_manager.wait_for_rate_limit()
    key = api_manager.get_key()
    
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant that extracts structural information from financial reports."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"} # Useful for newer OpenAI models
    }

    # Handle API URL appending (User often provides base URL)
    endpoint = API_URL
    if not endpoint.endswith("/chat/completions"):
        endpoint = endpoint.rstrip("/") + "/chat/completions"

    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        result = response.json()
        content = result['choices'][0]['message']['content']
        return json.loads(content)
    except Exception as e:
        print(f"API Call Failed for {file_name}: {e}")
        return None

# --- Main Processing ---

def process_file(file_path):
    file_name = os.path.basename(file_path)
    print(f"Processing: {file_name}")
    
    # --- Stage 1: Fast Scan (Page 1-50) ---
    print(f"  -> Stage 1: Scanning pages 1-50...")
    # read_pdf_pages now already returns cleaned, deduped text
    cleaned_text_s1 = read_pdf_pages(file_path, 1, 50)
    if not cleaned_text_s1:
        return {"file": file_name, "error": "Empty or unreadable PDF"}
        
    location_data = call_llm_api(file_name, cleaned_text_s1)
    
    # Decision Logic for Stage 2
    need_retry = False
    
    if not location_data:
        # API fail or empty -> Retry might help if it was a context issue? Unlikely, but if LLM said specific error maybe. 
        # But if just None, maybe we leave it.
        need_retry = True
    else:
        start_page = location_data.get("start_page")
        end_page = location_data.get("end_page")
        
        # Condition A: Not found at all
        if not start_page or not end_page:
            print(f"  -> Stage 1 inconclusive (Start: {start_page}, End: {end_page}). Preparing Stage 2...")
            need_retry = True
        
        # Condition B: Found, but ends dangerously close to cutoff (e.g. > 45)
        # This implies it might be truncated in reality, LLM just guessed the last visible page.
        elif end_page >= 45:
            print(f"  -> Stage 1 result near cutoff (Ends at {end_page}). Extending scan...")
            need_retry = True
            
        else:
            print(f"  -> Stage 1 Success: MDA found at {start_page}-{end_page}")

    # --- Stage 2: Deep Scan (Page 1-150) if needed ---
    if need_retry:
        print(f"  -> Stage 2: Scanning pages 1-150...")
        # Re-read with larger window
        cleaned_text_s2 = read_pdf_pages(file_path, 1, 150)
        
        # Call API again
        location_data = call_llm_api(file_name, cleaned_text_s2)
        
        if not location_data:
             return {"file": file_name, "error": "API extraction failed after Stage 2"}
             
        start_page = location_data.get("start_page")
        end_page = location_data.get("end_page")
        
        if not start_page or not end_page:
             return {"file": file_name, "error": "Could not locate MDA (start/end missing) even in 150 pages", "raw_response": location_data}
             
        print(f"  -> Stage 2 Result: MDA found at {start_page}-{end_page}")

    # --- Extraction Phase ---
    try:
        final_text = []
        with pdfplumber.open(file_path) as pdf:
            total_pages = len(pdf.pages)
            start_idx = max(0, start_page - 1)
            end_idx = min(total_pages, end_page) 
            
            for i in range(start_idx, end_idx):
                page = pdf.pages[i]
                # Use the robust extraction function for final output too
                content = extract_text_for_content(page)
                final_text.append(content)
        
        final_content = "\n".join(final_text)
        
        if not os.path.exists(OUTPUT_FOLDER):
            os.makedirs(OUTPUT_FOLDER)
            
        output_path = os.path.join(OUTPUT_FOLDER, file_name.replace(".pdf", ".txt"))
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(final_content)
            
        print(f"Successfully saved {output_path}")
        return None 

    except Exception as e:
        return {"file": file_name, "error": f"Extraction error: {e}"}

def main():
    if not os.path.exists(INPUT_FOLDER):
        print(f"Input folder '{INPUT_FOLDER}' does not exist.")
        return

    pdf_files = glob.glob(os.path.join(INPUT_FOLDER, "*.pdf"))
    if not pdf_files:
        print("No PDF files found in input folder.")
        return

    print(f"Found {len(pdf_files)} PDFs. Starting processing...")
    print("==================================================")
    print(f"  Script: extract_mda_llm.py")
    print(f"  Mode:   LLM API Extraction")
    print(f"  Model:  {MODEL_NAME}")
    print("==================================================")
    
    # Using ThreadPool for concurrency
    errors = []
    # Max workers = API rate limit / (60 / expected_latency)? 
    # Or just keep it small to match the request rate. 
    # Since we rate limit nicely, we can have more threads, they will just block.
    # But let's keep it reasonable, e.g., 5 workers.
    
    with ThreadPoolExecutor(max_workers=5) as executor:
        results = executor.map(process_file, pdf_files)
        
        for res in results:
            if res:
                errors.append(res)

    if errors:
        print(f"Completed with {len(errors)} errors. Saving log...")
        with open(ERROR_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(errors, f, ensure_ascii=False, indent=2)
        print(f"Error log saved to {ERROR_LOG_FILE}")
    else:
        print("All files processed successfully.")

if __name__ == "__main__":
    main()

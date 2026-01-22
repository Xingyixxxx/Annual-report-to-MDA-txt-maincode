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
    "XXXXXXXXXXXX",
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

def read_pdf_pages(pdf_path, start_page=1, end_page=50, add_markers=False):
    """
    Reads text from specific pages of a PDF using advanced cleaning.
    Pages are 1-indexed.
    
    Args:
        add_markers (bool): If True, adds "「{page_num}」" at the start of each page's text.
    """
    text_content = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            total_pages = len(pdf.pages)
            for i in range(start_page - 1, min(end_page, total_pages)):
                page = pdf.pages[i]
                current_page_num = i + 1
                
                # Use the robust extraction function
                page_text = extract_text_for_content(page)
                
                if add_markers:
                    # Add marker at the beginning of the page content
                    # Format: 「1」\nContent...
                    marked_text = f"「{current_page_num}」\n{page_text}"
                    text_content.append(marked_text)
                else:
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
    Calls the LLM API to identify MDA section coordinates from the Table of Contents.
    """
    prompt = f"""
请阅读以下A股公司年报的前20页内容（包含目录）。
你的任务是：
1. 在目录中找到"管理层讨论与分析"（或类似标题，如"董事会报告"、"经营情况讨论与分析"）的章节，提取目录中显示的开始页码和结束页码。
2. 计算"目录页码"与"PDF真实页码"之间的差值 (page_offset)。
   计算原理：文本中每一页的开头都有标记「页码」（例如「5」表示这是PDF的第5页）。
   请找出一个在目录中也出现的章节（如"公司简介"或"管理层讨论与分析"本身），对比其"目录显示的页码"与"文本中标记的真实页码"。
   公式：page_offset = 真实页码 - 目录页码。
   
请严格按照以下JSON格式返回，不要包含其他解释：
{{
  "start_page": 目录显示的开始页码 (int),
  "end_page": 目录显示的结束页码 (int),
  "page_offset": 计算出的差值 (int),
  "start_keyword": "找到的目录标题文本",
  "reasoning": "计算差值的依据简述(例如: 目录说公司简介在第4页，但标记显示在第6页)"
}}

文件名: {file_name}

前20页文本 (含真实页码标记):
{pdf_text} 
""" 
# Note: Text should already be truncated/selected before calling this.

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
        "response_format": {"type": "json_object"} 
    }

    # Handle API URL appending
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
    
    # --- Only One Stage: Read TOC (Pages 1-20) ---
    print(f"  -> Reading pages 1-20 to find Directory and Calculate Offset...")
    
    # Enable explicit page markers
    cleaned_text = read_pdf_pages(file_path, 1, 20, add_markers=True)
    
    if not cleaned_text:
        return {"file": file_name, "error": "Empty or unreadable PDF"}
        
    location_data = call_llm_api(file_name, cleaned_text)
    
    if not location_data:
        return {"file": file_name, "error": "LLM extraction failed"}
        
    start_page_doc = location_data.get("start_page")
    end_page_doc = location_data.get("end_page")
    offset = location_data.get("page_offset", 0)
    
    if not start_page_doc: # end_page might be None if implicit
        return {"file": file_name, "error": "Could not find MDA in Directory", "raw_response": location_data}
        
    # Calculate Actual Pages
    # Result = DocPage + Offset
    # Note: Sometimes offset can be negative? Usually Positive (PDF starts with covers).
    # If offset is None, assume 0.
    if offset is None: offset = 0
    
    actual_start = int(start_page_doc + offset)
    actual_end = int(end_page_doc + offset) if end_page_doc else int(actual_start + 50) # Fail safe 50 pages if no end
    
    print(f"  -> Found in Directory: {start_page_doc}-{end_page_doc} | Offset: {offset}")
    print(f"  -> Actual Extraction Range: {actual_start}-{actual_end}")

    # --- Extraction Phase ---
    try:
        final_text = []
        with pdfplumber.open(file_path) as pdf:
            total_pages = len(pdf.pages)
            # Use calculated actual pages
            start_idx = max(0, actual_start - 1)
            end_idx = min(total_pages, actual_end) 
            
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

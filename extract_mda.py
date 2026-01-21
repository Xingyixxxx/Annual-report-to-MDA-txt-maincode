import pdfplumber
import re
import os
import glob

# Configuration
PDF_DIR = "PDF"
OUTPUT_DIR = "OUTPUT"
START_PAGE_RANGE = (3, 25) # 0-indexed, so Page 4-25

# Matches: "Chapter 4/IV/Four Management Discussion..."
START_PATTERNS = [
    r"^第([一二三四五六七八九十0-9]{1,3})(?:章|节)\s*(?:管理层讨论与分析|董事会报告|董事会工作报告|董事局报告|经营情况讨论与分析)\s*$",
    r"^\s*([一二三四五六七八九十0-9]+)[、，：:]\s*(?:管理层讨论与分析|董事会报告|董事会工作报告|董事局报告|经营情况讨论与分析)\s*$"
]

# Matches: Next Chapter Header (e.g. "Chapter 5 Important Matters")
FALLBACK_END_PATTERNS = [
    r"^\s*(?:第\s*)?(?:[一二三四五六七八九十0-9]+)\s*[章节]?\s*[：:]*\s*(?:重要事项|公司治理|财务报告|企业管治报告|监事会报告|回顾)",
    r"^\s*(?:重要事项|公司治理|财务报告|企业管治报告|监事会报告)\s*$"
]

# --- UTILS ---
CN_NUM = {
    '零': 0, '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9,
    '十': 10
}

def chinese_to_number(s):
    if not s: return 0
    if s.isdigit(): return int(s)
    if s == '十': return 10
    if len(s) == 2 and s.startswith('十'): return 10 + CN_NUM.get(s[1], 0)
    if len(s) == 2 and s.endswith('十'): return CN_NUM.get(s[0], 1) * 10
    if len(s) == 3 and s[1] == '十': return CN_NUM.get(s[0], 0) * 10 + CN_NUM.get(s[2], 0)
    return CN_NUM.get(s, 0)

def number_to_chinese(n):
    if n <= 10: return list(CN_NUM.keys())[list(CN_NUM.values()).index(n)]
    if n < 20: return '十' + list(CN_NUM.keys())[list(CN_NUM.values()).index(n-10)]
    if n < 100:
        tens = n // 10
        units = n % 10
        res = list(CN_NUM.keys())[list(CN_NUM.values()).index(tens)] + '十'
        if units > 0: res += list(CN_NUM.keys())[list(CN_NUM.values()).index(units)]
        return res
    return str(n)

# --- CLEANING ---
def remove_duplicate_chars(text):
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

def extract_text_for_detection(page):
    text = page.extract_text()
    return text or ""

def extract_text_for_content(page, aggressive_crop=True):
    width = page.width
    height = page.height
    
    # 1. Crop
    if aggressive_crop:
        try:
            # Top 10%, Bottom 10% crop
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

# --- SEARCH LOGIC ---

def find_start(pdf):
    regexes = [re.compile(p) for p in START_PATTERNS]
    start_search = START_PAGE_RANGE[0]
    end_search = min(START_PAGE_RANGE[1], len(pdf.pages))
    
    for i in range(start_search, end_search):
        page = pdf.pages[i]
        
        # Method 1: Raw
        text_raw = extract_text_for_detection(page)
        lines = text_raw.split('\n')
        for line_idx, line in enumerate(lines):
            line = line.strip()
            for r_idx, regex in enumerate(regexes):
                match = regex.search(line)
                if match:
                    try:
                        chapter_num = chinese_to_number(match.group(1))
                        pattern_type = 'CHAPTER' if r_idx == 0 else 'DIGIT'
                        # Return START MATCH info: (page_index, match_obj, line_text)
                        return i, chapter_num, pattern_type, line
                    except: pass
                    
        # Method 2: Dedup
        text_dedup = remove_duplicate_chars(text_raw)
        if text_dedup == text_raw: continue
        lines_dedup = text_dedup.split('\n')
        for line_idx, line in enumerate(lines_dedup):
            line = line.strip()
            for r_idx, regex in enumerate(regexes):
                match = regex.search(line)
                if match:
                    try:
                        chapter_num = chinese_to_number(match.group(1))
                        pattern_type = 'CHAPTER' if r_idx == 0 else 'DIGIT'
                        return i, chapter_num, pattern_type, line
                    except: pass

    return None, None, None, None

def find_end_page(pdf, start_index, chapter_num, pattern_type):
    start_search = start_index 
    end_search = len(pdf.pages)
    
    next_num = chapter_num + 1
    next_cn = number_to_chinese(next_num)
    
    patterns = []
    if pattern_type == 'CHAPTER':
        patterns.append(rf"^第(?:{next_num}|{next_cn})(?:章|节)")
    else:
        patterns.append(rf"^\s*(?:{next_num}|{next_cn})[、，：:]")
        
    combined = patterns + FALLBACK_END_PATTERNS
    regexes = [re.compile(p) for p in combined]

    for i in range(start_search, end_search):
        page = pdf.pages[i]
        text = extract_text_for_detection(page)
        text_dedup = remove_duplicate_chars(text)
        
        all_lines = text.split('\n') + text_dedup.split('\n')
        for line in all_lines:
            line = line.strip()
            if len(line) < 2: continue
            for regex in regexes:
                if regex.search(line):
                    return i, line # Return end page and the matching line
                    
    return end_search, None

def extract_mda_from_pdf(pdf_path):
    print(f"Processing: {os.path.basename(pdf_path)}")
    try:
        with pdfplumber.open(pdf_path) as pdf:
            start_index, chapter_num, pattern_type, start_line_text = find_start(pdf)
            
            if start_index is None:
                print(f"  [!] Skipped (Start not found)")
                return

            print(f"  -> Found MD&A at page {start_index+1} (Chapter {chapter_num})")
            
            end_index, end_line_text = find_end_page(pdf, start_index, chapter_num, pattern_type)
            print(f"  -> Ends at page {end_index+1}")
            
            extracted_text = []
            
            # Extract and Trim
            for i in range(start_index, end_index + 1):
                if i >= len(pdf.pages): break
                page = pdf.pages[i]
                
                # Get clean content
                # For first/last page, we might need raw to identify where to split, 
                # BUT user wants clean output.
                # Strategy: 
                # 1. Get clean content.
                # 2. If it's Start Page: Find the start header in clean content and delete before.
                # 3. If it's End Page: Find the end header in clean content and delete after.
                
                content = extract_text_for_content(page, aggressive_crop=True)
                
                if i == start_index:
                    # Trim Before Start
                    # Try to locate the specific header line in the cleaned content.
                    # Since clean content is deduped/cleaned, exact match might fail.
                    # We will use the 'start_line_text' (from raw) as a fuzzy guide? 
                    # Actually, if we just find the regex again in the clean text, that's safer.
                    
                    lines = content.split('\n')
                    match_index = -1
                    regexes = [re.compile(p) for p in START_PATTERNS]
                    
                    for idx, line in enumerate(lines):
                        for regex in regexes:
                            if regex.search(line.strip()):
                                match_index = idx
                                break
                        if match_index != -1: break
                    
                    if match_index != -1:
                        # Keep from match_index onwards (User: "Delete start position above")
                        # "Includes 'Management Discussion' part" -> So keep the header
                        content = "\n".join(lines[match_index:])
                        
                if i == end_index and end_line_text:
                    # Trim After End
                    # Find the End Header
                    lines = content.split('\n')
                    match_index = -1
                    
                    # Need to reconstruct the end regexes used
                    next_num = chapter_num + 1
                    next_cn = number_to_chinese(next_num)
                    patterns = []
                    if pattern_type == 'CHAPTER':
                        patterns.append(rf"^第(?:{next_num}|{next_cn})(?:章|节)")
                    else:
                        patterns.append(rf"^\s*(?:{next_num}|{next_cn})[、，：:]")
                    combined = patterns + FALLBACK_END_PATTERNS
                    regexes = [re.compile(p) for p in combined]
                    
                    for idx, line in enumerate(lines):
                        for regex in regexes:
                            if regex.search(line.strip()):
                                match_index = idx
                                break
                        if match_index != -1: break
                    
                    if match_index != -1:
                        # Keep UP TO match_index (User: "Include end position below... delete?")
                        # No, User said: "find start and end position, and delete start position above, include end position below all text."
                        # This implies deleting the text BELOW the end position (and including the end position in the deletion? or keeping it?)
                        # "Final only keep 'Management Discussion' section" -> Removes Next Chapter Header.
                        # So discard from match_index onwards.
                        content = "\n".join(lines[:match_index])
                
                extracted_text.append(content)
                
            full_text = "\n".join(extracted_text)
            
            # Save
            if not os.path.exists(OUTPUT_DIR): os.makedirs(OUTPUT_DIR)
            filename = os.path.basename(pdf_path).replace('.pdf', '.txt')
            output_path = os.path.join(OUTPUT_DIR, filename)
            
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(full_text)
                
            print(f"  -> Saved to {output_path}")

    except Exception as e:
        print(f"  [!] Error processing: {e}")

def main():
    pdf_files = sorted(glob.glob(os.path.join(PDF_DIR, "*.pdf")))
    if not pdf_files:
        print("No PDF files found.")
        return
    print(f"Found {len(pdf_files)} PDFs.")
    for pdf_file in pdf_files:
        extract_mda_from_pdf(pdf_file)

if __name__ == "__main__":
    main()

import os
from pathlib import Path
import ocrmypdf

InputDir = "Input"
OutputDir = "Output"

def process_pdfs(InputDir, OutputDir):
    # 定义输入和输出目录
    input_dir = Path(InputDir)
    output_dir = Path(OutputDir)
    
    # 如果输出文件夹不存在，则创建
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_dir.exists():
        print(f"输入目录不存在: {input_dir}")
        return

    # 获取所有 PDF 文件
    pdf_files = list(input_dir.glob("*.pdf"))
    if not pdf_files:
        print(f"在 {input_dir} 中未找到 PDF 文件")
        return

    print(f"找到 {len(pdf_files)} 个 PDF 文件，准备处理...")

    for pdf_file in pdf_files:
        output_file = output_dir / pdf_file.name
        
        # 如果目标文件已存在，跳过（或者你可以选择覆盖）
        if output_file.exists():
            print(f"文件已跳过 (已存在): {output_file.name}")
            continue

        print(f"正在处理: {pdf_file.name} -> {output_file.name} ...")
        
        try:
            # 使用 ocrmypdf 进行 OCR 修复
            # force_ocr=True: 强制光栅化重做 OCR，解决乱码问题
            # language="chi_sim": 指定简体中文
            ocrmypdf.ocr(
                input_file=pdf_file,
                output_file=output_file,
                language="chi_sim",
                force_ocr=True,
                progress_bar=True
            )
            print(f"✅ 处理成功: {output_file.name}")
        except Exception as e:
            print(f"❌ 处理失败 {pdf_file.name}: {e}")

if __name__ == "__main__":
    process_pdfs(InputDir, OutputDir)

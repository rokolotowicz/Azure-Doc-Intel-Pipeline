import os
import shutil
from pypdf import PdfReader, PdfWriter

def split_pdf(file_path, output_dir, pages_per_split=50):
    """Checks and splits a single PDF if it exceeds the page threshold."""
    reader = PdfReader(file_path)
    total_pages = len(reader.pages)
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    
    # If the file is already small enough, copy it directly to the output folder as-is
    if total_pages <= pages_per_split:
        destination = os.path.join(output_dir, os.path.basename(file_path))
        shutil.copy(file_path, destination)
        print(f"  ✓ Copied (Under limit): {os.path.basename(file_path)} ({total_pages} pages)")
        return

    # If the file exceeds the limit, split it
    print(f"  → Splitting '{os.path.basename(file_path)}' ({total_pages} total pages):")
    part_number = 1
    for i in range(0, total_pages, pages_per_split):
        writer = PdfWriter()
        end_page = min(i + pages_per_split, total_pages)
        
        # Add pages to the current split segment
        for page_num in range(i, end_page):
            writer.add_page(reader.pages[page_num])
        
        output_filename = f"{base_name}_Part_{part_number}.pdf"
        output_path = os.path.join(output_dir, output_filename)
        
        with open(output_path, "wb") as out_file:
            writer.write(out_file)
            
        print(f"     ↳ Created: {output_filename} (Pages {i + 1} to {end_page})")
        part_number += 1


def process_directory(input_dir, pages_per_split=50):
    """Scans a directory and processes all PDF files inside it."""
    if not os.path.exists(input_dir):
        print(f"✗ Directory not found: {input_dir}")
        return

    # Define and create the output folder
    output_dir = os.path.join(input_dir, "processed_for_upload")
    os.makedirs(output_dir, exist_ok=True)

    print(f"Scanning directory: {input_dir}")
    print(f"Any PDF with more than {pages_per_split} pages will be split.")
    print(f"Processed output files will be saved in: {output_dir}\n")

    # Filter only PDF files
    pdf_files = [f for f in os.listdir(input_dir) if f.lower().endswith(".pdf")]
    
    if not pdf_files:
        print("No PDF files found in the directory.")
        return

    # Process each PDF sequentially
    for filename in pdf_files:
        full_path = os.path.join(input_dir, filename)
        try:
            split_pdf(full_path, output_dir, pages_per_split)
        except Exception as e:
            print(f" ✗ Error processing {filename}: {e}")

    print(f"\n✓ Reprocessing complete. Open this folder and upload its contents: \n{output_dir}")


if __name__ == "__main__":
    # Path to your directory containing multiple PDFs
    dir_path = r"D:\dl\projects\Azure-Doc-Intel-Pipeline\temp-reprocess"
    
    # Process the entire directory (splitting any PDF over 50 pages)
    process_directory(dir_path, pages_per_split=50)
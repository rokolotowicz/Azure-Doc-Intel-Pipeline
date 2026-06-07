import os
from pypdf import PdfReader, PdfWriter

def split_pdf(file_path, pages_per_split=50):
    """Splits a large PDF into smaller, multi-page PDF segments."""
    if not os.path.exists(file_path):
        print(f"✗ File not found: {file_path}")
        return

    reader = PdfReader(file_path)
    total_pages = len(reader.pages)
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    
    # Create an output directory for the split files
    output_dir = os.path.join(os.path.dirname(file_path), f"{base_name}_splits")
    os.makedirs(output_dir, exist_ok=True)

    print(f"Processing '{file_path}' ({total_pages} total pages)...")

    part_number = 1
    for i in range(0, total_pages, pages_per_split):
        writer = PdfWriter()
        end_page = min(i + pages_per_split, total_pages)
        
        # Add pages to the current split
        for page_num in range(i, end_page):
            writer.add_page(reader.pages[page_num])
        
        output_filename = f"{base_name}_Part_{part_number}.pdf"
        output_path = os.path.join(output_dir, output_filename)
        
        with open(output_path, "wb") as out_file:
            writer.write(out_file)
            
        print(f" ✓ Created: {output_filename} (Pages {i + 1} to {end_page})")
        part_number += 1

    print(f"\n✓ Splitting complete. Files saved in: {output_dir}")


if __name__ == "__main__":
    # Adjust this path to the location of your large NIST PDF
    pdf_path = r"D:\dl\projects\Azure-Doc-Intel-Pipeline\temp-reprocess\OWASP_LLM_2025.pdf"
    
    # Split into 50-page increments (adjust pages_per_split if desired)
    split_pdf(pdf_path, pages_per_split=50)
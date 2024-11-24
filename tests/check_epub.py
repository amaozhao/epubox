"""Check EPUB file content."""

import os
import ebooklib
from ebooklib import epub

def check_epub():
    """Check EPUB file content."""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    epub_path = os.path.join(current_dir, 'test.epub')
    
    # Read EPUB file
    book = epub.read_epub(epub_path)
    
    # Print metadata
    print("Metadata:")
    print(f"Title: {book.title}")
    print(f"Language: {book.language}")
    print(f"Authors: {book.get_metadata('DC', 'creator')}")
    
    # Print items
    print("\nItems:")
    for item in book.get_items():
        print(f"\nType: {item.get_type()}")
        print(f"ID: {item.id}")
        if hasattr(item, 'file_name'):
            print(f"File name: {item.file_name}")
        if isinstance(item, epub.EpubHtml):
            print("Content:")
            content = item.get_content().decode('utf-8')
            print(content[:200] + "..." if len(content) > 200 else content)

if __name__ == '__main__':
    check_epub()

"""
Create test EPUB file using ebooklib.
"""
import os
from ebooklib import epub

def create_test_epub():
    """Create a simple test EPUB file."""
    # Create a new EPUB book
    book = epub.EpubBook()

    # Set metadata
    book.set_identifier('test-book-1')
    book.set_title('Test Book')
    book.set_language('en')
    book.add_author('Test Author')

    # Create a chapter
    chapter = epub.EpubHtml(title='Chapter 1',
                           file_name='chapter1.xhtml',
                           content='''
        <html>
            <head>
                <title>Chapter 1</title>
            </head>
            <body>
                <h1>Chapter 1</h1>
                <p>This is a test chapter.</p>
                <p>It contains some text that can be translated.</p>
                <p>Multiple paragraphs help test the translation functionality.</p>
            </body>
        </html>
    ''')

    # Add chapter to book
    book.add_item(chapter)

    # Create table of contents
    book.toc = [epub.Link('chapter1.xhtml', 'Chapter 1', 'chapter1')]

    # Add default NCX and Nav file
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # Define spine
    book.spine = ['nav', chapter]

    # Create the EPUB file
    epub_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test.epub")
    epub.write_epub(epub_path, book)
    print(f"Created test.epub in {os.path.dirname(epub_path)}")
    return epub_path

if __name__ == "__main__":
    create_test_epub()

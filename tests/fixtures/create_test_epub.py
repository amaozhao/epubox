"""
Create test EPUB file using ebooklib.
"""
import os
from ebooklib import epub

def create_test_epub(output_path):
    """Create a simple test EPUB file.
    
    Args:
        output_path: Path where the EPUB file should be saved
    """
    # Create a new EPUB book
    book = epub.EpubBook()

    # Set metadata
    book.set_identifier('test-book-1')
    book.set_title('Test Book')
    book.set_language('en')
    book.add_author('Test Author')

    # Create a chapter
    chapter = epub.EpubHtml(
        title='Chapter 1',
        file_name='chapter1.xhtml',
        lang='en',
        content='''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
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
'''.strip()
    )

    # Add chapter
    book.add_item(chapter)

    # Add default CSS
    style = epub.EpubItem(
        uid="style_default",
        file_name="style/default.css",
        media_type="text/css",
        content='''
            body { margin: 5%; text-align: justify; }
            h1 { text-align: center; }
            nav#toc ol { padding: 0; margin-left: 1em; }
            nav#toc ol li { list-style-type: none; margin: 0; padding: 0; }
        '''
    )
    book.add_item(style)

    # Add navigation files
    nav_css = epub.EpubItem(
        uid="style_nav",
        file_name="style/nav.css",
        media_type="text/css",
        content='''
            nav#landmarks { display:none; }
            nav#page-list { display:none; }
            ol { list-style-type: none; }
        '''
    )
    book.add_item(nav_css)

    # Create NCX and Nav files
    book.add_item(epub.EpubNcx())
    nav = epub.EpubNav()
    book.add_item(nav)

    # Create Table of Contents
    book.toc = [(epub.Section('Chapter 1'), [chapter])]

    # Basic spine
    book.spine = ['nav', chapter]

    # Add required metadata
    book.add_metadata('DC', 'language', 'en')
    book.add_metadata('DC', 'identifier', 'test-book-1')
    book.add_metadata('DC', 'title', 'Test Book')
    book.add_metadata('DC', 'creator', 'Test Author')

    # Write the EPUB file
    epub.write_epub(output_path, book)
    return output_path

if __name__ == "__main__":
    create_test_epub("test.epub")

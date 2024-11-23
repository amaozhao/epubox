from typing import List, Set, Tuple, Optional
from bs4 import BeautifulSoup, Tag, NavigableString
from dataclasses import dataclass

@dataclass
class TextFragment:
    text: str
    path: str  # DOM path to the text node
    original_node: NavigableString

class HTMLProcessor:
    def __init__(self, skip_tags: Set[str] = None, max_tokens: int = 500):
        """
        Initialize HTML processor with configuration.
        
        Args:
            skip_tags: Set of HTML tags to skip during translation
            max_tokens: Maximum number of tokens per text fragment
        """
        # Default tags to skip during translation (content will not be translated)
        default_skip_tags = {
            # Script and style
            'script', 'style', 'noscript',
            
            # Programming and formatting
            'code', 'pre', 'kbd', 'samp', 'var', 'math', 'svg',
            
            # Interactive and embedded
            'canvas', 'object', 'embed', 'param',
            
            # Other technical elements
            'sup', 'sub', 'time', 'data',
            
            # Form elements (usually contain technical content)
            'input', 'button', 'select', 'option', 'optgroup', 'datalist',
            
            # Media elements (alt and title attributes will be handled separately)
            'img', 'audio', 'video', 'track', 'source'
        }
        
        # Tags that should be skipped but their children should be processed
        self.structural_tags = {
            'html', 'head', 'body', 'article', 'section', 'nav', 'aside',
            'header', 'footer', 'main', 'figure', 'figcaption'
        }
        
        # Tags that should be completely skipped (including children)
        self.skip_tags = skip_tags | default_skip_tags if skip_tags else default_skip_tags
        
        self.max_tokens = max_tokens
        
        # HTML attributes that should be translated
        self.translatable_attrs = {
            'title', 'alt', 'placeholder', 'aria-label', 'aria-description'
        }
    
    def parse_html(self, html_content: str) -> BeautifulSoup:
        """Parse HTML content into BeautifulSoup object."""
        return BeautifulSoup(html_content, 'html.parser')
    
    def _should_skip_node(self, node: Tag) -> bool:
        """
        Check if node should be skipped during translation.
        
        This method handles both Tag and NavigableString nodes, determining
        whether their content should be excluded from translation.
        """
        # Handle NavigableString nodes
        if isinstance(node, NavigableString):
            text = str(node).strip()
            if not text or text.isspace():
                return True
                
            # Skip XML declaration, DOCTYPE, and HTML structure elements
            if (text.startswith('<?xml') or
                text.startswith('<!DOCTYPE') or
                'xml version' in text or
                text == 'html' or  # DOCTYPE html
                text == 'xmlns' or  # xmlns attribute
                text.startswith('http://www.') or  # xmlns URLs
                text.startswith('javascript:') or  # JavaScript URLs
                text.startswith('data:') or       # Data URLs
                text.startswith('#')):            # Fragment identifiers
                return True
                
            # Skip if parent is a structural or non-translatable element
            if node.parent:
                if (node.parent.name in self.skip_tags or
                    node.parent.get('translate', '') == 'no' or
                    node.parent.get('contenteditable', '') == 'false' or
                    node.parent.get('hidden') is not None):
                    return True
                    
                # Skip based on class names
                classes = node.parent.get('class', [])
                if isinstance(classes, str):
                    classes = classes.split()
                skip_classes = {'notranslate', 'code', 'pre', 'technical'}
                if any(cls in skip_classes for cls in classes):
                    return True
                    
        return False

    def _get_node_path(self, node: NavigableString) -> str:
        """Generate a unique DOM path for a text node."""
        path_parts = []
        current = node.parent
        
        # Handle the case where we're at the document root
        while current:
            if isinstance(current, BeautifulSoup):
                path_parts.append("document[0]")
                break
            elif current.name:
                index = sum(1 for sibling in current.previous_siblings if sibling.name == current.name)
                path_parts.append(f"{current.name}[{index}]")
            current = current.parent
            
        return ' > '.join(reversed(path_parts))
    
    def _split_text(self, text: str) -> List[str]:
        """Split text into chunks based on max_tokens."""
        # Normalize whitespace
        text = ' '.join(text.split())
        
        # If text is shorter than max_tokens, return as is
        if len(text.split()) <= self.max_tokens:
            return [text]
            
        # Split into sentences, preserving original punctuation
        sentences = []
        current = ""
        for char in text:
            current += char
            if char in '.!?' and current.strip():
                sentences.append(current.strip())
                current = ""
        if current.strip():  # Add any remaining text
            sentences.append(current.strip())
            
        chunks = []
        current_chunk = []
        current_token_count = 0
        
        for sentence in sentences:
            # Count tokens (simple word-based approach)
            sentence_tokens = len(sentence.split())
            
            # If adding this sentence would exceed the limit, start a new chunk
            if current_token_count + sentence_tokens > self.max_tokens and current_chunk:
                chunks.append(' '.join(current_chunk))
                current_chunk = []
                current_token_count = 0
            
            # If a single sentence is longer than max_tokens, split it
            if sentence_tokens > self.max_tokens:
                words = sentence.split()
                temp_chunk = []
                temp_count = 0
                
                for word in words:
                    if temp_count + 1 > self.max_tokens:
                        if temp_chunk:
                            chunks.append(' '.join(temp_chunk))
                        temp_chunk = [word]
                        temp_count = 1
                    else:
                        temp_chunk.append(word)
                        temp_count += 1
                
                if temp_chunk:
                    current_chunk.extend(temp_chunk)
                    current_token_count = temp_count
            else:
                current_chunk.append(sentence)
                current_token_count += sentence_tokens
            
            # If current chunk is getting too big, add it to chunks
            if current_token_count >= self.max_tokens:
                chunks.append(' '.join(current_chunk))
                current_chunk = []
                current_token_count = 0
        
        # Add any remaining text
        if current_chunk:
            chunks.append(' '.join(current_chunk))
        
        return chunks

    def _extract_translatable_attributes(self, tag: Tag) -> List[TextFragment]:
        """
        Extract translatable attributes from a tag.
        Returns a list of TextFragment objects for translatable attribute values.
        """
        fragments = []
        
        if not isinstance(tag, Tag):
            return fragments
            
        # Skip if tag has translate="no"
        if tag.get('translate', '') == 'no':
            return fragments
            
        # Skip if tag is contenteditable="false"
        if tag.get('contenteditable', '') == 'false':
            return fragments
            
        # Skip if tag has hidden attribute
        if tag.get('hidden') is not None:
            return fragments
            
        # Skip if tag has notranslate class
        classes = tag.get('class', [])
        if isinstance(classes, str):
            classes = classes.split()
        if any(cls in {'notranslate', 'code', 'pre', 'technical'} for cls in classes):
            return fragments
            
        for attr in self.translatable_attrs:
            if attr in tag.attrs:
                value = tag[attr]
                if isinstance(value, str) and value.strip():
                    # Use space instead of [@] to separate path and attribute
                    path = f"{self._get_node_path(tag)} @{attr}"
                    for chunk in self._split_text(value):
                        fragments.append(TextFragment(
                            text=chunk,
                            path=path,
                            original_node=tag
                        ))
        
        return fragments

    def extract_text_fragments(self, soup: BeautifulSoup) -> List[TextFragment]:
        """
        Extract text fragments from HTML while preserving structure.
        Returns list of TextFragment objects.
        """
        fragments = []
        
        def process_node(node):
            if isinstance(node, NavigableString):
                # Process text nodes
                if not self._should_skip_node(node):
                    text = str(node).strip()
                    if text:
                        path = self._get_node_path(node)
                        for chunk in self._split_text(text):
                            fragments.append(TextFragment(
                                text=chunk,
                                path=path,
                                original_node=node
                            ))
            elif isinstance(node, Tag):
                # Extract translatable attributes
                fragments.extend(self._extract_translatable_attributes(node))
                
                # Skip processing children for tags in skip_tags
                if node.name in self.skip_tags:
                    return
                    
                # For structural tags and normal tags, process children
                for child in node.children:
                    process_node(child)
        
        process_node(soup)
        return fragments

    def rebuild_html(self, soup: BeautifulSoup, translated_fragments: List[Tuple[TextFragment, str]]) -> str:
        """
        Rebuild HTML with translated text fragments.
        
        Args:
            soup: Original BeautifulSoup object
            translated_fragments: List of (original_fragment, translated_text) tuples
        
        Returns:
            Translated HTML string
        """
        # First, update text nodes
        text_translations = {}  # path -> list of translations
        attr_translations = {}  # path -> {attr_name -> list of translations}
        
        # Group translations by path and type (text node or attribute)
        for fragment, translation in translated_fragments:
            path = fragment.path
            original_text = fragment.text
            
            # Check if this is an attribute translation
            if ' @' in path:
                base_path, attr_name = path.rsplit(' @', 1)
                attr_translations.setdefault(base_path, {}).setdefault(attr_name, []).append(translation)
            else:
                text_translations.setdefault(path, []).append(translation)
        
        # Update text nodes
        for path, translations in text_translations.items():
            # Find the tag using the path
            current = soup
            try:
                for part in path.split(' > '):
                    if not part:  # Skip empty parts
                        continue
                    if '[' in part:
                        tag_name, index_part = part.split('[', 1)
                        index = int(index_part[:-1])  # Remove the closing bracket
                        if tag_name == 'document':
                            continue  # Skip document node, as we're already at soup
                        current = list(current.find_all(tag_name, recursive=False))[index]
                    else:
                        current = current.find(part, recursive=False)
                    
                    if not current:
                        break
                
                if current and current.string:
                    current.string.replace_with(' '.join(t for t in translations if t))
            except (ValueError, IndexError) as e:
                print(f"Error processing path {path}: {str(e)}")
                continue
        
        # Update translated attributes
        for path, attrs in attr_translations.items():
            # Find the tag using the path
            current = soup
            try:
                for part in path.split(' > '):
                    if not part:  # Skip empty parts
                        continue
                    if '[' in part:
                        tag_name, index_part = part.split('[', 1)
                        index = int(index_part[:-1])  # Remove the closing bracket
                        if tag_name == 'document':
                            continue  # Skip document node, as we're already at soup
                        current = list(current.find_all(tag_name, recursive=False))[index]
                    else:
                        current = current.find(part, recursive=False)
                    
                    if not current:
                        break
                
                if current:
                    for attr_name, translations in attrs.items():
                        translated_text = ' '.join(t for t in translations if t)
                        if translated_text:
                            current[attr_name] = translated_text
            except (ValueError, IndexError) as e:
                print(f"Error processing path {path}: {str(e)}")
                continue
        
        return str(soup)

    def process_html(self, html_content: str) -> Tuple[BeautifulSoup, List[TextFragment]]:
        """
        Process HTML content and return soup object and text fragments.
        
        Args:
            html_content: Raw HTML string
            
        Returns:
            Tuple of (BeautifulSoup object, List of TextFragment objects)
        """
        soup = self.parse_html(html_content)
        fragments = self.extract_text_fragments(soup)
        return soup, fragments

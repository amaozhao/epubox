import re
from typing import List
import tiktoken
from engine.schemas import Chunk


class Chunker:
    """
    Splits HTML content into chunks based on a maximum token count,
    prioritizing splitting at the end of closing HTML tags within the token limit.
    """

    def __init__(self, limit: int = 3000, encoder: str = "gpt-3.5-turbo"):
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer token count")

        try:
            self.tokenizer = tiktoken.encoding_for_model(encoder)
        except KeyError:
            self.tokenizer = tiktoken.get_encoding("cl100k_base")

        self.limit = limit
        # This regex matches only true HTML closing tags like </a>, </div>, etc.
        self.tag_pattern = re.compile(r"</([a-zA-Z][a-zA-Z0-9]*)\s*?>")

    def get_token_count(self, content: str) -> int:
        if not content:
            return 0
        return len(self.tokenizer.encode(content))

    def chunk(self, html: str) -> List[Chunk]:
        if not isinstance(html, str):
            raise ValueError("html content must be a string")

        chunks: List[Chunk] = []
        pos = 0
        cid = 0
        n = len(html)

        while pos < n:
            cid += 1

            # Binary search to find the maximum character length l such that token count <= limit
            low = 0
            high = n - pos
            while low <= high:
                mid = (low + high) // 2
                substr = html[pos : pos + mid]
                tokens = self.get_token_count(substr)
                if tokens <= self.limit:
                    low = mid + 1
                else:
                    high = mid - 1

            token_limit_char_end = pos + high

            # Step 1: Find the last valid tag end within pos to token_limit_char_end
            last_valid_tag_end = pos
            for m in self.tag_pattern.finditer(html, pos=pos):
                tag_end = m.end()  # end() gives the position after the closing tag (e.g. "</a>" ends at `tag_end`)
                if tag_end <= token_limit_char_end:
                    last_valid_tag_end = tag_end  # Update the last valid tag end position
                else:
                    break

            # Step 2: Ensure split_at is strictly at the last valid tag end
            split_at = last_valid_tag_end if last_valid_tag_end > pos else token_limit_char_end

            # Step 3: Prevent infinite loop by forcing progress if needed
            if split_at == pos and pos < n:
                split_at = pos + 1

            # Step 4: Create the chunk if content is valid
            chunk_content = html[pos:split_at]
            if chunk_content.strip():
                chunk_name = str(cid)
                chunk = Chunk(
                    name=chunk_name,
                    original=chunk_content,
                    translated=None,
                    tokens=self.get_token_count(chunk_content),
                )
                chunks.append(chunk)

            # Move position forward to the next chunk
            pos = split_at

        return chunks

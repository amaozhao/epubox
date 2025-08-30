from typing import List

from engine.schemas import Chunk


class Merger:
    """
    Merges a list of chunks' translated content into a single string.
    """

    def merge(self, chunks: List[Chunk]) -> str:
        """
        Merges the translated content from a list of chunks.

        Args:
            chunks: A list of Chunk objects, each containing translated content.

        Returns:
            A single string with all the translated content joined together.
        """
        if not chunks:
            return ""

        # Use a list comprehension and join for better performance
        # It's more efficient than repeated string concatenation
        translated_parts = [chunk.translated for chunk in chunks if chunk.translated is not None]

        return "".join(translated_parts)

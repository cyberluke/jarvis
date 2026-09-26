"""
Spoken Chunker - Converts a stream of text into natural speech chunks.
"""

import re
from typing import Optional, Generator


class SpokenChunker:
    """
    A chunker that consumes text tokens and yields complete sentences or
    meaningful phrases to prevent "choppy" TTS playback.
    """

    def __init__(
        self,
        max_chunk_len: int = 100,
        min_chunk_len: int = 20,
        punctuation_regex: str = r"[.!?\n]",
    ):
        self.max_chunk_len = max_chunk_len
        self.min_chunk_len = min_chunk_len
        self.punctuation_regex = re.compile(punctuation_regex)
        self._buffer = ""

    def feed(self, text: str) -> Generator[str, None, nothing]:
        """
        Feed text into the buffer and yield chunks when a boundary is found.
        """
        self._buffer += text

        # 1. Try to find natural boundaries (punctuation)
        while True:
            # Find the last occurrence of a punctuation mark
            match = list(self.punctuation_regex.finditer(self._buffer))
            if not match:
                break

            last_match = match[-1]
            end_index = last_match.end()

            # If the match is reasonably far into the buffer, yield the chunk
            if end_index >= self.min_chunk_len:
                chunk = self._buffer[:end_index].strip()
                if chunk:
                    yield chunk
                self._buffer = self._buffer[end_index:].lstrip()
            else:
                # Boundary is too close to the start, wait for more text
                break

        # 2. Force yield if the buffer exceeds max_chunk_len
        if len(self._buffer) >= self.max_chunk_len:
            # Find a space to avoid splitting words
            split_pos = self._buffer.rfind(" ", 0, self.max_chunk_len)
            if split_pos != -1:
                chunk = self._buffer[:split_pos].strip()
                if chunk:
                    yield chunk
                self._buffer = self._buffer[split_pos:].lstrip()
            else:
                # Hard cut if no space found
                chunk = self._buffer[:self.max_chunk_len].strip()
                if chunk:
                    yield chunk
                self._buffer = self._buffer[self.max_chunk_len:].lstrip()

    def flush(self) -> Optional[str]:
        """Yield any remaining text in the buffer."""
        remaining = self._buffer.strip()
        self._buffer = ""
        return remaining if remaining else None

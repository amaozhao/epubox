from pydantic import BaseModel


class EpubFileResponse(BaseModel):
    """Response model for EPUB file operations."""
    id: int
    filename: str
    status: str

    class Config:
        """Pydantic model configuration."""
        from_attributes = True

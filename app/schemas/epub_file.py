from datetime import datetime

from pydantic import BaseModel


class EPUBFileBase(BaseModel):
    filename: str
    file_size: int
    original_filename: str


class EPUBFileCreate(EPUBFileBase):
    pass


class EPUBFileUpdate(EPUBFileBase):
    pass


class EPUBFileInDBBase(EPUBFileBase):
    id: int
    user_id: int
    upload_date: datetime
    file_path: str

    class Config:
        orm_mode = True


class EPUBFile(EPUBFileInDBBase):
    pass


class EPUBFileInDB(EPUBFileInDBBase):
    pass

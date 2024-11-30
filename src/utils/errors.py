class EPUBoxError(Exception):
    """基础异常类"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


class AuthError(EPUBoxError):
    """认证相关错误"""

    pass


class FileError(EPUBoxError):
    """文件处理相关错误"""

    pass


class TranslationError(EPUBoxError):
    """翻译相关错误"""

    pass


class EPUBError(EPUBoxError):
    """EPUB处理相关错误"""

    pass


class HTMLError(EPUBoxError):
    """HTML处理相关错误"""

    pass

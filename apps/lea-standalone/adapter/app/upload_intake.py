"""Transport intake limits; extraction and disk publication are worker-owned."""
MAX_UPLOAD_BYTES=25*1024*1024

class UploadError(ValueError):
    def __init__(self,message,code='invalid'):super().__init__(message);self.code=code

OVERLEAF_SOURCE_EXTS = {'.tex', '.sty', '.cls'}

class BaseAppException(Exception):
    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)

class DataProviderError(BaseAppException):
    def __init__(self, message: str = "Upstream provider error"):
        super().__init__(message, status_code=503)


class NoDataFoundError(BaseAppException):
    def __init__(self, message: str = "No data found"):
        super().__init__(message, status_code=404)


class UnsupportedEventTypeError(BaseAppException):
    def __init__(self, message: str = "Unsupported event type"):
        super().__init__(message, status_code=400)
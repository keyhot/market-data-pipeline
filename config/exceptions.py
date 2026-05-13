class BaseAppException(Exception):
    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


class DataProviderError(BaseAppException):
    def __init__(
        self, message: str = "Upstream data provider error", status_code: int = 503
    ):
        super().__init__(message, status_code)


class NoDataFoundError(BaseAppException):
    def __init__(self, message: str = "No data found", status_code: int = 404):
        super().__init__(message, status_code)


class UnsupportedEventTypeError(BaseAppException):
    def __init__(self, message: str = "Unsupported event type", status_code: int = 400):
        super().__init__(message, status_code)


class DataTooLargeError(BaseAppException):
    def __init__(self, message: str = "Response too large", status_code: int = 400):
        super().__init__(message, status_code)


class InvalidDateError(BaseAppException):
    def __init__(self, message: str = "Invalid date", status_code: int = 400):
        super().__init__(message, status_code)

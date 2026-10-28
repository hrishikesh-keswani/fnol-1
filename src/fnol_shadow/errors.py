"""Errors for the shadow summary. Messages never include the API key or a restricted value."""


class ShadowError(Exception):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


class ControlError(Exception):
    """Bedrock control-plane call failed. code is safe to print."""

    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status

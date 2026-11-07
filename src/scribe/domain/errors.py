class AppError(Exception):
    code: str = "BAD_REQUEST"
    default_message: str = "Bad request"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class BadRequestError(AppError):
    code = "BAD_REQUEST"


class UnauthorizedError(AppError):
    code = "UNAUTHORIZED"
    default_message = "Authentication required"


class TokenExpiredError(UnauthorizedError):
    code = "TOKEN_EXPIRED"
    default_message = "Token has expired"


class ForbiddenError(AppError):
    code = "FORBIDDEN"
    default_message = "Action is forbidden"


class NotFoundError(AppError):
    code = "NOT_FOUND"
    default_message = "Resource not found"


class InvalidStateError(AppError):
    code = "INVALID_STATE"
    default_message = "Operation is not allowed in the current state"


class EmailTakenError(AppError):
    code = "EMAIL_TAKEN"
    default_message = "Email is already registered"

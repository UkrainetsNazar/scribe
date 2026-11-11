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


class DomainValidationError(AppError):
    code = "VALIDATION_ERROR"
    default_message = "Validation failed"


class FileTooLargeError(AppError):
    code = "FILE_TOO_LARGE"
    default_message = "File is too large"


class UnsupportedMediaError(AppError):
    code = "UNSUPPORTED_MEDIA"
    default_message = "Unsupported media type"


class InvalidMediaError(AppError):
    code = "INVALID_MEDIA"
    default_message = "File is not a valid audio/video file"


class UploadMissingError(AppError):
    code = "UPLOAD_MISSING"
    default_message = "File has not been uploaded yet"


class ProjectNameTakenError(AppError):
    code = "PROJECT_NAME_TAKEN"
    default_message = "A project with this name already exists"


class DependencyUnavailableError(AppError):
    code = "DEPENDENCY_UNAVAILABLE"
    default_message = "A required dependency is unavailable"


class QuotaExceededError(AppError):
    code = "QUOTA_EXCEEDED"
    default_message = "Monthly processing quota exceeded"

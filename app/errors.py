class ResolverError(RuntimeError):
    code = "UNKNOWN_ERROR"

class InvalidUrlError(ResolverError):
    code = "INVALID_URL"

class ParserChangedError(ResolverError):
    code = "PARSER_CHANGED"

class RateLimitedError(ResolverError):
    code = "RATE_LIMITED"

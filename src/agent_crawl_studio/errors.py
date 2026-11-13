"""Fixed error table. Error text on stdout always comes from here, never from an engine's raw stderr,
a URL, a header, a response body or an exception.

16 contract codes (hint of the v1 contract, SPEC 7.2) and 4 command-level codes kept from SPR-01."""

CONTRACT_CODES = (
    "INVALID_REQUEST", "URL_BLOCKED", "REDIRECT_BLOCKED", "OUTPUT_PATH_BLOCKED", "DEPENDENCY_MISSING",
    "AUTH_REQUIRED", "RATE_LIMITED", "NETWORK_ERROR", "SIZE_LIMIT", "UNSUPPORTED_SOURCE", "TOOL_FAILED",
    "CANCELLED", "CONTENT_MISMATCH", "TIMEOUT", "ROBOTS_DISALLOWED", "OUTPUT_WRITE_FAILED",
)
COMMAND_CODES = ("USAGE_ERROR", "INTERNAL_ERROR", "INTERRUPTED", "WORK_ROOT_UNKNOWN")

MESSAGES = {
    "INVALID_REQUEST": "The request is not a valid v1 request; nothing was created.",
    "URL_BLOCKED": "The URL is not allowed by the strict network profile.",
    "REDIRECT_BLOCKED": "A redirect step is not allowed by the strict network profile.",
    "OUTPUT_PATH_BLOCKED": "The output folder is outside the allowed work root; nothing was created.",
    "DEPENDENCY_MISSING": "A required dependency is missing; nothing was installed.",
    "AUTH_REQUIRED": "The server requires authentication; none is sent.",
    "RATE_LIMITED": "The server asked to slow down.",
    "NETWORK_ERROR": "A network or server error stopped the download.",
    "SIZE_LIMIT": "The download is larger than the configured size limit.",
    "UNSUPPORTED_SOURCE": "This kind of source is not supported yet.",
    "TOOL_FAILED": "The tool failed on this item; details were not printed.",
    "CANCELLED": "The item was cancelled.",
    "CONTENT_MISMATCH": "The content is not what the request expected.",
    "TIMEOUT": "The operation ran out of time.",
    "ROBOTS_DISALLOWED": "robots.txt does not allow this URL.",
    "OUTPUT_WRITE_FAILED": "The file could not be written; no partial file was kept.",
    "USAGE_ERROR": "Invalid command or arguments.",
    "INTERNAL_ERROR": "An unexpected internal error occurred; details were not printed.",
    "INTERRUPTED": "The command was interrupted.",
    "WORK_ROOT_UNKNOWN": "The work root cannot be determined; set CRAWL_STUDIO_WORK to an absolute path.",
}

# Can the same request succeed if tried again later?
RETRYABLE = {
    "INVALID_REQUEST": False, "URL_BLOCKED": False, "REDIRECT_BLOCKED": False, "OUTPUT_PATH_BLOCKED": False,
    "DEPENDENCY_MISSING": False, "AUTH_REQUIRED": False, "RATE_LIMITED": True, "NETWORK_ERROR": True,
    "SIZE_LIMIT": False, "UNSUPPORTED_SOURCE": False, "TOOL_FAILED": False, "CANCELLED": True,
    "CONTENT_MISMATCH": False, "TIMEOUT": True, "ROBOTS_DISALLOWED": False, "OUTPUT_WRITE_FAILED": False,
    "USAGE_ERROR": False, "INTERNAL_ERROR": False, "INTERRUPTED": False, "WORK_ROOT_UNKNOWN": False,
}

# Status of an ITEM that ends with this code (None: the code does not belong to an item).
ITEM_STATUS_FOR = {
    "URL_BLOCKED": "blocked", "REDIRECT_BLOCKED": "blocked", "OUTPUT_PATH_BLOCKED": "blocked",
    "ROBOTS_DISALLOWED": "blocked", "CANCELLED": "cancelled",
    "AUTH_REQUIRED": "failed", "RATE_LIMITED": "failed", "NETWORK_ERROR": "failed", "SIZE_LIMIT": "failed",
    "UNSUPPORTED_SOURCE": "failed", "TOOL_FAILED": "failed", "CONTENT_MISMATCH": "failed", "TIMEOUT": "failed",
    "OUTPUT_WRITE_FAILED": "failed", "DEPENDENCY_MISSING": "failed",
}

# Exit code when the code rejects the whole request/command; None: decided by the job result (JOB_EXIT).
EXIT_FOR = {
    "INVALID_REQUEST": 2, "OUTPUT_PATH_BLOCKED": 2, "DEPENDENCY_MISSING": 2, "OUTPUT_WRITE_FAILED": 2,
    "USAGE_ERROR": 2, "WORK_ROOT_UNKNOWN": 2, "INTERNAL_ERROR": 1, "INTERRUPTED": 130, "CANCELLED": 130,
    "URL_BLOCKED": None, "REDIRECT_BLOCKED": None, "AUTH_REQUIRED": None, "RATE_LIMITED": None,
    "NETWORK_ERROR": None, "SIZE_LIMIT": None, "UNSUPPORTED_SOURCE": None, "TOOL_FAILED": None,
    "CONTENT_MISMATCH": None, "TIMEOUT": None, "ROBOTS_DISALLOWED": None,
}

JOB_EXIT = {"succeeded": 0, "failed": 1, "partial": 3, "cancelled": 130}
REJECTED_EXIT = 2  # request refused before anything ran

# Closed list of `reason` values for NETWORK_ERROR / CONTENT_MISMATCH. Never a piece of exception text.
REASONS = (
    "tls_verify_failed", "connection_refused", "connection_reset", "dns_failed", "network_unreachable",
    "http_status", "truncated_body", "content_encoding_unsupported", "forbidden", "other",
)

HINT_CODES = ("unknown_key", "wrong_type", "out_of_range")

# Fixed `hint` texts of item errors (never a piece of an engine's output).
HINTS = {
    "tools_missing": "install ffmpeg, node and the core venv; nothing was installed",
    "job_object_unavailable": "the process could not be contained; it was stopped",
}


class ContractError(ValueError):
    """The request breaks the v1 contract. `path` points at an object (never a user key or value),
    `why` is one of HINT_CODES."""

    code = "INVALID_REQUEST"

    def __init__(self, code, path, why):
        super().__init__(code)
        self.code = code
        self.path = path
        self.why = why

    @property
    def hint(self):
        return self.path + " " + self.why


def message_for(code):
    return MESSAGES.get(code, MESSAGES["INTERNAL_ERROR"])


def error_object(code, hint=None):
    """Command-level error object (SPR-01 shape): code, fixed message, optional fixed hint."""
    if code not in MESSAGES:
        code = "INTERNAL_ERROR"
    obj = {"code": code, "message": MESSAGES[code]}
    if hint:
        obj["hint"] = hint
    return obj


def item_error(code, reason=None, http_status=None, retryable=None, hint=None):
    """Error object of the result/manifest: code, fixed message, `retryable`, optional `reason`
    (closed table), `http_status`, fixed `hint`."""
    if code not in MESSAGES:
        code = "INTERNAL_ERROR"
    obj = {"code": code, "message": MESSAGES[code], "retryable": RETRYABLE[code] if retryable is None else bool(retryable)}
    if reason is not None:
        obj["reason"] = reason if reason in REASONS else "other"
    if http_status is not None:
        obj["http_status"] = int(http_status)
    if hint:
        obj["hint"] = hint
    return obj

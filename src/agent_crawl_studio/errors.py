"""Fixed error table. Error text on stdout always comes from here, never from an engine's raw stderr."""

MESSAGES = {
    "DEPENDENCY_MISSING": "A required dependency is missing; nothing was installed.",
    "INTERNAL_ERROR": "An unexpected internal error occurred; details were not printed.",
    "INTERRUPTED": "The command was interrupted.",
    "OUTPUT_PATH_BLOCKED": "The output folder is outside the allowed work root; nothing was created.",
    "USAGE_ERROR": "Invalid command or arguments.",
    "URL_BLOCKED": "The URL is not allowed by the strict network profile.",
    "WORK_ROOT_UNKNOWN": "The work root cannot be determined; set CRAWL_STUDIO_WORK to an absolute path.",
}


def message_for(code):
    return MESSAGES.get(code, MESSAGES["INTERNAL_ERROR"])


def error_object(code, hint=None):
    if code not in MESSAGES:
        code = "INTERNAL_ERROR"
    obj = {"code": code, "message": MESSAGES[code]}
    if hint:
        obj["hint"] = hint
    return obj

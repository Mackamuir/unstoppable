import re


def _glob_to_regex(pattern: str) -> re.Pattern:
    """Convert a glob pattern to regex, with ** support for recursive dirs."""
    regex = ""
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*" and i + 1 < len(pattern) and pattern[i + 1] == "*":
            regex += ".*"
            i += 2
            if i < len(pattern) and pattern[i] == "/":
                i += 1
        elif c == "*":
            regex += "[^/]*"
            i += 1
        elif c == "?":
            regex += "[^/]"
            i += 1
        elif c in r"\.+^${}()|[]":
            regex += "\\" + c
            i += 1
        else:
            regex += c
            i += 1
    return re.compile(regex + "$")


def matches_any_pattern(path: str, patterns: list[str]) -> bool:
    """Check if a path matches any of the given glob patterns."""
    for pattern in patterns:
        if _glob_to_regex(pattern).match(path):
            return True
    return False

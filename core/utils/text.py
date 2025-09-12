"""
Text processing utility functions.
"""


def trim_logs(logs: str) -> str:
    """
    Trim logs by removing everything after specific marker phrases.

    This function cuts off the string at the first occurrence of
    "Here are the backend logs" or "Here are the frontend logs".

    :param logs: Log text to trim
    :return: Trimmed log text with the marker phrase removed
    """
    try:
        if not logs:
            return ""

        # Ensure we have a string
        if not isinstance(logs, str):
            logs = str(logs)

        # Define marker phrases
        markers = ["Here are the backend logs", "Here are the frontend logs"]

        # Find the first occurrence of any marker
        index = float("inf")
        for marker in markers:
            pos = logs.find(marker)
            if pos != -1 and pos < index:
                index = pos

        # If a marker was found, trim the string
        if index != float("inf"):
            return logs[:index]

        return logs

    except Exception:
        # If anything goes wrong, return the original input as string or empty string
        return str(logs) if logs is not None else ""

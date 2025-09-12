import os
from collections import deque
from logging import FileHandler, Formatter, Logger, StreamHandler, getLogger

from core.config import LogConfig
from core.config.constants import LOGS_LINE_LIMIT


class LineCountLimitedFileHandler(FileHandler):
    """
    A file handler that limits the number of lines in the log file.
    It keeps a fixed number of the most recent log lines.
    """

    def __init__(self, filename, max_lines=LOGS_LINE_LIMIT, mode="a", encoding=None, delay=False):
        """
        Initialize the handler with the file and max lines.

        :param filename: Log file path
        :param max_lines: Maximum number of lines to keep in the file
        :param mode: File open mode
        :param encoding: File encoding
        :param delay: Delay file opening until first emit
        """
        super().__init__(filename, mode, encoding, delay)
        self.max_lines = max_lines
        self.line_buffer = deque(maxlen=max_lines)
        self._load_existing_lines()

    def _load_existing_lines(self):
        """Load existing lines from the file into the buffer if the file exists."""
        if os.path.exists(self.baseFilename):
            try:
                with open(self.baseFilename, "r", encoding=self.encoding) as f:
                    for line in f:
                        if len(self.line_buffer) < self.max_lines:
                            self.line_buffer.append(line)
                        else:
                            self.line_buffer.popleft()
                            self.line_buffer.append(line)
            except Exception:
                # If there's an error reading the file, we'll just start with an empty buffer
                self.line_buffer.clear()

    def emit(self, record):
        """
        Emit a record and maintain the line count limit.

        :param record: Log record to emit
        """
        try:
            msg = self.format(record)
            line = msg + self.terminator
            self.line_buffer.append(line)

            # Rewrite the entire file with the current buffer
            with open(self.baseFilename, "w", encoding=self.encoding) as f:
                f.writelines(self.line_buffer)

            self.flush()
        except Exception:
            self.handleError(record)


def setup(config: LogConfig, force: bool = False):
    """
    Set up logging based on the current configuration.

    The method is idempotent unless `force` is set to True,
    in which case it will reconfigure the logging.
    """

    root = getLogger()
    logger = getLogger("core")
    # Only clear/remove existing log handlers if we're forcing a new setup
    if not force and (root.handlers or logger.handlers):
        return

    while force and root.handlers:
        root.removeHandler(root.handlers[0])

    while force and logger.handlers:
        logger.removeHandler(logger.handlers[0])

    level = config.level
    formatter = Formatter(config.format)

    if config.output:
        # Use our custom handler that limits line count
        max_lines = getattr(config, "max_lines", LOGS_LINE_LIMIT)
        handler = LineCountLimitedFileHandler(config.output, max_lines=max_lines, encoding="utf-8")
    else:
        handler = StreamHandler()

    handler.setFormatter(formatter)
    handler.setLevel(level)

    logger.setLevel(level)
    logger.addHandler(handler)


def get_logger(name) -> Logger:
    """
    Get log function for a given (module) name

    :return: Logger instance
    """
    return getLogger(name)


__all__ = ["setup", "get_logger"]

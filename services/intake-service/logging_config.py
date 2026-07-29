"""Logging setup for intake-service (copy-pasted per service — see ADR 0001).

Note: this service writes the full intake request body to a repo-level file
handler (logs/intake-service.log) so the front desk has a record of every
registration. That file therefore contains PHI in plain text — flagged here,
not yet remediated.
"""
import logging
import os


def configure(service_name: str) -> logging.Logger:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, level, logging.INFO)

    logger = logging.getLogger(service_name)
    logger.setLevel(log_level)

    # Don't stack duplicate handlers if configure() is called more than once.
    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s %(levelname)s [" + service_name + "] %(message)s")

    # Console handler.
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    # File handler — repo-level logs/<service>.log. Create the directory robustly
    # so the container does not crash at startup on a fresh volume.
    #
    # LOG_DIR override (W3): the test suite points this at a temp directory.
    # Without it, running `pytest` appends real-shaped SSNs to a TRACKED file --
    # which is debt D1 demonstrating itself, and is not something CI should do to
    # the repository. Production behaviour is unchanged when LOG_DIR is unset.
    logs_dir = os.getenv("LOG_DIR") or os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "logs")
    )
    os.makedirs(logs_dir, exist_ok=True)
    file_handler = logging.FileHandler(os.path.join(logs_dir, service_name + ".log"))
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger

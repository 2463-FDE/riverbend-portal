"""PHI-safe logging for ai-orchestrator — the deliberate inverse of debt D1.

`intake-service` logs the full request body at INFO, to a repo-level file, on
every registration. That file is a PHI store nobody classified as one: every
operator, every backup, and every log-shipping vendor is in scope for it.

This service does the opposite, in two layers:

  1. **Design.** Nothing on this path ever passes a user-supplied string to the
     logger. One structured audit event per call, with a fixed key set (audit.py).
     That is the control.

  2. **Backstop.** ``RedactingFilter`` pattern-matches identifiers on every record
     this logger emits, so a future careless ``log.info("... %s", user_text)`` is
     redacted instead of leaked. That is defence in depth, not the control — a
     filter you rely on is a filter you will eventually outgrow.

Note also what is absent: there is **no file handler**. Console only. A log file
is a durable artifact with a retention policy nobody wrote, and this service has
no reason to create one.
"""
import logging
import os

from deidentify import _PATTERNS  # noqa: PLC2701 — same package, one source of truth


class RedactingFilter(logging.Filter):
    """Redact PHI-shaped substrings from any record before it is emitted.

    Applies to the formatted message *and* to each positional arg, because
    ``log.info("body=%s", payload)`` keeps the payload in ``record.args`` until
    formatting time.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = self._redact(str(record.msg))
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: self._redact(str(v)) for k, v in record.args.items()}
                else:
                    record.args = tuple(self._redact(str(a)) for a in record.args)
        except Exception:  # noqa: BLE001 — a logging filter must never raise
            pass
        return True

    @staticmethod
    def _redact(text: str) -> str:
        for kind, pat in _PATTERNS.items():
            text = pat.sub(f"[REDACTED-{kind.upper()}]", text)
        return text


def configure(service_name: str) -> logging.Logger:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logger = logging.getLogger(service_name)
    logger.setLevel(getattr(logging, level, logging.INFO))

    if logger.handlers:  # don't stack handlers on repeated configure()
        return logger

    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [" + service_name + "] %(message)s")
    )
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)

    # Do not inherit the root logger's handlers — a parent handler would emit the
    # same record without our filter attached, which is exactly the leak we are
    # defending against.
    logger.propagate = False
    return logger

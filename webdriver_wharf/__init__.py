import logging
from threading import RLock

lock = RLock()

log_format = '[%(levelname)s] %(name)s %(message)s'
formatter = logging.Formatter(log_format)


def logging_init(level):
    logging.basicConfig(
        level=level,
        format=log_format
    )

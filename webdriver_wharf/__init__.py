import logging
from threading import RLock

lock = RLock()

date_format = '%Y-%m-%d %H:%M:%S'
log_format = '%(asctime)s %(levelname)s:%(name)s %(message)s'
formatter = logging.Formatter(log_format, date_format)


def logging_init(level):
    logging.basicConfig(
        level=level,
        datefmt=date_format,
        format=log_format
    )

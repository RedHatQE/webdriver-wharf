import logging
from threading import RLock

import docker

try:
    docker.AutoVersionClient
except AttributeError:
    pass  # yay no docker_py
else:
    raise ImportError("""\
docker-py detected!

This is incompatible with the newer docker package.
Please uninstall both docker and docker-py
and then reinstall the newer docker package to run Wharf.
""")

lock = RLock()

log_format = '[%(levelname)s] %(name)s %(message)s'
formatter = logging.Formatter(log_format)


def logging_init(level):
    logging.basicConfig(
        level=level,
        format=log_format
    )

import logging
from threading import RLock

import docker

try:
    docker.AutoVersionClient
except AttributeError:
    pass  # yay no docker_py
else:
    raise ImportError("""\
docker-py installation detect

this breaks the good docker client
please uninstall both docker-py and docker
to get a working docker after reinstallation of docker
""")

lock = RLock()

log_format = '[%(levelname)s] %(name)s %(message)s'
formatter = logging.Formatter(log_format)


def logging_init(level):
    logging.basicConfig(
        level=level,
        format=log_format
    )

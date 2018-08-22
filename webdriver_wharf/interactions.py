"""
Docker/DB Interactions

The docker client and all methods that work with it live here,
as well as state tracking between docker and the DB

"""
import logging
import os
import time
from contextlib import contextmanager
import requests
from threading import Thread
import docker
from docker.errors import APIError

from webdriver_wharf import db, lock
log = logging.getLogger(__name__)

try:
    start_timeout = int(os.environ.get('WEBDRIVER_WHARF_START_TIMEOUT', 60))
except (TypeError, ValueError):
    log.warning('WEBDRIVER_WHARF_START_TIMEOUT must be an integer, defaulting to 60')
    start_timeout = 60


PORT_SSH = u'22/tcp'
PORT_HTTP = u'80/tcp'
PORT_WEBDRIVER = u'4444/tcp'
PORT_VNC = u'5999/tcp'

# docker client is localhost only for now
client = docker.from_env(timeout=120, version='auto')
last_pulled_image_id = None


@contextmanager
def apierror_squasher():
    try:
        yield
    except APIError as ex:
        log.error('Docker APIError Caught: %s', ex.explanation or ex.args[0])


def to_docker_container(db_or_docker_container):
    """
    takes any kind of container and returns a real docker container
    """
    return client.containers.get(db_or_docker_container.id)


def run_container(image_name):
    if last_pulled_image_id is None:
        pull()

    container = client.containers.run(
        image_name,
        detach=True, tty=True,
        # publish_all_ports=True,
        ports={
            PORT_VNC: None,
            PORT_HTTP: None,
            PORT_WEBDRIVER: None,
        },
        privileged=True,
        remove=True,
    )

    with db.transaction() as session, lock:
        # Use the db lock to ensure next_available_port doesn't return dupes
        container = db.Container(
            id=container.id,
            image_id=last_pulled_image_id,
            name=container.name,
        )
        session.add(container)
        session.expire_on_commit = False

    log.info('Container %s created (id: %s)', container.name, container.id)
    return container


def create_containers(image_name, number):
    containers = [run_container(image_name) for _ in range(number)]

    thread_pool = []
    for container in containers:
        try:
            docker_container = to_docker_container(container)
            docker_container.start()
            docker_container.reload()

            port_mapping = docker_container.attrs['NetworkSettings']['Ports']
            log.info('updating port mapping of %s', container.id)

            def get_port(key, pm=port_mapping):
                portlist = pm[key]
                return int(portlist[0][u'HostPort'])

            with db.transaction() as session:
                container.webdriver_port = get_port(PORT_WEBDRIVER)
                container.http_port = get_port(PORT_HTTP)
                container.vnc_port = get_port(PORT_VNC)
                session.merge(container)
        except APIError as exc:
            # No need to cleanup here since normal balancing will take care of it
            log.warning('Error starting %s', container.name)
            log.exception(exc)
            continue

        thread = Thread(target=_watch_selenium, args=(container,))
        thread_pool.append(thread)
        thread.start()
    for thread in thread_pool:
        thread.join()


def running(*containers_to_filter):
    # container filter function
    # can be used to check if a single container is running by passing
    # only one container, since it returns a set of passed-in containers
    # that are currently running. If passed all containers, an 'in'
    # check can be used to see if a container is running.
    containers_info = docker_info()
    running_containers = set()
    if not containers_to_filter:
        containers_to_filter = containers()
    for container in containers_to_filter:
        if container.id in containers_info:
            c = containers_info[container.id]
            # docker does have a state value we can check, but not
            # without inspecting the container, resulting in another api
            # call to docker. If ports are forwarded and we see "up" in
            # the container status, we should be good to go
            if c.status == 'running':
                running_containers.add(container)
    return running_containers


def check_selenium(container):
    try:
        status = requests.get('http://localhost:%d/wd/hub' % container.webdriver_port).status_code
        if 200 <= status < 400:
            return True
        else:
            log.info('selenium on %s responded with %d' % (container.name, status))
            return False
    except Exception:
        return False


def _watch_selenium(container):
    # Before returning, make sure the selenium server is accepting requests
    start_time = time.time()
    while True:
        if check_selenium(container):
            log.info('Container %s started', container.name)
            return
        else:
            log.debug('port %d not yet open, sleeping...' % container.webdriver_port)
            if time.time() - start_time > start_timeout:
                log.warning('Container %s failed to start selenium', container.name)
                return
            time.sleep(10)


def stop(container):
    if running(container):
        with apierror_squasher():
            to_docker_container(container).stop(timeout=10)
            log.info('Container %s stopped', container.name)


def destroy(container):
    stop(container)
    with apierror_squasher():
        to_docker_container(container).remove(v=True)
        log.info('Container %s destroyed', container.name)


def destroy_all():
    # This is not an API function
    destroy_us = containers()
    log.info('Destroying %s containers', len(destroy_us))
    for c in destroy_us:
        log.debug('Destroying %s', c.name)
        destroy(c)


def pull(image_name):
    global last_pulled_image_id

    # Add in some newlines so we can iterate over the concatenated json
    image_result = client.images.pull(image_name, tag='latest')
    log.debug("get image result %r", image_result)
    if isinstance(image_result, list):
        assert len(image_result) == 1
        image = image_result[0]
    else:
        image = image_result

    pulled_image_id = image.id
    if pulled_image_id != last_pulled_image_id:
        # TODO: Add a config flag on this so we aren't rudely deleting peoples' images
        #       if they aren't tracking a tag
        last_pulled_image_id = pulled_image_id
        log.info('Pulled image "%s" (docker id: %s)', image_name, pulled_image_id)
        # flag to indicate pulled image is new
        return True


def docker_info():
    return {
        c.id: c
        for c in client.containers.list(all=True)
    }


def containers():
    containers = set()
    # Get all the docker containers that the DB knows about
    for container_id in docker_info():
        container = db.Container.from_id(container_id)
        if container is None:
            log.debug("Container %s isn't in the DB; ignored", id)
            continue
        containers.add(container)

    # Clean container out of the DB that docker doesn't know about
    with db.transaction() as session:
        for db_container in session.query(db.Container).all():
            if db_container not in containers:
                log.debug('Container %s (%s) no longer exists, removing from DB',
                    db_container.name, db_container.id)
                session.delete(db_container)
    return containers

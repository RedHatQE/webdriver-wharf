"""
Docker/DB Interactions

The docker client and all methods that work with it live here,
as well as state tracking between docker and the DB

"""
import json
import logging
import os
import time
from contextlib import contextmanager
from itertools import count
from urllib import urlopen
from threading import Thread

from docker import AutoVersionClient, errors

from webdriver_wharf import db, lock

try:
    start_timeout = int(os.environ.get('WEBDRIVER_WHARF_START_TIMEOUT', 60))
except (TypeError, ValueError):
    print 'WEBDRIVER_WHARF_START_TIMEOUT must be an integer, defaulting to 60'
    start_timeout = 60

# TODO: Making these configurable would be good
# lowest port for webdriver binding
_wd_port_start = 4900
# Offsets for finding the other ports
_vnc_port_offset = 5900 - _wd_port_start
_http_port_offset = 6900 - _wd_port_start

# docker client is localhost only for now
client = AutoVersionClient(timeout=120)
logger = logging.getLogger(__name__)
container_pool_size = 4
last_pulled_image_id = None


@contextmanager
def apierror_squasher():
    try:
        yield
    except errors.APIError as ex:
        err_tpl = 'Docker APIError Caught: %s'
        if ex.explanation:
            logger.error(err_tpl, ex.explanation)
        else:
            logger.error(err_tpl, ex.args[0])


def _next_available_port():
    # Get the list of in-use webdriver ports from docker
    # Returns a the lowest port greater than or equal to wd_port_start
    # that isn't currently associated with a docker pid
    seen_wd_ports = [(c.webdriver_port, c.http_port, c.vnc_port) for c in containers()]

    for wd_port in count(_wd_port_start):
        http_port = wd_port + _http_port_offset
        vnc_port = wd_port + _vnc_port_offset
        if (wd_port, http_port, vnc_port) not in seen_wd_ports:
            return wd_port


def image_id(image):
    # normalize image names or ids to id for easy comparison
    try:
        return _dgci(client.inspect_image(image), 'id')
    except TypeError:
        # inspect_image returned None
        return None


def create_container(image_name):
    if last_pulled_image_id is None:
        pull()

    create_info = client.create_container(image_name, detach=True, tty=True)
    container_id = _dgci(create_info, 'id')
    container_info = client.inspect_container(container_id)
    name = _name(container_info)

    with db.transaction() as session, lock:
        # Use the db lock to ensure next_available_port doesn't return dupes
        webdriver_port = _next_available_port()
        http_port = webdriver_port + _http_port_offset
        vnc_port = webdriver_port + _vnc_port_offset
        container = db.Container(
            id=container_id,
            image_id=last_pulled_image_id,
            name=name,
            webdriver_port=webdriver_port,
            http_port=http_port,
            vnc_port=vnc_port
        )
        session.add(container)
        session.expire_on_commit = False

    logger.info('Container %s created (id: %s)', name, container_id[:12])
    return container


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
            if c['Ports'] and 'up' in str(c['Status']).lower():
                running_containers.add(container)
    return running_containers


def start(*containers):
    if containers:
        logger.info('Starting %d containers' % len(containers))
    thread_pool = []
    for container in containers:
        try:
            client.start(container.id, privileged=True, port_bindings=container.port_bindings)
        except errors.APIError as exc:
            # No need to cleanup here since normal balancing will take care of it
            logger.warning('Error starting %s', container.name)
            logger.exception(exc)
            continue

        thread = Thread(target=_watch_selenium, args=(container,))
        thread_pool.append(thread)
        thread.start()

    # Join all the threads before returning
    for thread in thread_pool:
        thread.join()


def check_selenium(container):
    try:
        return urlopen('http://smyers-hobbes.usersys.redhat.com:4903/wd/hub').getcode() == 200
    except Exception:
        return False


def _watch_selenium(container):
    # Before returning, make sure the selenium server is accepting requests
    start_time = time.time()
    while True:
        if check_selenium(container):
            logger.info('Container %s started', container.name)
            return
        else:
            logger.debug('port %d not yet open, sleeping...' % container.webdriver_port)
            if time.time() - start_time > start_timeout:
                logger.warning('Container %s failed to start selenium', container.name)
                return
            time.sleep(1)


def stop(container):
    if running(container):
        with apierror_squasher():
            client.stop(container.id, timeout=10)
            logger.info('Container %s stopped', container.name)


def destroy(container):
    stop(container)
    with apierror_squasher():
        client.remove_container(container.id, v=True)
        logger.info('Container %s destroyed', container.name)


def destroy_all():
    # This is not an API function
    destroy_us = containers()
    print 'Destroying %d containers' % len(destroy_us)
    for c in destroy_us:
        print 'Destroying %s' % c.name
        destroy(c)


def pull(image):
    global last_pulled_image_id

    # Add in some newlines so we can iterate over the concatenated json
    output = client.pull(image).replace('}{', '}\r\n{')
    # Check the docker output when running a command, explode if needed
    for line in output.splitlines():
        try:
            out = json.loads(line)
            if 'error' in out:
                errmsg = out.get('errorDetail', {'message': out['error']})['message']
                logger.error(errmsg)
                # TODO: Explode here if we can't pull...
                break
            elif 'id' in out and 'status' in out:
                logger.debug('{id}: {status}'.format(**out))
        except:
            pass

    pulled_image_id = image_id(image)[:12]
    if pulled_image_id != last_pulled_image_id:
        # TODO: Add a config flag on this so we aren't rudely deleting peoples' images
        #       if they aren't tracking a tag
        last_pulled_image_id = pulled_image_id
        logger.info('Pulled image "%s" (docker id: %s)', image, pulled_image_id)
        # flag to indicate pulled image is new
        return True


def docker_info():
    return {_dgci(c, 'id'): c for c in client.containers(all=True, trunc=False)}


def containers():
    containers = set()
    # Get all the docker containers that the DB knows about
    for container_id in docker_info():
        container = db.Container.from_id(container_id)
        if container is None:
            logger.debug("Container %s isn't in the DB; ignored", id)
            continue
        containers.add(container)

    # Clean container out of the DB that docker doesn't know about
    with db.transaction() as session:
        for db_container in session.query(db.Container).all():
            if db_container not in containers:
                logger.debug('Container %s (%s) no longer exists, removing from DB',
                    db_container.name, db_container.id)
                session.delete(db_container)
    return containers


def _dgci(d, key):
    # dgci = dict get case-insensitive
    keymap = {k.lower(): k for k in d.keys()}
    return d.get(keymap[key.lower()])


def _name(docker_info):
    return _dgci(docker_info, 'name').strip('/')

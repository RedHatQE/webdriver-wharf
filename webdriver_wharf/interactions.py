"""
Docker/DB Interactions

The docker client and all methods that work with it live here,
as well as state tracking between docker and the DB

"""
import logging
import json
import time
import urllib
from contextlib import contextmanager
from itertools import count

from docker import Client, errors

from webdriver_wharf import db, lock

# lowest port for webdriver binding
_wd_port_start = 4900
# Offsets for finding the other ports
_vnc_port_offset = 5900 - _wd_port_start
_ssh_port_offset = 2200 - _wd_port_start

# docker client is localhost only for now
client = Client()
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
    seen_wd_ports = [c.webdriver_port for c in containers()]

    for port in count(_wd_port_start):
        if port not in seen_wd_ports:
            # TODO: Test all ports here before returning
            return port


def image_id(image):
    # normalize image names or ids to id for easy comparison
    try:
        return _dgci(client.inspect_image(image), 'id')
    except TypeError:
        # inspect_image returned None
        return None


def create_container(image):
    create_info = client.create_container(image, detach=True, tty=True)
    container_id = _dgci(create_info, 'id')
    container_info = client.inspect_container(container_id)
    name = _name(container_info)

    with db.transaction() as session, lock:
        # Use the db lock to ensure next_available_port doesn't return dupes
        webdriver_port = _next_available_port()
        ssh_port = webdriver_port + _ssh_port_offset
        vnc_port = webdriver_port + _vnc_port_offset
        container = db.Container(
            id=container_id,
            image_id=image_id(image),
            name=name,
            webdriver_port=webdriver_port,
            ssh_port=ssh_port,
            vnc_port=vnc_port
        )
        session.add(container)
        session.expire_on_commit = False

    logger.info('Container %s created (id: %s)', name, container_id[:12])
    return container


def is_running(container):
    container_info = client.inspect_container(container.id)
    return container_info['State']['Running']


def start(container):
    # TODO: Something something error checking
    client.start(container.id, privileged=True, port_bindings=container.port_bindings)

    # Before returning, make sure the selenium server is accepting requests
    tries = 0
    while True:
        try:
            # If we ever support managing remote dockers,
            # localhost will need to be the docker host instead
            urllib.urlopen('http://localhost:%d' % container.webdriver_port)
            break
        except:
            logger.debug('port %d not yet open, sleeping...' % container.webdriver_port)
            if tries >= 40:
                logger.warning('Container %s failed to start selenium, attempting to destroy it',
                    container.name)
                with lock:
                    stop(container)
                # Should probably actually respond with something useful, but at least this
                # will blow up the test runner and not the wharf
                return None
            time.sleep(3)
            tries += 1

    logger.info('Container %s started', container.name)


def stop(container):
    if is_running(container):
        with apierror_squasher():
            client.stop(container.id, timeout=10)
            logger.info('Container %s stopped', container.name)


def destroy(container):
    stop(container)
    with apierror_squasher():
        client.remove_container(container.id, v=True)
        logger.info('Container %s destroyed', container.name)


def destroy_all():
    c = containers()
    logger.info('Destroying %d containers', len(c))
    map(destroy, c)


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


def containers():
    containers = set()
    # Get all the docker containers that the DB knows about
    for container_id in [_dgci(c, 'id') for c in client.containers(all=True, trunc=False)]:
        name = _name(client.inspect_container(container_id))
        container = db.Container.from_name(name)
        if container is None:
            logger.debug("Container %s isn't in the DB; ignored", name)
            continue

        containers.add(container)

    # Clean container out of the DB that docker doesn't know about
    with db.transaction() as session:
        for db_container in session.query(db.Container).all():
            if db_container not in containers:
                logger.debug('Container %s no longer exists, removing from DB', db_container.name)
                session.delete(db_container)
    return containers


def _dgci(d, key):
    # dgci = dict get case-insensitive
    keymap = {k.lower(): k for k in d.keys()}
    return d.get(keymap[key.lower()])


def _name(docker_info):
    return _dgci(docker_info, 'name').strip('/')

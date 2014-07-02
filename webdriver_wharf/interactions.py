"""
Docker/DB Interactions

The docker client and all methods that work with it live here,
as well as state tracking between docker and the DB

"""
import logging
import json
import time
import urllib
from datetime import datetime
from itertools import count

import docker

from webdriver_wharf import db, lock

# lowest port for webdriver binding
_wd_port_start = 4900
# Offsets for finding the other ports
_vnc_port_offset = 5900 - _wd_port_start
_ssh_port_offset = 2200 - _wd_port_start

client = docker.Client()
logger = logging.getLogger(__name__)
container_pool_size = 4


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

    logger.info('Container %s created (id: %s)', name, container_id)
    return container


def is_running(container):
    container_info = client.inspect_container(container.id)
    return container_info['State']['Running']


def is_checked_out(container):
    return container.checked_out is not None


def start(container):
    # TODO: Something something error checking
    client.start(container.id, privileged=True, port_bindings=container.port_bindings)

    # Before returning, make sure sthe selenium server is accepting requests
    tries = 0
    while True:
        try:
            # If we ever support managing remote dockers,
            # localhost will need to be the docker host instead
            urllib.urlopen('http://localhost:%d' % container.webdriver_port)
            break
        except:
            logger.debug('port %d not yet open, sleeping...' % container.webdriver_port)
            if tries >= 10:
                logger.warning('Container %s failed to start selenium', container.name)
                destroy(container)
                # Should probably actually respond with something useful, but at least this
                # will blow up the test runner and not the wharf
                return None
            time.sleep(3)
            tries += 1

    logger.info('Container %s started', container.name)


def stop(container):
    client.stop(container.id)
    logger.info('Container %s stopped', container.name)


def destroy(container):
    client.stop(container.id)
    client.remove_container(container.id, v=True)
    logger.info('Container %s destroyed', container.name)


def destroy_all():
    c = containers()
    logger.info('Destroying %d containers', len(containers))
    map(destroy, c)


def checkout(container):
    if not is_running(container):
        start(container)

    with db.transaction() as session:
        container.checked_out = datetime.utcnow()
        session.merge(container)
    logger.info('Container %s checked out', container.name)


def checkin(container):
    with db.transaction() as session:
        container.checked_out = None
        session.merge(container)
    logger.info('Container %s checked in', container.name)


def checkin_all():
    logger.info('Checking in all containers')
    for container in containers():
        if is_checked_out(container):
            checkin(container)


def cleanup(image, max_checkout_time=86400):
    # checks in containers that are checked out longer than the max lifetime
    # then destroys containers that aren't running if their image is out of date
    for container in containers():
        if is_checked_out(container) and (
                (datetime.utcnow() - container.checked_out).total_seconds() > max_checkout_time):
            logger.info('Container %s checkout out longer than %d seconds, forcing checkin',
                container.name, max_checkout_time)
            checkin(container.name)

        if not is_checked_out(container) and container.image_id != image_id(image):
            logger.info('Container %s running an old image', container.name)
            destroy(container)


def pull(image):
    logger.info('Pulling image {} -- this could take a while'.format(image))
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

    logger.info('Pulled image "%s" (docker id: %s)', image, image_id(image)[:12])


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

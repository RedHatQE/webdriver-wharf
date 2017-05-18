import logging
import os
from datetime import datetime
from pkg_resources import require
from threading import Thread
from time import sleep, time

from apscheduler.schedulers.background import BackgroundScheduler
from docker.errors import APIError
from flask import Flask, jsonify, request
from pytz import utc
from requests.exceptions import RequestException

from webdriver_wharf import db, interactions, lock

pool = set()
logger = logging.getLogger(__name__)
image_name = os.environ.get('WEBDRIVER_WHARF_IMAGE', 'cfmeqe/sel_ff_chrome')
# Number of containers to have on "hot standby" for checkout
pool_size = int(os.environ.get('WEBDRIVER_WHARF_POOL_SIZE', 4))
# Max time for an appliance to be checked out before it's forcibly checked in, in seconds.
max_checkout_time = int(os.environ.get('WEBDRIVER_WHARF_MAX_CHECKOUT_TIME', 3600))
pull_interval = int(os.environ.get('WEBDRIVER_WHARF_IMAGE_PULL_INTERVAL', 3600))
rebalance_interval = int(os.environ.get('WEBDRIVER_WHARF_REBALANCE_INTERVAL', 3600 * 6))
no_content = ('', 204)

application = Flask('webdriver-wharf')

index_document = """
<pre>Webdriver Wharf

/status - Get information on running containers

/status/[container_name] - Get information on a specific container

/checkout - Check out a running webdriver container

    Returns a JSON mapping in the form of {container: data}, where data is a mapping
    of information related to the spawned container, with the following keys (sometimes more):

        checkin_url - URL to use to check the container back in when finished
        webdriver_port - integer port number of the webdriver server on this host
        webdriver_url - webdriver URL that can be injected as a selenium command_executor
        vnc_port - integer port number of the VNC server for this webdriver container
        vnc_url - URL that might launch a VNC viewer when clicked

    Renewal information will also be returned in this mapping, see /renew for more info

/checkin/[container_name] - Check in a webdriver container with the given name
/checkin/all - Check in all containers

    Keep track of the container you checked out, and try to check it back it when you're done.

    Containers will be automatically checked in after %d seconds.

    There should be no expectation that a container will continue to exist after being checked in.

/renew/[container_name]

    Update the expiration time of the named container. The following keys will be included:

        now - current time in seconds from the epoch from the server's point of view
        expire_time - time in seconds from the epoch when this container will expire
        expire_interval - time in seconds until expire_time

    now + expire_interval = expire_time

    All three are included to simplify renew implementation on the client side.

/pull - Trigger a docker pull of the configured image

/rebalance - Trigger pool balancing, mainly useful in debugging

Behind the scenes, webdriver wharf tries to maintain a pool of containers ready to be checked out.
It occasionally pulls new images, and will destroy checked-in containers running the old image.

All views return JSON or nothing, and respond to POST and GET verbs

</pre>
""" % max_checkout_time


@application.route('/checkout')
def checkout():
    if not pool:
        logger.info('Pool exhausted on checkout, waiting for an available container')
        balance_containers.trigger()

    # Sleep until we get a container back with selenium running
    while True:
        try:
            with lock:
                container = pool.pop()
                keepalive(container)
                if not interactions.check_selenium(container):
                    continue
            break
        except KeyError:
            # pool pop blew up, still no containers in the pool
            sleep(1)

    logger.info('Container %s checked out', container.name)
    info = container_info(container)
    info.update(expiry_info(container))
    balance_containers.trigger()
    return jsonify(**{container.name: info})


@application.route('/checkin/<container_name>')
def checkin(container_name):
    if container_name == 'all':
        for container in interactions.containers():
            stop_async(container)
        logger.info('All containers checked in')
    else:
        container = db.Container.from_name(container_name)
        if container:
            stop_async(container)
            logger.info('Container %s checked in', container.name)
    balance_containers.trigger()
    return no_content


@application.route('/pull')
def pull():
    pull_latest_image.trigger()
    logger.info('Pull latest image triggered')
    return no_content


@application.route('/rebalance')
def balance():
    balance_containers.trigger()
    logger.info('Rebalance triggered')
    return no_content


@application.route('/renew/<container_name>')
def renew(container_name):
    container = db.Container.from_name(container_name)
    if container:
        keepalive(container)
        logger.info('Container %s renewed', container.name)
        out = expiry_info(container)
    else:
        out = {}
    return jsonify(**out)


@application.route('/status')
def status():
    containers = interactions.running()
    return jsonify(**{container.name: container_info(container) for container in containers})


@application.route('/status/<container_name>')
def container_status(container_name):
    container = db.Container.from_name(container_name)
    if container:
        out = {container_name: container_info(container)}
    else:
        out = {}
    return jsonify(**out)


@application.route('/')
def index():
    return index_document


def container_info(container):
    host = requesting_host()
    host_noport = host.split(':')[0]

    data = {
        'image_id': container.image_id,
        'checked_out': container.checked_out,
        'checkin_url': 'http://%s/checkin/%s' % (host, container.name),
        'renew_url': 'http://%s/renew/%s' % (host, container.name),
    }

    def porturl(key, viewkey, fmt):

        the_port = getattr(container, key)
        if the_port:
            data[key] = the_port
            data[viewkey] = fmt.format(host=host_noport, port=the_port)
        else:
            data.setdefault('missing_keys', []).extend([key, viewkey])

    porturl('webdriver_port', 'webdriver_url', 'http://{host}:{port}/wd/hub')
    porturl('vnc_port', 'vnc_display', 'vnc://{host}:{port}')
    porturl('http_port', 'fileviewer_url', 'http://{host}:{port}/')
    return data


def keepalive(container):
    with db.transaction() as session:
        container.checked_out = datetime.utcnow()
        session.merge(container)


def _stop_async_worker(container):
    interactions.stop(container)
    balance_containers.trigger()


def stop_async(container):
    with lock:
        try:
            pool.remove(container)
        except KeyError:
            pass

    Thread(target=_stop_async_worker, args=(container,)).start()


def expiry_info(container):
    # Expiry time for checkout and renew returns, plus renewl url
    # includes 'now' as seen by the wharf in addition to the expire time so
    # client can choose how to best handle renewals without doing
    host = requesting_host()
    now = int(time())
    expire_time = now + max_checkout_time
    return {
        'renew_url': 'http://%s/renew/%s' % (host, container.name),
        'now': now,
        'expire_time': expire_time,
        'expire_interval': max_checkout_time
    }


def requesting_host():
    return request.headers.get('Host')


# Scheduled tasks use docker for state, so use memory for jobs
scheduler = BackgroundScheduler({
    'apschedule.jobstores.default': {
        'type': 'sqlite',
        'engine': db.engine(),
    },
    'apscheduler.executors.default': {
        'class': 'apscheduler.executors.pool:ThreadPoolExecutor',
    },
})


@scheduler.scheduled_job('interval', id='pull_latest_image',
    seconds=pull_interval, timezone=utc)
def pull_latest_image():
    # Try to pull a new image
    if interactions.pull(image_name):
        # If we got a new image, trigger a rebalance
        balance_containers.trigger()

pull_latest_image.trigger = lambda: scheduler.modify_job(
    'pull_latest_image', next_run_time=datetime.now())


@scheduler.scheduled_job('interval', id='balance_containers',
    seconds=rebalance_interval, timezone=utc)
def balance_containers():
    try:
        # Clean up before interacting with the pool:
        # - checks in containers that are checked out longer than the max lifetime
        # - stops containers that aren't running if their image is out of date
        # - stops containers from the pool not running selenium
        for container in interactions.containers():
            if container.checked_out:
                if ((datetime.utcnow() - container.checked_out).total_seconds()
                        > max_checkout_time):
                    logger.info('Container %s checked out longer than %d seconds, forcing stop',
                        container.name, max_checkout_time)
                    interactions.stop(container)
                    continue
            else:
                if container.image_id != interactions.last_pulled_image_id:
                    logger.info('Container %s running an old image', container.name)
                    interactions.stop(container)
                    continue

        pool_balanced = False
        while not pool_balanced:
            # Grabs/releases the lock each time through the loop so checkouts don't have to wait
            # too long if a container's being destroyed
            with lock:
                # Make sure the number of running containers that aren't checked out
                containers = interactions.containers()
                running = interactions.running(*containers)
                not_running = containers - running
                checked_out = set(filter(lambda c: bool(c.checked_out), running))

                # Reset the global pool based on the current derived state
                pool.clear()
                pool.update(running - checked_out)

            pool_stat_str = '%d/%d - %d checked out - %d to destroy' % (
                len(pool), pool_size, len(checked_out), len(not_running))
            containers_to_start = pool_size - len(pool)
            containers_to_stop = len(pool) - pool_size

            # Starting containers can happen at-will, and shouldn't be done under lock
            # so that checkouts don't have to block unless the pool is exhausted
            if containers_to_start > 0:
                if containers_to_start > 4:
                    # limit the number of ocntainers to start so we
                    # don't spend *too* much time refilling the pool if
                    # there's more work to be done
                    containers_to_start = 4
                logger.info('Pool %s, adding %d containers', pool_stat_str, containers_to_start)
                new_containers = []
                for __ in range(containers_to_start):
                    new_containers.append(interactions.create_container(image_name))
                interactions.start(*new_containers)
                # after starting, continue the loop to ensure that
                # starting new containers happens before destruction
                continue

            # Stopping containers does need to happen under lock to ensure that
            # simultaneous balance_containers don't attempt to stop the same container
            # This should be rare, since containers are never returned to the pool,
            # but can happen if, for example, the configured pool size changes
            if containers_to_stop > 0:
                logger.debug('%d containers to stop', containers_to_stop)
                with lock:
                    oldest_container = sorted(pool)[0]
                    logger.info('Pool %s, removing oldest container %s',
                        pool_stat_str, oldest_container.name)
                    interactions.stop(oldest_container)
                # again, continue the loop here to save destroys for last
                continue

            # after balancing the pool, destroy oldest stopped container
            if not_running:
                if len(not_running) % 4 == 0:
                    logger.info('Pool %s' % pool_stat_str)

                interactions.destroy(sorted(not_running)[0])
                continue

            # If we've made it this far...
            logger.info('Pool balanced, %s', pool_stat_str)
            pool_balanced = True
    except (APIError, RequestException) as exc:
        logger.error('%s while balancing containers, retrying.' % type(exc).__name__)
        logger.exception(exc)
        balance_containers.trigger()
balance_containers.trigger = lambda: scheduler.modify_job(
    'balance_containers', next_run_time=datetime.now())


# starts the scheduler before handling the first request
@application.before_first_request
def _wharf_init():
    version = require("webdriver-wharf")[0].version
    logger.info('version %s', version)
    # Before doing anything else, grab the image or explode
    logger.info('Pulling image %s -- this could take a while', image_name)
    interactions.pull(image_name)
    scheduler.start()
    balance_containers.trigger()
    # Give the scheduler and executor a nice cool glass of STFU
    # This supresses informational messages about the task be fired by the scheduler,
    # as well as warnings from the executor that the task is already running.
    # For our purposes, neither is notable.
    logging.getLogger('apscheduler.scheduler').setLevel(logging.ERROR)
    logging.getLogger('apscheduler.executors.default').setLevel(logging.ERROR)
    logger.info('Initializing pool, ready for checkout')

if __name__ == '__main__':
    application.run()

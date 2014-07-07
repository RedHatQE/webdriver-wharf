import logging
import os
from datetime import datetime
from time import sleep

import waitress
from apscheduler.schedulers.background import BackgroundScheduler
from bottle import Bottle, ServerAdapter, request
from pytz import utc

from webdriver_wharf import db, interactions, lock

pool = set()
logger = logging.getLogger(__name__)
image_name = os.environ.get('WEBDRIVER_WHARF_IMAGE', 'cfmeqe/sel_ff_chrome')
# Number of containers to have on "hot standby" for checkout
pool_size = os.environ.get('WEBDRIVER_WHARF_POOL_SIZE', 4)
# Max time for an appliance to be checked out before it's forcibly checked in, in seconds.
max_checkout_time = os.environ.get('WEBDRIVER_WHARF_MAX_CHECKOUT_TIME', 86400)

application = Bottle(catchall=False)

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

/checkin/[docker_id] - Check in a webdriver container with the given ID
/checkin/all - Check in all containers

    Keep track of the container you checked out, and try to check it back it when you're done.

    Containers will be automatically checked in after %d seconds.

    There should be no expectation that a container will continue to exist after being checked in.

/pull - Trigger a docker pull of the configured image

Behind the scenes, webdriver wharf tries to maintain a pool of containers ready to be checked out.
It occasionally pulls new images, and will destroy checked-in containers running the old image.

All views return JSON or nothing, and respond to POST and GET verbs

</pre>
""" % max_checkout_time


@application.route('/checkout')
def checkout():
    if not pool:
        logger.info('Pool exhausted on checkout, starting new instances')
        balance_containers.trigger()

    # Sleep until the pool is populated
    while not pool:
        sleep(.1)

    # Checkout a webdriver URL
    with lock:
        container = pool.pop()
    interactions.checkout(container)
    balance_containers.trigger()
    return {container.name: container_info(container)}


@application.route('/checkin/<container_name>')
def checkin(container_name):
    if container_name == 'all':
        interactions.checkin_all()
    else:
        container = db.Container.from_name(container_name)
        if container:
            interactions.checkin(container)

    balance_containers.trigger()


@application.route('/pull')
def pull():
    pull_latest_image.trigger()


@application.route('/status')
@application.route('/status/<container_name>')
def status(container_name=None):
    if container_name is None:
        containers = interactions.containers()
        return {container.name: container_info(container) for container in containers}
    else:
        container = db.Container.from_name(container_name)
        if container:
            return {container_name: container_info(container)}
        else:
            return {}


@application.route('/')
def index():
    return index_document


def container_info(container):
    host = request.headers.get('Host')
    hostname, __ = host.split(':')
    return {
        'is_running': interactions.is_running(container),
        'checked_out': interactions.is_checked_out(container),
        'checkin_url': 'http://%s/checkin/%s' % (host, container.name),
        'webdriver_port': container.webdriver_port,
        'webdriver_url': 'http://%s:%d/wd/hub' % (hostname, container.webdriver_port),
        'vnc_port': container.vnc_port,
        'vnc_display': 'vnc://%s:%d' % (hostname, container.vnc_port - 5900)
    }


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


@scheduler.scheduled_job('interval', id='pull_latest_image', hours=1, timezone=utc)
def pull_latest_image():
    # Try to pull a new image
    if interactions.pull(image_name):
        # If we got a new image, trigger a rebalance
        balance_containers.trigger()
pull_latest_image.trigger = lambda: scheduler.modify_job(
    'pull_latest_image', next_run_time=datetime.now())


@scheduler.scheduled_job('interval', id='balance_containers', hours=6, timezone=utc)
def balance_containers():
    # Clean up before interacting with the pool
    interactions.cleanup(image_name, max_checkout_time)

    pool_balanced = False
    while not pool_balanced:
        # Grabs/releases the lock each time through the loop so checkouts don't have to wait
        # too long if a container's being destroyed
        with lock:
            # Make sure the number of running containers that aren't checked out
            containers = interactions.containers()
            running = set(filter(interactions.is_running, containers))
            not_running = containers - running
            checked_out = set(filter(interactions.is_checked_out, running))
            checked_in = running - checked_out

            # Reset the global pool based on the current derived state
            pool.clear()
            pool.update(checked_in)

            pool_stat_str = '%d/%d' % (len(pool), pool_size)
            containers_to_start = pool_size - len(checked_in)
            containers_to_stop = len(checked_in) - pool_size

            # Stopping containers needs to be done under the lock to
            # prevent stopping a container currently being checked out
            if containers_to_stop > 0:
                logger.debug('%d containers to stop', containers_to_stop)
                oldest_container = sorted(pool)[0]
                latest_image_id = interactions.image_id(image_name)
                logger.info('Pool %s, removing oldest container %s',
                    pool_stat_str, oldest_container.name)
                interactions.stop(oldest_container)
                if oldest_container.image_id != latest_image_id:
                    # destroy containers with old image ids
                    interactions.destroy(oldest_container)

        # Starting containers can happen at-will, and shouldn't be done under lock
        # so that checkouts don't have to block unless the pool is exhausted
        if containers_to_start > 0:
            logger.debug('%d containers to start', containers_to_start)
            if not_running:
                # Start the newest stopped container
                container_to_start = sorted(not_running)[-1]
            else:
                container_to_start = interactions.create_container(image_name)
            logger.info('Pool %s, adding container %s',
                pool_stat_str, container_to_start.name)
            interactions.start(container_to_start)

        if not (containers_to_start or containers_to_stop):
            logger.info('Pool balanced, %s', pool_stat_str)
            pool_balanced = True
balance_containers.trigger = lambda: scheduler.modify_job(
    'balance_containers', next_run_time=datetime.now())


# starts the scheduler before running the webserver
class WharfServer(ServerAdapter):
    def run(self, handler):
        from pkg_resources import require
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
        waitress.serve(handler, host=self.host, port=self.port, threads=pool_size * 2)

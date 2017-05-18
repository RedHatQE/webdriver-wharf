import logging
import os
import signal

from webdriver_wharf import app, logging_init

logger = logging.getLogger(__name__)

loglevel = getattr(logging, os.environ.get('WEBDRIVER_WHARF_LOG_LEVEL', 'info').upper(), 'INFO')
listen_host = os.environ.get('WEBDRIVER_WHARF_LISTEN_HOST', '0.0.0.0')
listen_port = int(os.environ.get('WEBDRIVER_WHARF_LISTEN_PORT', 4899))


def handle_hup(signum, stackframe):
    app.pull_latest_image.trigger()

signal.signal(signal.SIGHUP, handle_hup)


def main():
    # TODO: Centralized config would be nice, bring in argparse or something that already
    # handles envvars. Also expose interactions.destroy_all somehow, so wharf can clean
    # up after itself when asked
    logging_init(loglevel)
    app.application.try_trigger_before_first_request_functions()
    app.application.run(
        host=listen_host,
        port=listen_port,
        threaded=True,
    )

webdriver-wharf
===============

A docker-based warehouse of Selenium servers running chrome and firefox,
ready to be checked out for use by Selenium WebDriver clients.

Configuration is done entirely with environment variables (detailed below),
which should make it trivial to use something other than systemd to manage
the wharf service, should systemd be unavailable.

systemd example config
======================

/etc/systemd/system/webdriver-wharf.service
-------------------------------------------

```
[Unit]
Description=WebDriver Wharf
After=docker.service

[Service]
Type=simple
ExecStart=/usr/bin/webdriver-wharf
EnvironmentFile=/etc/default/webdriver-wharf

[Install]
WantedBy=multi-user.target
```

Note that on RPM-bases systems, `EnvironmentFile` should probably be
`/etc/sysconfig/webdriver-wharf`


Docker example config
=====================

```
docker run -v /var/run/docker.sock:/var/run/docker.sock -e WEBDRIVER_WHARF_IMAGE=quay.io/redhatqe/selenium-standalone -e WEBDRIVER_WHARF_POOL_SIZE=16 --publish 4899:4899 --net=host --detach --privileged --name wharf-master -v wharf-data:/var/run/wharf/ wharf:latest
```


Environment Variables
=====================

WEBDRIVER_WHARF_IMAGE
---------------------

The name of the docker image to spawn in the wharf pool.

Defaults to `cfmeqe/sel_ff_chrome`, but can be any docker image that exposes a selenium
server on port 4444 and a VNC server on port 5999 (display :99). The `sel_ff_chrome`
image also exposes nginx's json-based file browser on port 80.

WEBDRIVER_WHARF_POOL_SIZE
-------------------------

Number of containers to keep in the active pool, ready for checkout.

Defaults to 4

WEBDRIVER_WHARF_MAX_CHECKOUT_TIME
---------------------------------

Maximum time, in seconds, a container can be checked out before it is reaped.

Defaults to 3600, set to 0 for no max checkout time (probably a bad idea)

WEBDRIVER_WHARF_IMAGE_PULL_INTERVAL
-----------------------------------

Interval, in seconds, of how often wharf will check for updates to the docker image.

Defaults to one hour (3600 seconds)

WEBDRIVER_WHARF_REBALANCE_INTERVAL
----------------------------------

Interval, in seconds, of how often wharf will rebalance the active container pool.

Frequent rebalancing should not be necessary, and indicates a wharf bug.

Defaults to six hours (21600 seconds)

WEBDRIVER_WHARF_LOG_LEVEL
-------------------------

Loglevel for wharf's console spam. Must be one of python's builtin loglevels:
'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'

Defaults to 'INFO', which offers a running commentary on the starting, stopping,
and destruction of containers

WEBDRIVER_WHARF_LISTEN_HOST
---------------------------

Host address to bind to.

Defaults to 0.0.0.0 (all interfaces)

WEBDRIVER_WHARF_LISTEN_PORT
---------------------------

Host port (TCP) to bind to.

Defaults to 4899

WEBDRIVER_WHARF_START_TIMEOUT
-----------------------------

How long, in seconds, wharf will wait when starting a container before deciding
the container has failed to start.

Defaults to 60

WEBDRIVER_WHARF_DB_URL
----------------------

Database URL that wharf should connect to for container tracking.

By default, wharf creates and maintains its own SQLite database in sane locations,
though not necessarily the "correct" one according to the Filesystem Hierarchy Standard

If set, this value is passed directly to sqlalchemy with no further processing.
See [the sqlalchemy docs](http://docs.sqlalchemy.org/en/rel_1_0/core/engines.html#database-urls)
for information regarding the construction of URLs. When using other database engines,
wharf takes no responsibility for installing the correct driver, needs the ability to
create tables, and most importantly *has not been tested with that engine*.

# make image to make the image and make run to run it

FROM fedora:25
ENV \
	WEBDRIVER_WHARF_LOG_LEVEL=info \
	WEBDRIVER_WHARF_LISTEN_HOST=0.0.0.0 \
	WEBDRIVER_WHARF_LISTEN_PORT=4899 \
	WEBDRIVER_WHARF_IMAGE=cfmeqe/sel_ff_chrome \
	WEBDRIVER_WHARF_DB_URL=sqlite:////var/run/wharf/containers.sqlite

RUN dnf install python-pip sqlite -y && dnf clean all

ADD . /wharf-source
WORKDIR wharf-source
RUN cp webdriver_wharf.egg-info/PKG-INFO . && \
	pip install -e . && \
	rm -rf ~/.pip/cache ~/.cache/pip 
RUN mkdir -p /var/run/wharf/ && sqlite3 /var/run/wharf/containers.sqlite
VOLUME ["/var/run/wharf/" ,"/var/run/docker.sock"]
EXPOSE 4899
ENTRYPOINT ["webdriver-wharf"]
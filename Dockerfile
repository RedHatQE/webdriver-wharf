# make image to make the image and make run to run it

FROM fedora:28
ENV \
	WEBDRIVER_WHARF_LOG_LEVEL=info \
	WEBDRIVER_WHARF_LISTEN_HOST=0.0.0.0 \
	WEBDRIVER_WHARF_LISTEN_PORT=4899 \
	WEBDRIVER_WHARF_IMAGE=cfmeqe/sel_ff_chrome \
	WEBDRIVER_WHARF_DB_URL=sqlite:////var/run/wharf/containers.sqlite

RUN dnf install python-pip python-pbr git sqlite -y && dnf clean all
ADD requirements.txt requirements.txt
RUN pip install -r requirements.txt && \
	rm -rf ~/.pip/cache ~/.cache/pip
ADD . /wharf-source
WORKDIR wharf-source
RUN git pull https://github.com/RedHatQE/webdriver-wharf/ --tags
RUN pip install -e . && \
	rm -rf ~/.pip/cache ~/.cache/pip
RUN mkdir -p /var/run/wharf/ && sqlite3 /var/run/wharf/containers.sqlite
VOLUME ["/var/run/wharf/" ,"/var/run/docker.sock"]
EXPOSE 4899
ENTRYPOINT ["webdriver-wharf"]

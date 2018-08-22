# make image to make the image and make run to run it

FROM fedora:28
ENV \
	WEBDRIVER_WHARF_LOG_LEVEL=info \
	WEBDRIVER_WHARF_LISTEN_HOST=0.0.0.0 \
	WEBDRIVER_WHARF_LISTEN_PORT=4899 \
	WEBDRIVER_WHARF_IMAGE=cfmeqe/cfme_sel_stable \
	WEBDRIVER_WHARF_DB_URL=sqlite:////var/run/wharf/containers.sqlite

RUN dnf install python3-pip python3-pbr git sqlite -y && dnf clean all
ADD requirements.txt requirements.txt
RUN pip3 install -r requirements.txt && \
	rm -rf ~/.pip/cache ~/.cache/pip
ADD . /wharf-source
WORKDIR wharf-source
RUN git pull https://github.com/RedHatQE/webdriver-wharf/ --tags
RUN pip3 install -e . && \
	rm -rf ~/.pip/cache ~/.cache/pip
RUN mkdir -p /var/run/wharf/
VOLUME ["/var/run/wharf/" ,"/var/run/docker.sock"]
EXPOSE 4899
ENTRYPOINT ["webdriver-wharf"]

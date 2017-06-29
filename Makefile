.phony: image run clean upload sdist

sdist:
	./setup.py sdist

upload:
	./setup.py sdist bdist_wheel upload

clean:
	rm -rf AUTHORS build ChangeLog dist __pycache__ *.egg *.egg-info .coverage

image:
	docker build . -t webdriver-wharf

run: image
	docker run -v /var/run/docker.sock:/var/run/docker.sock --privileged  webdriver-wharf
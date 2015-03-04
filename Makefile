sdist:
	./setup.py sdist

upload:
	./setup.py sdist bdist_wheel upload

clean:
	rm -rf AUTHORS build ChangeLog dist __pycache__ *.egg *.egg-info .coverage

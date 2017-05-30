import errno
import logging
import os
from contextlib import contextmanager
from datetime import datetime
from threading import local

from appdirs import AppDirs
from sqlalchemy import create_engine, Column, DateTime, Integer, String
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base

logger = logging.getLogger(__name__)
thread_local = local()
Base = declarative_base()


class Container(Base):
    __tablename__ = 'container'
    # Docker ID
    id = Column(String(64), primary_key=True)
    # Images ID, so we can tell when containers are stale
    image_id = Column(String(64), primary_key=True)
    # Docker name, friendly name given by docker to running instances
    name = Column(String(64))
    # Dual role: if null, not checked out. If set, seconds since the epoch when checked out
    checked_out = Column(DateTime)
    # Created timestamp to make it easy to destroy oldest containers first
    created = Column(DateTime, default=datetime.utcnow)
    # Exposed ports
    http_port = Column(Integer)
    webdriver_port = Column('webdriver_port', Integer)
    vnc_port = Column(Integer)

    def __iter__(self):
        # Expose columns and their values as pairs to facilitate casting the container as a dict
        for column in self.__table__.columns:
            yield (column.name, getattr(self, column.name))

    def __repr__(self):
        # e.g. "<webdriver_wharf.db.Container 'crazy_ivan'>"
        return "<%s.%s '%s'>" % (__name__, type(self).__name__, self.name)

    def __eq__(self, other):
        # If the IDs match, they're the same container
        try:
            return self.id == other.id
        except AttributeError:
            return False

    def __hash__(self):
        # Id is hex, use it to make Containers hashable
        return int(self.id, 16)

    def __cmp__(self, other):
        # containers are sortable, oldest first
        if isinstance(other, Container):
            # containers are sortable by their creation time
            return cmp(self.created, other.created)
        else:
            # if other isn't a container, assume it is a datetime
            return cmp(self.created, other)

    @classmethod
    def from_id(cls, docker_id):
        return get_session().query(cls).filter_by(id=docker_id).first()

    @classmethod
    def from_name(cls, name):
        return get_session().query(cls).filter_by(name=name).first()


def ensure_dir(data_dir):
    try:
        os.makedirs(data_dir)
    except OSError as ex:
        if ex.errno == errno.EACCES:
            raise

        if ex.errno == errno.EEXIST and not os.path.isdir(data_dir):
            print "dir exists but isn't a dir! Explode!"
            raise

        if not os.access(data_dir, os.W_OK | os.X_OK):
            print "Can't access dir! Explode!"
            raise

    return data_dir


# Make sure we've got a place to put a DB
try:
    url = os.environ['WEBDRIVER_WHARF_DB_URL']
except KeyError:
    _dirs = AppDirs('webdriver-wharf')
    try:
        # Use the site data dir
        data_dir = ensure_dir(_dirs.site_data_dir)
    except OSError as ex:
        if (ex.errno != errno.EACCES):
            raise

        # If we got permission denied, we aren't root. Use the user dir
        data_dir = ensure_dir(_dirs.user_data_dir)

    url = 'sqlite:///' + os.path.join(data_dir, 'db.sqlite')


def engine():
    if not hasattr(thread_local, 'engine'):
        thread_local.engine = create_engine(url, connect_args={'check_same_thread': False})
        Base.metadata.create_all(thread_local.engine)
    return thread_local.engine


def get_session():
    if not hasattr(thread_local, 'sessionmaker'):
        thread_local.sessionmaker = sessionmaker(bind=engine())
    return thread_local.sessionmaker()


@contextmanager
def transaction():
    session = get_session()
    try:
        yield session
        session.commit()
    except:
        session.rollback()
        raise
    finally:
        session.close()

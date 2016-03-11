from pyramid.config import Configurator
from sqlalchemy import engine_from_config
from pyramid.session import SignedCookieSessionFactory

from .models import (
    DBSession,
    Base,
    )


def main(global_config, **settings):
    """ This function returns a Pyramid WSGI application.
    """
    engine = engine_from_config(settings, 'sqlalchemy.')
    DBSession.configure(bind=engine)
    Base.metadata.bind = engine
    config = Configurator(settings=settings)
    config.set_session_factory(SignedCookieSessionFactory('a scret phrase that noone will ever guess. oh.'))
    config.include('pyramid_chameleon')
    config.add_static_view('static', 'static', cache_max_age=3600)
    config.add_route('home', '/')
    config.add_route('graph', '/graph')
    config.add_route('control', '/control')
    config.add_route('query', '/query')
    config.add_route('queryactions', '/query')
    config.scan()
    return config.make_wsgi_app()

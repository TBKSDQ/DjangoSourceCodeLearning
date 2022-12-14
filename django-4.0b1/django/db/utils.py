import pkgutil
from importlib import import_module

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
# For backwards compatibility with Django < 3.2
from django.utils.connection import ConnectionDoesNotExist  # NOQA: F401
from django.utils.connection import BaseConnectionHandler
from django.utils.functional import cached_property
from django.utils.module_loading import import_string

DEFAULT_DB_ALIAS = 'default'
DJANGO_VERSION_PICKLE_KEY = '_django_version'


class Error(Exception):
    pass


class InterfaceError(Error):
    pass


class DatabaseError(Error):
    pass


class DataError(DatabaseError):
    pass


class OperationalError(DatabaseError):
    pass


class IntegrityError(DatabaseError):
    pass


class InternalError(DatabaseError):
    pass


class ProgrammingError(DatabaseError):
    pass


class NotSupportedError(DatabaseError):
    pass


class DatabaseErrorWrapper:
    """
    Context manager and decorator that reraises backend-specific database
    exceptions using Django's common wrappers.
    主要的作用是对with内部的语句进行异常处理
    """

    def __init__(self, wrapper):
        """
        :params wrapper: DatabaseWrapper对象(与mysql相关的DatabaseWrapper位于django.db.backends.mysq.base中)
        wrapper is a database wrapper.

        It must have a Database attribute defining PEP-249 exceptions.
        """
        self.wrapper = wrapper

    def __enter__(self):
        # 使用with时，不会返回任何内容
        pass

    def __exit__(self, exc_type, exc_value, traceback):
        """
            __exit__方法会在with内部语句执行完成后或者执行过程中遇到异常时执行。
        """
        # 判断是否出现了异常，没有则直接返回
        if exc_type is None:
            return
        # 这些异常都在上面定义了
        for dj_exc_type in (
                DataError,
                OperationalError,
                IntegrityError,
                InternalError,
                ProgrammingError,
                NotSupportedError,
                DatabaseError,
                InterfaceError,
                Error,
        ):
            # 查找Database(该Database为mysqlclient模块)中有没有与上面遍历的异常类相同名字的属性
            db_exc_type = getattr(self.wrapper.Database, dj_exc_type.__name__)
            # 判断抛出的异常是否是当前遍历的异常类子类
            if issubclass(exc_type, db_exc_type):
                dj_exc_value = dj_exc_type(*exc_value.args)
                # Only set the 'errors_occurred' flag for errors that may make
                # the connection unusable.
                if dj_exc_type not in (DataError, IntegrityError):
                    self.wrapper.errors_occurred = True
                raise dj_exc_value.with_traceback(traceback) from exc_value

    def __call__(self, func):
        # Note that we are intentionally not using @wraps here for performance
        # reasons. Refs #21109.
        def inner(*args, **kwargs):
            with self:
                return func(*args, **kwargs)
        return inner


def load_backend(backend_name):
    """
    根据给定的backend_name来导入对应数据库目录下的base模块
    Return a database backend's "base" module given a fully qualified database
    backend name, or raise an error if it doesn't exist.
    """
    # This backend was renamed in Django 1.9.
    if backend_name == 'django.db.backends.postgresql_psycopg2':
        backend_name = 'django.db.backends.postgresql'

    try:
        # 以Mysql举例，导入的是django.db.backends.mysql.base
        return import_module('%s.base' % backend_name)
    except ImportError as e_user:
        # The database backend wasn't found. Display a helpful error message
        # listing all built-in database backends.
        # 输出错误信息，列举所能够使用的数据库
        import django.db.backends
        builtin_backends = [
            name for _, name, ispkg in pkgutil.iter_modules(django.db.backends.__path__)
            if ispkg and name not in {'base', 'dummy'}
        ]
        if backend_name not in ['django.db.backends.%s' % b for b in builtin_backends]:
            backend_reprs = map(repr, sorted(builtin_backends))
            raise ImproperlyConfigured(
                "%r isn't an available database backend or couldn't be "
                "imported. Check the above exception. To use one of the "
                "built-in backends, use 'django.db.backends.XXX', where XXX "
                "is one of:\n"
                "    %s" % (backend_name, ", ".join(backend_reprs))
            ) from e_user
        else:
            # If there's some other error, this must be an error in Django
            raise


class ConnectionHandler(BaseConnectionHandler):
    settings_name = 'DATABASES'
    # Connections needs to still be an actual thread local, as it's truly
    # thread-critical. Database backends should use @async_unsafe to protect
    # their code from async contexts, but this will give those contexts
    # separate connections in case it's needed as well. There's no cleanup
    # after async contexts, though, so we don't allow that if we can help it.
    thread_critical = True

    def configure_settings(self, databases):
        databases = super().configure_settings(databases)
        if databases == {}:
            databases[DEFAULT_DB_ALIAS] = {'ENGINE': 'django.db.backends.dummy'}
        elif DEFAULT_DB_ALIAS not in databases:
            raise ImproperlyConfigured(
                f"You must define a '{DEFAULT_DB_ALIAS}' database."
            )
        elif databases[DEFAULT_DB_ALIAS] == {}:
            databases[DEFAULT_DB_ALIAS]['ENGINE'] = 'django.db.backends.dummy'
        return databases

    @property
    def databases(self):
        # self.settings是BaseConnectionHandler中的一个方法，返回包含settings.py中数据库配置信息的字典
        return self.settings

    def ensure_defaults(self, alias) -> None:
        """
        将一些默认的信息记录到alias对应的数据库配置信息字典当中(如果本身已经存在有对应的信息，那么不进行修改)
        Put the defaults into the settings dictionary for a given connection
        where no settings is provided.
        """
        try:
            # 获取alias对应名称的数据库配置信息字典
            conn = self.databases[alias]
        except KeyError:
            raise self.exception_class(f"The connection '{alias}' doesn't exist.")

        # 尝试将设置这些信息到字典当中(已存在则不会修改)
        conn.setdefault('ATOMIC_REQUESTS', False)
        conn.setdefault('AUTOCOMMIT', True)
        conn.setdefault('ENGINE', 'django.db.backends.dummy')
        if conn['ENGINE'] == 'django.db.backends.' or not conn['ENGINE']:
            conn['ENGINE'] = 'django.db.backends.dummy'
        conn.setdefault('CONN_MAX_AGE', 0)
        conn.setdefault('OPTIONS', {})
        conn.setdefault('TIME_ZONE', None)
        for setting in ['NAME', 'USER', 'PASSWORD', 'HOST', 'PORT']:
            conn.setdefault(setting, '')

    def prepare_test_settings(self, alias) -> None:
        """
        作用类似于ensure_defaults方法，将一些信息添加到配置信息字典中
        Make sure the test settings are available in the 'TEST' sub-dictionary.
        """
        try:
            conn = self.databases[alias]
        except KeyError:
            raise self.exception_class(f"The connection '{alias}' doesn't exist.")

        test_settings = conn.setdefault('TEST', {})
        default_test_settings = [
            ('CHARSET', None),
            ('COLLATION', None),
            ('MIGRATE', True),
            ('MIRROR', None),
            ('NAME', None),
        ]
        for key, value in default_test_settings:
            test_settings.setdefault(key, value)

    def create_connection(self, alias):
        """
        :params alias: settings.py中数据库配置信息别名
        """
        # 为配置信息字典添加一些默认信息
        self.ensure_defaults(alias)
        # 配置一些额外信息
        self.prepare_test_settings(alias)
        # 获取配置信息字典
        db = self.databases[alias]
        # 导入对应的数据库base模块，例如mysql就是导入django.db.backends.mysql.base
        backend = load_backend(db['ENGINE'])
        # 返回对应backend的base模块里面的DatabaseWrapper
        return backend.DatabaseWrapper(db, alias)

    def close_all(self):
        for alias in self:
            try:
                connection = getattr(self._connections, alias)
            except AttributeError:
                continue
            connection.close()


class ConnectionRouter:
    def __init__(self, routers=None):
        """
        If routers is not specified, default to settings.DATABASE_ROUTERS.
        """
        self._routers = routers

    @cached_property
    def routers(self):
        if self._routers is None:
            self._routers = settings.DATABASE_ROUTERS
        routers = []
        for r in self._routers:
            if isinstance(r, str):
                router = import_string(r)()
            else:
                router = r
            routers.append(router)
        return routers

    def _router_func(action):
        def _route_db(self, model, **hints):
            chosen_db = None
            for router in self.routers:
                try:
                    method = getattr(router, action)
                except AttributeError:
                    # If the router doesn't have a method, skip to the next one.
                    pass
                else:
                    chosen_db = method(model, **hints)
                    if chosen_db:
                        return chosen_db
            instance = hints.get('instance')
            if instance is not None and instance._state.db:
                return instance._state.db
            return DEFAULT_DB_ALIAS
        return _route_db

    db_for_read = _router_func('db_for_read')
    db_for_write = _router_func('db_for_write')

    def allow_relation(self, obj1, obj2, **hints):
        for router in self.routers:
            try:
                method = router.allow_relation
            except AttributeError:
                # If the router doesn't have a method, skip to the next one.
                pass
            else:
                allow = method(obj1, obj2, **hints)
                if allow is not None:
                    return allow
        return obj1._state.db == obj2._state.db

    def allow_migrate(self, db, app_label, **hints):
        for router in self.routers:
            try:
                method = router.allow_migrate
            except AttributeError:
                # If the router doesn't have a method, skip to the next one.
                continue

            allow = method(db, app_label, **hints)

            if allow is not None:
                return allow
        return True

    def allow_migrate_model(self, db, model):
        return self.allow_migrate(
            db,
            model._meta.app_label,
            model_name=model._meta.model_name,
            model=model,
        )

    def get_migratable_models(self, app_config, db, include_auto_created=False):
        """Return app models allowed to be migrated on provided db."""
        models = app_config.get_models(include_auto_created=include_auto_created)
        return [model for model in models if self.allow_migrate_model(db, model)]

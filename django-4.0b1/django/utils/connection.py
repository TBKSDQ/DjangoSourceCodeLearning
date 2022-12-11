from asgiref.local import Local

from django.conf import settings as django_settings
from django.utils.functional import cached_property


class ConnectionProxy:
    """
    该类用于代理ConnectionHandler中连接对象，通过当前Proxy类来操作
    Proxy for accessing a connection object's attributes.
    """

    def __init__(self, connections, alias):
        """
        :params connections: ConnectionHandler的实例对象
        :params alias: settings.py中数据库连接信息的别名
        """
        self.__dict__['_connections'] = connections
        self.__dict__['_alias'] = alias

    def __getattr__(self, item):
        """
        从ConnectionHandler中查找连接别名为alias的连接对象，然后再获取对应的属性
        """
        return getattr(self._connections[self._alias], item)

    def __setattr__(self, name, value):
        """
        设置属性
        """
        return setattr(self._connections[self._alias], name, value)

    def __delattr__(self, name):
        return delattr(self._connections[self._alias], name)

    def __contains__(self, key):
        return key in self._connections[self._alias]

    def __eq__(self, other):
        return self._connections[self._alias] == other


class ConnectionDoesNotExist(Exception):
    pass


class BaseConnectionHandler:
    settings_name = None
    exception_class = ConnectionDoesNotExist
    thread_critical = False

    def __init__(self, settings=None):
        self._settings = settings
        self._connections = Local(self.thread_critical)

    # 使用该装饰器装饰后，只会在首次执行对应的方法，后面对会从当前实例对象中获取属性值
    @cached_property
    def settings(self):
        self._settings = self.configure_settings(self._settings)
        # 返回配置好的self._settings
        return self._settings

    def configure_settings(self, settings):
        """
        获取settings.py中的数据库配置信息
        :params settings: settings就是当前__init__中的self._settings, 默认在对象初始化时就会设置该值为None(没有外部传入的情况下)
        """
        # 如果settings为None，代表之前没有传入或者说还没有执行过该方法
        if settings is None:
            # django_settings就是django.conf里面的settings
            # 从settings中获取数据库的配置信息，返回的内容就是在配置文件中定义的数据库信息(字典格式)
            settings = getattr(django_settings, self.settings_name)
        # 返回数据库配置信息字典
        return settings

    def create_connection(self, alias):
        raise NotImplementedError('Subclasses must implement create_connection().')

    def __getitem__(self, alias):
        """
        当以"connections['default']"的形式来获取一个对象的属性时，会调用__getitem__方法
        """
        try:
            return getattr(self._connections, alias)
        except AttributeError:
            # 当self._connections中找不到对应名字的连接时，会判断self.settings是否存在对应的连接名(settings.py中的数据库配置信息名)
            if alias not in self.settings:  # self.settings包含了数据库配置信息
                # 如果该名字不存在于配置信息中，抛出异常
                raise self.exception_class(f"The connection '{alias}' doesn't exist.")
        # 当前类没有实现create_connection，需要看回继承该类的子类中的实现
        # 通过create_connection方法得到一个DatabaseWrapper实例
        conn = self.create_connection(alias)
        # 将该实例记录在_connections中，这样下次再获取时就无需重复创建
        setattr(self._connections, alias, conn)
        return conn

    def __setitem__(self, key, value):
        setattr(self._connections, key, value)

    def __delitem__(self, key):
        delattr(self._connections, key)

    def __iter__(self):
        return iter(self.settings)

    def all(self):
        return [self[alias] for alias in self]

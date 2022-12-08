"""
Settings and configuration for Django.

Read values from the module specified by the DJANGO_SETTINGS_MODULE environment
variable, and then from django.conf.global_settings; see the global_settings.py
for a list of all possible variables.
"""

import importlib
import os
import time
import traceback
import warnings
from pathlib import Path

import django
from django.conf import global_settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.deprecation import RemovedInDjango50Warning
from django.utils.functional import LazyObject, empty

ENVIRONMENT_VARIABLE = "DJANGO_SETTINGS_MODULE"

# RemovedInDjango50Warning
USE_DEPRECATED_PYTZ_DEPRECATED_MSG = (
    'The USE_DEPRECATED_PYTZ setting, and support for pytz timezones is '
    'deprecated in favor of the stdlib zoneinfo module. Please update your '
    'code to use zoneinfo and remove the USE_DEPRECATED_PYTZ setting.'
)

USE_L10N_DEPRECATED_MSG = (
    'The USE_L10N setting is deprecated. Starting with Django 5.0, localized '
    'formatting of data will always be enabled. For example Django will '
    'display numbers and dates using the format of the current locale.'
)


class SettingsReference(str):
    """
    String subclass which references a current settings value. It's treated as
    the value in memory but serializes to a settings.NAME attribute reference.
    """
    def __new__(self, value, setting_name):
        return str.__new__(self, value)

    def __init__(self, value, setting_name):
        self.setting_name = setting_name


class LazySettings(LazyObject):
    """
    LazySettings用于加载django全局配置文件(global_settings.py)和项目下的自定义配置文件(settings.py)
    用户可以在使用django前手动设置自定义配置文件的位置，如果不进行设置，那么django会直接使用
    "DJANGO_SETTINGS_MODULE"环境变量中所指向的配置文件。
    在python中，通过"对象.xxx"的方式来获取属性时，会先去对象的__dict__这个字典中查找，找不到就
    通过__getattr__方法来查找。LazySettings的实现主要是依赖于这个机制。
    A lazy proxy for either global Django settings or a custom settings object.
    The user can manually configure settings prior to using them. Otherwise,
    Django uses the settings module pointed to by DJANGO_SETTINGS_MODULE.
    """
    def _setup(self, name=None):
        """
        加载环境变量中指向的配置文件(在调用LazySettings实例时需要先设置好指向配置文件的环境变量)。
        Load the settings module pointed to by the environment variable. This
        is used the first time settings are needed, if the user hasn't
        configured settings manually.
        """
        # 获取环境变量指向的配置文件路径
        settings_module = os.environ.get(ENVIRONMENT_VARIABLE)
        # 没有获取到值时会抛出异常
        # 在通过"python"命令进入shell时，如果想调用django.conf下的settings，那么必须自己设置好环境变量
        if not settings_module:
            desc = ("setting %s" % name) if name else "settings"
            raise ImproperlyConfigured(
                "Requested %s, but settings are not configured. "
                "You must either define the environment variable %s "
                "or call settings.configure() before accessing settings."
                % (desc, ENVIRONMENT_VARIABLE))

        # 将获取到的配置文件路径作为Settings的参数，返回一个包含着配置文件属性信息的Settings实例对象
        self._wrapped = Settings(settings_module)

    def __repr__(self):
        # Hardcode the class name as otherwise it yields 'Settings'.
        if self._wrapped is empty:
            return '<LazySettings [Unevaluated]>'
        return '<LazySettings "%(settings_module)s">' % {
            'settings_module': self._wrapped.SETTINGS_MODULE,
        }

    def __getattr__(self, name):
        """
        返回配置文件中的属性值并将该属性缓存到当前LazySettings实例对象的__dict__中
        Return the value of a setting and cache it in self.__dict__.
        """
        # self._wrapped默认在创建实例时就赋值为了一个object实例(self._wrapped = empty)
        # 当通过LazySettings实例对象获取属性时(也就是说__dict__中没有找到对应的属性)，那么会判断是否为object实例
        # 是的话调用_setup方法来加载配置文件中的信息
        if self._wrapped is empty:
            self._setup(name)
        # 至此，全局配置文件和用户自定义配置文件都已经加载完成
        # 尝试从其中查找属性
        val = getattr(self._wrapped, name)

        # Special case some settings which require further modification.
        # This is done here for performance reasons so the modified value is cached.
        # MEDIA_URL、STATIC_URL、SECRET_KEY属性的处理
        if name in {'MEDIA_URL', 'STATIC_URL'} and val is not None:
            val = self._add_script_prefix(val)
        elif name == 'SECRET_KEY' and not val:
            raise ImproperlyConfigured("The SECRET_KEY setting must not be empty.")

        # 将从self._wrapped中查找到的存入到__dict__中，这样后面再获取相同的属性时不再需要通过__getattr__来查找
        self.__dict__[name] = val
        return val

    def __setattr__(self, name, value):
        """
        Set the value of setting. Clear all cached values if _wrapped changes
        (@override_settings does this) or clear single values when set.
        """
        if name == '_wrapped':
            self.__dict__.clear()
        else:
            self.__dict__.pop(name, None)
        super().__setattr__(name, value)

    def __delattr__(self, name):
        """Delete a setting and clear it from cache if needed."""
        super().__delattr__(name)
        self.__dict__.pop(name, None)

    def configure(self, default_settings=global_settings, **options):
        """
        Called to manually configure the settings. The 'default_settings'
        parameter sets where to retrieve any unspecified values from (its
        argument must support attribute access (__getattr__)).
        """
        if self._wrapped is not empty:
            raise RuntimeError('Settings already configured.')
        holder = UserSettingsHolder(default_settings)
        for name, value in options.items():
            if not name.isupper():
                raise TypeError('Setting %r must be uppercase.' % name)
            setattr(holder, name, value)
        self._wrapped = holder

    @staticmethod
    def _add_script_prefix(value):
        """
        Add SCRIPT_NAME prefix to relative paths.

        Useful when the app is being served at a subpath and manually prefixing
        subpath to STATIC_URL and MEDIA_URL in settings is inconvenient.
        """
        # Don't apply prefix to absolute paths and URLs.
        if value.startswith(('http://', 'https://', '/')):
            return value
        from django.urls import get_script_prefix
        return '%s%s' % (get_script_prefix(), value)

    @property
    def configured(self):
        """Return True if the settings have already been configured."""
        return self._wrapped is not empty

    @property
    def USE_L10N(self):
        stack = traceback.extract_stack()
        # Show a warning if the setting is used outside of Django.
        # Stack index: -1 this line, -2 the caller.
        filename, _, _, _ = stack[-2]
        if not filename.startswith(os.path.dirname(django.__file__)):
            warnings.warn(
                USE_L10N_DEPRECATED_MSG,
                RemovedInDjango50Warning,
                stacklevel=2,
            )
        return self.__getattr__('USE_L10N')

    # RemovedInDjango50Warning.
    @property
    def _USE_L10N_INTERNAL(self):
        # Special hook to avoid checking a traceback in internal use on hot
        # paths.
        return self.__getattr__('USE_L10N')


class Settings:
    def __init__(self, settings_module):
        # update this dict from global settings (but only for ALL_CAPS settings)
        # 列举出global_settings下的所有变量，只将全部字符为大写的加入到当前实例对象的__dict__中
        for setting in dir(global_settings):
            if setting.isupper():
                setattr(self, setting, getattr(global_settings, setting))

        # store the settings module in case someone later cares
        self.SETTINGS_MODULE = settings_module

        # 尝试导入配置文件
        mod = importlib.import_module(self.SETTINGS_MODULE)

        tuple_settings = (
            'ALLOWED_HOSTS',
            "INSTALLED_APPS",
            "TEMPLATE_DIRS",
            "LOCALE_PATHS",
        )
        self._explicit_settings = set()
        # 遍历配置文件模块中所有变量，将字符都为大写的存放到__dict__当中
        for setting in dir(mod):
            if setting.isupper():
                setting_value = getattr(mod, setting)

                # 检查tuple_settings中记录的属性是否为列表或者元组，不是则抛出异常
                if (setting in tuple_settings and
                        not isinstance(setting_value, (list, tuple))):
                    raise ImproperlyConfigured("The %s setting must be a list or a tuple." % setting)
                setattr(self, setting, setting_value)
                self._explicit_settings.add(setting)

        if self.USE_TZ is False and not self.is_overridden('USE_TZ'):
            warnings.warn(
                'The default value of USE_TZ will change from False to True '
                'in Django 5.0. Set USE_TZ to False in your project settings '
                'if you want to keep the current default behavior.',
                category=RemovedInDjango50Warning,
            )

        if self.is_overridden('USE_DEPRECATED_PYTZ'):
            warnings.warn(USE_DEPRECATED_PYTZ_DEPRECATED_MSG, RemovedInDjango50Warning)

        if hasattr(time, 'tzset') and self.TIME_ZONE:
            # When we can, attempt to validate the timezone. If we can't find
            # this file, no check happens and it's harmless.
            zoneinfo_root = Path('/usr/share/zoneinfo')
            zone_info_file = zoneinfo_root.joinpath(*self.TIME_ZONE.split('/'))
            if zoneinfo_root.exists() and not zone_info_file.exists():
                raise ValueError("Incorrect timezone setting: %s" % self.TIME_ZONE)
            # Move the time zone info into os.environ. See ticket #2315 for why
            # we don't do this unconditionally (breaks Windows).
            os.environ['TZ'] = self.TIME_ZONE
            time.tzset()

        if self.is_overridden('USE_L10N'):
            warnings.warn(USE_L10N_DEPRECATED_MSG, RemovedInDjango50Warning)

    def is_overridden(self, setting):
        return setting in self._explicit_settings

    def __repr__(self):
        return '<%(cls)s "%(settings_module)s">' % {
            'cls': self.__class__.__name__,
            'settings_module': self.SETTINGS_MODULE,
        }


class UserSettingsHolder:
    """Holder for user configured settings."""
    # SETTINGS_MODULE doesn't make much sense in the manually configured
    # (standalone) case.
    SETTINGS_MODULE = None

    def __init__(self, default_settings):
        """
        Requests for configuration variables not in this class are satisfied
        from the module specified in default_settings (if possible).
        """
        self.__dict__['_deleted'] = set()
        self.default_settings = default_settings

    def __getattr__(self, name):
        if not name.isupper() or name in self._deleted:
            raise AttributeError
        return getattr(self.default_settings, name)

    def __setattr__(self, name, value):
        self._deleted.discard(name)
        if name == 'USE_L10N':
            warnings.warn(USE_L10N_DEPRECATED_MSG, RemovedInDjango50Warning)
        super().__setattr__(name, value)
        if name == 'USE_DEPRECATED_PYTZ':
            warnings.warn(USE_DEPRECATED_PYTZ_DEPRECATED_MSG, RemovedInDjango50Warning)

    def __delattr__(self, name):
        self._deleted.add(name)
        if hasattr(self, name):
            super().__delattr__(name)

    def __dir__(self):
        return sorted(
            s for s in [*self.__dict__, *dir(self.default_settings)]
            if s not in self._deleted
        )

    def is_overridden(self, setting):
        deleted = (setting in self._deleted)
        set_locally = (setting in self.__dict__)
        set_on_default = getattr(self.default_settings, 'is_overridden', lambda s: False)(setting)
        return deleted or set_locally or set_on_default

    def __repr__(self):
        return '<%(cls)s>' % {
            'cls': self.__class__.__name__,
        }


# settings是LazySettings的实例，用于加载global_settings.py和项目下settings.py中的变量
settings = LazySettings()

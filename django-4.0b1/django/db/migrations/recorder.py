from django.apps.registry import Apps
from django.db import DatabaseError, models
from django.utils.functional import classproperty
from django.utils.timezone import now

from .exceptions import MigrationSchemaMissing


class MigrationRecorder:
    """
    Deal with storing migration records in the database.
    处理存储在数据库中的迁移记录(实际对迁移记录表操作的类)

    Because this table is actually itself used for dealing with model
    creation, it's the one thing we can't do normally via migrations.
    We manually handle table creation/schema updating (using schema backend)
    and then have a floating model to do queries with.

    If a migration is unapplied its row is removed from the table. Having
    a row in the table always means a migration is applied.
    """
    _migration_class = None

    @classproperty
    def Migration(cls):
        """
        Lazy load to avoid AppRegistryNotReady if installed apps import
        MigrationRecorder.
        """
        # 如果_migration_class为空，那么就会设置为其内部定义的Migration
        if cls._migration_class is None:
            class Migration(models.Model):  # 迁移模型类
                app = models.CharField(max_length=255)
                name = models.CharField(max_length=255)
                applied = models.DateTimeField(default=now)

                class Meta:
                    apps = Apps()
                    app_label = 'migrations'  # 应用标签
                    db_table = 'django_migrations'  # 表名

                def __str__(self):
                    return 'Migration %s for %s' % (self.name, self.app)

            cls._migration_class = Migration
        return cls._migration_class

    def __init__(self, connection):
        self.connection = connection

    @property
    def migration_qs(self):
        """查询所有的迁移记录"""
        return self.Migration.objects.using(self.connection.alias)

    def has_table(self):
        """判断是否已经存在django的迁移记录表，存在返回True"""
        """Return True if the django_migrations table exists."""
        with self.connection.cursor() as cursor:
            tables = self.connection.introspection.table_names(cursor)
        return self.Migration._meta.db_table in tables

    def ensure_schema(self):
        """Ensure the table exists and has the correct schema."""
        """确认是否已经存在django的迁移记录表，不存在则进行创建"""
        # If the table's there, that's fine - we've never changed its schema
        # in the codebase.
        if self.has_table():
            return
        # Make the table
        try:
            with self.connection.schema_editor() as editor:
                editor.create_model(self.Migration)
        except DatabaseError as exc:
            raise MigrationSchemaMissing("Unable to create the django_migrations table (%s)" % exc)

    def applied_migrations(self):
        """
        以(app_name, migration_name)的格式返回所有迁移记录(从数据库中查询)
        Return a dict mapping (app_name, migration_name) to Migration instances
        for all applied migrations.
        """
        if self.has_table():
            return {(migration.app, migration.name): migration for migration in self.migration_qs}
        else:
            # If the django_migrations table doesn't exist, then no migrations
            # are applied.
            return {}

    def record_applied(self, app, name):
        """
        记录一条迁移记录
        Record that a migration was applied.
        """
        self.ensure_schema()
        self.migration_qs.create(app=app, name=name)

    def record_unapplied(self, app, name):
        """
        移除一条迁移记录
        Record that a migration was unapplied.
        """
        self.ensure_schema()
        self.migration_qs.filter(app=app, name=name).delete()

    def flush(self):
        """
        删除所有迁移记录
        Delete all migration records. Useful for testing migrations.
        """
        self.migration_qs.all().delete()

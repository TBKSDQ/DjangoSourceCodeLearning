import pkgutil
import sys
from importlib import import_module, reload

from django.apps import apps
from django.conf import settings
from django.db.migrations.graph import MigrationGraph
from django.db.migrations.recorder import MigrationRecorder

from .exceptions import (
    AmbiguityError, BadMigrationError, InconsistentMigrationHistory,
    NodeNotFoundError,
)

MIGRATIONS_MODULE_NAME = 'migrations'


class MigrationLoader:
    """
    该类的作用主要是加载迁移记录以及根据记录构建迁移图
    Load migration files from disk and their status from the database.
    从本地加载迁移文件以及从数据库中加载迁移记录

    Migration files are expected to live in the "migrations" directory of
    an app. Their names are entirely unimportant from a code perspective,
    but will probably follow the 1234_name.py convention.

    On initialization, this class will scan those directories, and open and
    read the Python files, looking for a class called Migration, which should
    inherit from django.db.migrations.Migration. See
    django.db.migrations.migration for what that looks like.

    Some migrations will be marked as "replacing" another set of migrations.
    These are loaded into a separate set of migrations away from the main ones.
    If all the migrations they replace are either unapplied or missing from
    disk, then they are injected into the main set, replacing the named migrations.
    Any dependency pointers to the replaced migrations are re-pointed to the
    new migration.

    This does mean that this class MUST also talk to the database as well as
    to disk, but this is probably fine. We're already not just operating
    in memory.
    """

    def __init__(
        self, connection, load=True, ignore_no_migrations=False,
        replace_migrations=True,
    ):
        self.connection = connection
        # disk_migrations会在下面调用了build_graph后进行数据的填充
        self.disk_migrations = None
        self.applied_migrations = None
        self.ignore_no_migrations = ignore_no_migrations
        self.replace_migrations = replace_migrations
        if load:
            self.build_graph()

    @classmethod
    def migrations_module(cls, app_label):
        """
        根据给定的app_label来返回迁移模块的路径(也就是app路径下的migrations文件夹)以及一个布尔值，
        如果这个模块有在setttings.MIGRATION_MODULE中，那么布尔值为True.
        返回的数据格式与这个相似: ('books.migrations', False)
        Return the path to the migrations module for the specified app_label
        and a boolean indicating if the module is specified in
        settings.MIGRATION_MODULE.
        """
        if app_label in settings.MIGRATION_MODULES:
            return settings.MIGRATION_MODULES[app_label], True
        else:
            app_package_name = apps.get_app_config(app_label).name
            return '%s.%s' % (app_package_name, MIGRATIONS_MODULE_NAME), False

    def load_disk(self):
        """
        加载所有INSTALLED_APPS记录的应用的迁移文件
        Load the migrations from all INSTALLED_APPS from disk.
        """
        self.disk_migrations = {}
        self.unmigrated_apps = set()
        self.migrated_apps = set()
        # 通过apps获取所有已经在INSTALLED_APPS中记录的应用
        for app_config in apps.get_app_configs():
            # Get the migrations module directory
            # 经过migrations_module方法处理，返回的module_name指向每个应用下的migrations文件夹
            # 例如这样: ('django.contrib.admin.migrations', False)
            module_name, explicit = self.migrations_module(app_config.label)
            # module_name为None的，都会记录到unmigrated_apps中
            if module_name is None:
                self.unmigrated_apps.add(app_config.label)
                continue
            was_loaded = module_name in sys.modules
            try:
                # 尝试导入模块(也就是app下的migrations)
                # 如果没有成功导入migrations模块，那么进入到异常处理，同时下面的migrated_apps不会记录该应用
                module = import_module(module_name)
            except ModuleNotFoundError as e:
                if (
                    (explicit and self.ignore_no_migrations) or
                    (not explicit and MIGRATIONS_MODULE_NAME in e.name.split('.'))
                ):
                    self.unmigrated_apps.add(app_config.label)
                    continue
                raise
            else:
                # Module is not a package (e.g. migrations.py).
                if not hasattr(module, '__path__'):
                    self.unmigrated_apps.add(app_config.label)
                    continue
                # Empty directories are namespaces. Namespace packages have no
                # __file__ and don't use a list for __path__. See
                # https://docs.python.org/3/reference/import.html#namespace-packages
                if (
                    getattr(module, '__file__', None) is None and
                    not isinstance(module.__path__, list)
                ):
                    self.unmigrated_apps.add(app_config.label)
                    continue
                # Force a reload if it's already loaded (tests need this)
                if was_loaded:
                    reload(module)
            # 将应用的标签名(也就是应用名)添加到migrated_apps中
            self.migrated_apps.add(app_config.label)
            migration_names = {
                name for _, name, is_pkg in pkgutil.iter_modules(module.__path__)
                if not is_pkg and name[0] not in '_~'
            }
            # Load migrations
            # 遍历上面获得的所有的app migrations下的模块名，也就是迁移文件名
            for migration_name in migration_names:
                # 将模块路径和迁移文件名拼接，然后导入
                migration_path = '%s.%s' % (module_name, migration_name)
                try:
                    migration_module = import_module(migration_path)
                except ImportError as e:
                    if 'bad magic number' in str(e):
                        raise ImportError(
                            "Couldn't import %r as it appears to be a stale "
                            ".pyc file." % migration_path
                        ) from e
                    else:
                        raise
                if not hasattr(migration_module, "Migration"):
                    raise BadMigrationError(
                        "Migration %s in app %s has no Migration class" % (migration_name, app_config.label)
                    )
                # 记录应用中的所有迁移记录(只涉及本地的迁移文件，不涉及数据库中的迁移记录)
                # disk_migrations的key是一个元组，value是Migration的实例化对象
                # django使用一个元组(app_config.label, migration_name)来区别迁移记录，
                # 本身django已经有一个，auth应用了，如果在自己项目下创建auth应用，在执行makemigrations时会报错，
                # 因为这两个应用虽然位置不同，大师最终在disk_migrations中的key是相同的
                self.disk_migrations[app_config.label, migration_name] = migration_module.Migration(
                    migration_name,
                    app_config.label,
                )

    def get_migration(self, app_label, name_prefix):
        """Return the named migration or raise NodeNotFoundError."""
        return self.graph.nodes[app_label, name_prefix]

    def get_migration_by_prefix(self, app_label, name_prefix):
        """
        Return the migration(s) which match the given app label and name_prefix.
        根据给定的app名称和迁移记录前缀来找到对应的迁移记录，最终返回该迁移记录的Migration实例
        """
        # Do the search
        results = []
        # 从加载好的本地迁移记录中查找名字符合给定的name_prefix的记录
        for migration_app_label, migration_name in self.disk_migrations:
            if migration_app_label == app_label and migration_name.startswith(name_prefix):
                results.append((migration_app_label, migration_name))
        # 如果查找出来的记录数量大于1，代表着所给的名字前缀不能够准确定位到一个迁移记录
        if len(results) > 1:
            raise AmbiguityError(
                "There is more than one migration for '%s' with the prefix '%s'" % (app_label, name_prefix)
            )
        # 找不到迁移记录的情况
        elif not results:
            raise KeyError(
                f"There is no migration for '{app_label}' with the prefix "
                f"'{name_prefix}'"
            )
        else:
            return self.disk_migrations[results[0]]

    def check_key(self, key, current_app):
        if (key[1] != "__first__" and key[1] != "__latest__") or key in self.graph:
            return key
        # Special-case __first__, which means "the first migration" for
        # migrated apps, and is ignored for unmigrated apps. It allows
        # makemigrations to declare dependencies on apps before they even have
        # migrations.
        if key[0] == current_app:
            # Ignore __first__ references to the same app (#22325)
            return
        if key[0] in self.unmigrated_apps:
            # This app isn't migrated, but something depends on it.
            # The models will get auto-added into the state, though
            # so we're fine.
            return
        if key[0] in self.migrated_apps:
            try:
                if key[1] == "__first__":
                    return self.graph.root_nodes(key[0])[0]
                else:  # "__latest__"
                    return self.graph.leaf_nodes(key[0])[0]
            except IndexError:
                if self.ignore_no_migrations:
                    return None
                else:
                    raise ValueError("Dependency on app with no migrations: %s" % key[0])
        raise ValueError("Dependency on unknown app: %s" % key[0])

    def add_internal_dependencies(self, key, migration):
        """
        检查当前传入的Migration有哪些依赖，将这些依赖添加到图中
        Internal dependencies need to be added first to ensure `__first__`
        dependencies find the correct root node.
        """
        for parent in migration.dependencies:
            # Ignore __first__ references to the same app.
            if parent[0] == key[0] and parent[1] != '__first__':
                self.graph.add_dependency(migration, key, parent, skip_validation=True)

    def add_external_dependencies(self, key, migration):
        """处理外部依赖"""
        for parent in migration.dependencies:
            # Skip internal dependencies
            if key[0] == parent[0]:
                continue
            parent = self.check_key(parent, key[0])
            if parent is not None:
                self.graph.add_dependency(migration, key, parent, skip_validation=True)
        for child in migration.run_before:
            child = self.check_key(child, key[0])
            if child is not None:
                self.graph.add_dependency(migration, child, key, skip_validation=True)

    def build_graph(self):
        """
        根据数据库和本地的迁移文件构建迁移图
        Build a migration dependency graph using both the disk and database.
        You'll need to rebuild the graph if you apply migrations. This isn't
        usually a problem as generally migration stuff runs in a one-shot process.
        """
        # 从本地加载迁移记录
        # Load disk data
        self.load_disk()
        # 从数据库加载迁移记录
        # Load database data
        if self.connection is None:
            self.applied_migrations = {}
        else:
            recorder = MigrationRecorder(self.connection)
            self.applied_migrations = recorder.applied_migrations()
        # To start, populate the migration graph with nodes for ALL migrations
        # 使用所有的迁移记录来创建节点并填充迁移图，然后处理节点间的依赖
        # and their dependencies. Also make note of replacing migrations at this step.
        self.graph = MigrationGraph()
        self.replacements = {}  # 记录replace
        # disk_migrations是一个元素，key为应用名，value为Migration实例对象
        for key, migration in self.disk_migrations.items():  # 通过for循环来将所有迁移记录添加到图中
            self.graph.add_node(key, migration)
            # Replacing migrations.
            if migration.replaces:
                self.replacements[key] = migration
        for key, migration in self.disk_migrations.items():  # 处理内部依赖
            # Internal (same app) dependencies.
            self.add_internal_dependencies(key, migration)
        # Add external dependencies now that the internal ones have been resolved.
        for key, migration in self.disk_migrations.items():  # 处理外部依赖
            self.add_external_dependencies(key, migration)
        # Carry out replacements where possible and if enabled.
        # 处理replace
        if self.replace_migrations:
            for key, migration in self.replacements.items():
                # Get applied status of each of this migration's replacement
                # targets.
                applied_statuses = [(target in self.applied_migrations) for target in migration.replaces]
                # The replacing migration is only marked as applied if all of
                # its replacement targets are.
                if all(applied_statuses):
                    self.applied_migrations[key] = migration
                else:
                    self.applied_migrations.pop(key, None)
                # A replacing migration can be used if either all or none of
                # its replacement targets have been applied.
                if all(applied_statuses) or (not any(applied_statuses)):
                    self.graph.remove_replaced_nodes(key, migration.replaces)
                else:
                    # This replacing migration cannot be used because it is
                    # partially applied. Remove it from the graph and remap
                    # dependencies to it (#25945).
                    self.graph.remove_replacement_node(key, migration.replaces)
        # Ensure the graph is consistent.
        # 判断图中是否仍然有DummyNode
        try:
            self.graph.validate_consistency()
        except NodeNotFoundError as exc:
            # Check if the missing node could have been replaced by any squash
            # migration but wasn't because the squash migration was partially
            # applied before. In that case raise a more understandable exception
            # (#23556).
            # Get reverse replacements.
            reverse_replacements = {}
            for key, migration in self.replacements.items():
                for replaced in migration.replaces:
                    reverse_replacements.setdefault(replaced, set()).add(key)
            # Try to reraise exception with more detail.
            if exc.node in reverse_replacements:
                candidates = reverse_replacements.get(exc.node, set())
                is_replaced = any(candidate in self.graph.nodes for candidate in candidates)
                if not is_replaced:
                    tries = ', '.join('%s.%s' % c for c in candidates)
                    raise NodeNotFoundError(
                        "Migration {0} depends on nonexistent node ('{1}', '{2}'). "
                        "Django tried to replace migration {1}.{2} with any of [{3}] "
                        "but wasn't able to because some of the replaced migrations "
                        "are already applied.".format(
                            exc.origin, exc.node[0], exc.node[1], tries
                        ),
                        exc.node
                    ) from exc
            raise
        # 确保图中不会存在循环问题
        self.graph.ensure_not_cyclic()

    def check_consistent_history(self, connection):
        """
        检查迁移记录的一致性，判断迁移记录的父级是否存在相应的迁移记录，
        如果没有，那么抛出异常(因为没有上一级的迁移不可能有当前的迁移，如果
        出现了这种情况，代表着数据库中迁移记录有缺失)。
        Raise InconsistentMigrationHistory if any applied migrations have
        unapplied dependencies.
        """
        recorder = MigrationRecorder(connection)
        # 从数据库查询迁移记录
        applied = recorder.applied_migrations()
        for migration in applied:
            # If the migration is unknown, skip it.
            if migration not in self.graph.nodes:
                continue
            # 遍历当前迁移记录的所有父级
            for parent in self.graph.node_map[migration].parents:
                # 判断父级是否已经执行过迁移
                if parent not in applied:
                    # Skip unapplied squashed migrations that have all of their
                    # `replaces` applied.
                    if parent in self.replacements:
                        if all(m in applied for m in self.replacements[parent].replaces):
                            continue
                    # 如果父级没有执行过迁移，而其子却已经进行了迁移，那么抛出异常
                    raise InconsistentMigrationHistory(
                        "Migration {}.{} is applied before its dependency "
                        "{}.{} on database '{}'.".format(
                            migration[0], migration[1], parent[0], parent[1],
                            connection.alias,
                        )
                    )

    def detect_conflicts(self):
        """
        检查迁移图中是否有冲突(即是否有两个叶子节点)
        比如0002依赖于0001，0003也依赖于00001，那么这时候就会有冲突
        Look through the loaded graph and detect any conflicts - apps
        with more than one leaf migration. Return a dict of the app labels
        that conflict with the migration names that conflict.
        """
        seen_apps = {}
        conflicting_apps = set()
        for app_label, migration_name in self.graph.leaf_nodes():
            if app_label in seen_apps:
                conflicting_apps.add(app_label)
            seen_apps.setdefault(app_label, set()).add(migration_name)
        return {app_label: sorted(seen_apps[app_label]) for app_label in conflicting_apps}

    def project_state(self, nodes=None, at_end=True):
        """
        Return a ProjectState object representing the most recent state
        that the loaded migrations represent.

        See graph.make_state() for the meaning of "nodes" and "at_end".
        """
        return self.graph.make_state(nodes=nodes, at_end=at_end, real_apps=self.unmigrated_apps)

    def collect_sql(self, plan):
        """
        Take a migration plan and return a list of collected SQL statements
        that represent the best-efforts version of that plan.
        """
        statements = []
        state = None
        for migration, backwards in plan:
            with self.connection.schema_editor(collect_sql=True, atomic=migration.atomic) as schema_editor:
                if state is None:
                    state = self.project_state((migration.app_label, migration.name), at_end=False)
                if not backwards:
                    state = migration.apply(state, schema_editor, collect_sql=True)
                else:
                    state = migration.unapply(state, schema_editor, collect_sql=True)
            statements.extend(schema_editor.collected_sql)
        return statements

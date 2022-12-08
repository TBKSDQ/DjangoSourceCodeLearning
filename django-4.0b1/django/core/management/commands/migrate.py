import sys
import time
from importlib import import_module

from django.apps import apps
from django.core.management.base import (
    BaseCommand, CommandError, no_translations,
)
from django.core.management.sql import (
    emit_post_migrate_signal, emit_pre_migrate_signal,
)
from django.db import DEFAULT_DB_ALIAS, connections, router
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import AmbiguityError
from django.db.migrations.state import ModelState, ProjectState
from django.utils.module_loading import module_has_submodule
from django.utils.text import Truncator


class Command(BaseCommand):
    help = "Updates database schema. Manages both apps with migrations and those without."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument(
            '--skip-checks', action='store_true',
            help='Skip system checks.',
        )
        # 接收应用名称
        parser.add_argument(
            'app_label', nargs='?',
            help='App label of an application to synchronize the state.',
        )
        # 迁移名称(迁移文件名和数据库中的迁移记录名都会根据这个设置)
        parser.add_argument(
            'migration_name', nargs='?',
            help='Database state will be brought to the state after that '
                 'migration. Use the name "zero" to unapply all migrations.',
        )
        parser.add_argument(
            '--noinput', '--no-input', action='store_false', dest='interactive',
            help='Tells Django to NOT prompt the user for input of any kind.',
        )
        # 指定使用哪个数据库连接来同步迁移，默认为settings.py中设置的default连接
        parser.add_argument(
            '--database',
            default=DEFAULT_DB_ALIAS,
            help='Nominates a database to synchronize. Defaults to the "default" database.',
        )
        # 传递fake参数后，会将迁移记录同步到数据库，但是不会根据迁移记录去对表进行实际的操作
        parser.add_argument(
            '--fake', action='store_true',
            help='Mark migrations as run without actually running them.',
        )
        parser.add_argument(
            '--fake-initial', action='store_true',
            help='Detect if tables already exist and fake-apply initial migrations if so. Make sure '
                 'that the current database schema matches your initial migration before using this '
                 'flag. Django will only check for an existing table name.',
        )
        # 输出本次迁移要执行的操作(不会在数据库中增加迁移记录以及不会修改表)
        parser.add_argument(
            '--plan', action='store_true',
            help='Shows a list of the migration actions that will be performed.',
        )
        # 为应用创建表(不记录迁移)
        # 要执行该操作的应用目录下不能存在migrations文件夹，否则会报错
        parser.add_argument(
            '--run-syncdb', action='store_true',
            help='Creates tables for apps without migrations.',
        )
        parser.add_argument(
            '--check', action='store_true', dest='check_unapplied',
            help='Exits with a non-zero status if unapplied migrations exist.',
        )

    @no_translations
    def handle(self, *args, **options):
        # 获取需要使用哪个数据库连接名，默认为default
        database = options['database']
        if not options['skip_checks']:
            self.check(databases=[database])

        self.verbosity = options['verbosity']
        self.interactive = options['interactive']

        # Import the 'management' module within each installed app, to register
        # dispatcher events.
        # 导入每个记录在installed_app中的应用的management模块(一般就是django自己的应用才会有这个management模块)
        for app_config in apps.get_app_configs():
            if module_has_submodule(app_config.module, "management"):
                import_module('.management', app_config.name)

        # Get the database we're operating from
        # 根据数据库连接名来获取数据库连接
        connection = connections[database]

        # Hook for backends needing any database preparation
        connection.prepare_database()
        # Work out which apps have migrations and which do not
        # 检查出哪些应用有迁移记录，哪些没有(内部创建了MigrationLoader和MigrationRecorder的实例)
        executor = MigrationExecutor(connection, self.migration_progress_callback)

        # Raise an error if any migrations are applied before their dependencies.
        # 一致性检查(本地迁移记录和数据库迁移记录的检查)
        executor.loader.check_consistent_history(connection)

        # Before anything else, see if there's conflicting apps and drop out
        # hard if there are any
        # 检查冲突，有冲突就会直接抛出异常
        conflicts = executor.loader.detect_conflicts()
        if conflicts:
            name_str = "; ".join(
                "%s in %s" % (", ".join(names), app)
                for app, names in conflicts.items()
            )
            raise CommandError(
                "Conflicting migrations detected; multiple leaf nodes in the "
                "migration graph: (%s).\nTo fix them run "
                "'python manage.py makemigrations --merge'" % name_str
            )

        # If they supplied command line arguments, work out what they mean.
        run_syncdb = options['run_syncdb']
        target_app_labels_only = True
        # 检查命令中是否有指定应用标签，如果有指定
        # 那么就尝试通过get_app_config来验证app是否有效(也就是有没有在settings.py中注册)
        if options['app_label']:
            # Validate app_label.
            app_label = options['app_label']
            try:
                apps.get_app_config(app_label)
            except LookupError as err:
                raise CommandError(str(err))
            # run_syncdb作用是创建表但不记录迁移，如果app存在于migrated_apps中，那么会抛出异常
            if run_syncdb:
                if app_label in executor.loader.migrated_apps:
                    raise CommandError("Can't use run_syncdb with app '%s' as it has migrations." % app_label)
            elif app_label not in executor.loader.migrated_apps:
                raise CommandError("App '%s' does not have migrations." % app_label)

        # 这部分的作用主要是得到targets，也就是找到尚未执行迁移操作的记录(一般是叶子节点的记录)
        # 命令中传递了应用名和迁移名的情况
        if options['app_label'] and options['migration_name']:
            migration_name = options['migration_name']
            # 如果migration_name为zero，应该会尝试将该app的所有迁移撤销掉
            if migration_name == "zero":
                # 这个None值会在migration_plan中被标识为需要撤销迁移
                targets = [(app_label, None)]
            else:
                try:
                    # 根据迁移名来得到对应的Migration类
                    migration = executor.loader.get_migration_by_prefix(app_label, migration_name)
                except AmbiguityError:  # 前缀名不能准确定位到某个迁移记录的情况
                    raise CommandError(
                        "More than one migration matches '%s' in app '%s'. "
                        "Please be more specific." %
                        (migration_name, app_label)
                    )
                except KeyError:
                    raise CommandError("Cannot find a migration matching '%s' from app '%s'." % (
                        migration_name, app_label))
                # 将应用名和迁移名组合成一个元组
                target = (app_label, migration.name)
                # Partially applied squashed migrations are not included in the
                # graph, use the last replacement instead.
                # 正常来说上面的迁移是会在迁移图中记录有的
                # 如果没有出现在nodes里，而是出现在replacements里，那么这个可能是要用于替换的(尚未确认清楚)
                if (
                    target not in executor.loader.graph.nodes and
                    target in executor.loader.replacements
                ):
                    incomplete_migration = executor.loader.replacements[target]
                    target = incomplete_migration.replaces[-1]
                # 将target放入targets
                targets = [target]
            target_app_labels_only = False
        # 只传递了app名的情况
        # 从迁移图中找出传递的app名对应的叶子节点记录(格式为: ('admin', '0003_logentry_add_action_flag_choices'))
        elif options['app_label']:
            targets = [key for key in executor.loader.graph.leaf_nodes() if key[0] == app_label]
        else:  # 没有传递app名就获取所有的app的叶子节点记录
            targets = executor.loader.graph.leaf_nodes()

        # 根据targets，调用migration_plan来获得每个迁移节点应该做的操作(是撤销迁移还是增加迁移)
        plan = executor.migration_plan(targets)
        exit_dry = plan and options['check_unapplied']

        # 如果命令中传递了--plan参数，那么需要输出本次迁移所需要做的操作
        if options['plan']:
            self.stdout.write('Planned operations:', self.style.MIGRATE_LABEL)
            # plan为空则代表着没有迁移需要执行
            if not plan:
                self.stdout.write('  No planned migration operations.')
            for migration, backwards in plan:
                self.stdout.write(str(migration), self.style.MIGRATE_HEADING)
                for operation in migration.operations:
                    message, is_error = self.describe_operation(operation, backwards)
                    style = self.style.WARNING if is_error else None
                    self.stdout.write('    ' + message, style)
            if exit_dry:
                sys.exit(1)
            return
        if exit_dry:
            sys.exit(1)

        # At this point, ignore run_syncdb if there aren't any apps to sync.
        # 如果没有app是尚未迁移过的，那么就忽略run_syncdb选项
        # 从unmigrated_apps中获取未曾迁移过的app，以及查看命令中是否传递了run_syncdb参数
        run_syncdb = options['run_syncdb'] and executor.loader.unmigrated_apps
        # Print some useful info
        if self.verbosity >= 1:
            self.stdout.write(self.style.MIGRATE_HEADING("Operations to perform:"))
            if run_syncdb:
                # 有指定app就只输出该app的提示信息
                if options['app_label']:
                    self.stdout.write(
                        self.style.MIGRATE_LABEL("  Synchronize unmigrated app: %s" % app_label)
                    )
                else:
                    self.stdout.write(
                        self.style.MIGRATE_LABEL("  Synchronize unmigrated apps: ") +
                        (", ".join(sorted(executor.loader.unmigrated_apps)))
                    )
            if target_app_labels_only:
                self.stdout.write(
                    self.style.MIGRATE_LABEL("  Apply all migrations: ") +
                    (", ".join(sorted({a for a, n in targets})) or "(none)")
                )
            else:
                if targets[0][1] is None:
                    self.stdout.write(
                        self.style.MIGRATE_LABEL('  Unapply all migrations: ') +
                        str(targets[0][0])
                    )
                else:
                    self.stdout.write(self.style.MIGRATE_LABEL(
                        "  Target specific migration: ") + "%s, from %s"
                        % (targets[0][1], targets[0][0])
                    )
        # 猜测是获得当前项目的整体状态
        pre_migrate_state = executor._create_project_state(with_applied_migrations=True)
        pre_migrate_apps = pre_migrate_state.apps
        # 发送信号，这样可以触发那些要在前执行的事件
        emit_pre_migrate_signal(
            self.verbosity, self.interactive, connection.alias, stdout=self.stdout, apps=pre_migrate_apps, plan=plan,
        )

        # Run the syncdb phase.
        # 如果之前有传递run_syncdb参数并且能够正常到达这里，那么就要开始创建表了(但不记录迁移信息)
        if run_syncdb:
            if self.verbosity >= 1:
                self.stdout.write(self.style.MIGRATE_HEADING("Synchronizing apps without migrations:"))
            # 如果有指定app名称，那么就只会对指定的app进行操作，否则，会对所有
            # 位于unmigrated_apps中的app进行操作
            if options['app_label']:
                self.sync_apps(connection, [app_label])
            else:
                self.sync_apps(connection, executor.loader.unmigrated_apps)

        # Migrate!
        # 迁移操作
        if self.verbosity >= 1:
            self.stdout.write(self.style.MIGRATE_HEADING("Running migrations:"))
        # 如果没有迁移计划(也就是没有要迁移的应用)，那么会输出一些提示信息
        if not plan:
            if self.verbosity >= 1:
                self.stdout.write("  No migrations to apply.")
                # If there's changes that aren't in migrations yet, tell them how to fix it.
                autodetector = MigrationAutodetector(
                    executor.loader.project_state(),
                    ProjectState.from_apps(apps),
                )
                changes = autodetector.changes(graph=executor.loader.graph)
                if changes:
                    self.stdout.write(self.style.NOTICE(
                        "  Your models in app(s): %s have changes that are not "
                        "yet reflected in a migration, and so won't be "
                        "applied." % ", ".join(repr(app) for app in sorted(changes))
                    ))
                    self.stdout.write(self.style.NOTICE(
                        "  Run 'manage.py makemigrations' to make new "
                        "migrations, and then re-run 'manage.py migrate' to "
                        "apply them."
                    ))
            fake = False
            fake_initial = False
        else:
            # 获取fake参数状态(当命令行中传递了该参数，代表着只在表中记录迁移信息但不对其他表进行修改)。
            fake = options['fake']
            fake_initial = options['fake_initial']
        # 执行迁移
        post_migrate_state = executor.migrate(
            targets, plan=plan, state=pre_migrate_state.clone(), fake=fake,
            fake_initial=fake_initial,
        )
        # post_migrate signals have access to all models. Ensure that all models
        # are reloaded in case any are delayed.
        post_migrate_state.clear_delayed_apps_cache()
        post_migrate_apps = post_migrate_state.apps

        # Re-render models of real apps to include relationships now that
        # we've got a final state. This wouldn't be necessary if real apps
        # models were rendered with relationships in the first place.
        with post_migrate_apps.bulk_update():
            model_keys = []
            for model_state in post_migrate_apps.real_models:
                model_key = model_state.app_label, model_state.name_lower
                model_keys.append(model_key)
                post_migrate_apps.unregister_model(*model_key)
        post_migrate_apps.render_multiple([
            ModelState.from_model(apps.get_model(*model)) for model in model_keys
        ])

        # Send the post_migrate signal, so individual apps can do whatever they need
        # to do at this point.
        emit_post_migrate_signal(
            self.verbosity, self.interactive, connection.alias, stdout=self.stdout, apps=post_migrate_apps, plan=plan,
        )

    def migration_progress_callback(self, action, migration=None, fake=False):
        if self.verbosity >= 1:
            compute_time = self.verbosity > 1
            if action == "apply_start":
                if compute_time:
                    self.start = time.monotonic()
                self.stdout.write("  Applying %s..." % migration, ending="")
                self.stdout.flush()
            elif action == "apply_success":
                elapsed = " (%.3fs)" % (time.monotonic() - self.start) if compute_time else ""
                if fake:
                    self.stdout.write(self.style.SUCCESS(" FAKED" + elapsed))
                else:
                    self.stdout.write(self.style.SUCCESS(" OK" + elapsed))
            elif action == "unapply_start":
                if compute_time:
                    self.start = time.monotonic()
                self.stdout.write("  Unapplying %s..." % migration, ending="")
                self.stdout.flush()
            elif action == "unapply_success":
                elapsed = " (%.3fs)" % (time.monotonic() - self.start) if compute_time else ""
                if fake:
                    self.stdout.write(self.style.SUCCESS(" FAKED" + elapsed))
                else:
                    self.stdout.write(self.style.SUCCESS(" OK" + elapsed))
            elif action == "render_start":
                if compute_time:
                    self.start = time.monotonic()
                self.stdout.write("  Rendering model states...", ending="")
                self.stdout.flush()
            elif action == "render_success":
                elapsed = " (%.3fs)" % (time.monotonic() - self.start) if compute_time else ""
                self.stdout.write(self.style.SUCCESS(" DONE" + elapsed))

    def sync_apps(self, connection, app_labels):
        """Run the old syncdb-style operation on a list of app_labels."""
        # 通过数据库连接获取所有表的名称
        with connection.cursor() as cursor:
            tables = connection.introspection.table_names(cursor)

        # Build the manifest of apps and models that are to be synchronized.
        # 构建需要同步到数据库的app和models的清单
        # 先获取项目中所有app的AppConfig对象，通过该对象检查app下是否存在models.py
        # 以及判断该app是否在要执行同步的app列表中，在的话就添加到all_models里面
        all_models = [
            (
                app_config.label,
                router.get_migratable_models(app_config, connection.alias, include_auto_created=False),
            )
            for app_config in apps.get_app_configs()
            if app_config.models_module is not None and app_config.label in app_labels
        ]
        # TODO:下面的内容后面再看

        def model_installed(model):
            opts = model._meta
            converter = connection.introspection.identifier_converter
            return not (
                (converter(opts.db_table) in tables) or
                (opts.auto_created and converter(opts.auto_created._meta.db_table) in tables)
            )

        manifest = {
            app_name: list(filter(model_installed, model_list))
            for app_name, model_list in all_models
        }

        # Create the tables for each model
        if self.verbosity >= 1:
            self.stdout.write('  Creating tables...')
        with connection.schema_editor() as editor:
            for app_name, model_list in manifest.items():
                for model in model_list:
                    # Never install unmanaged models, etc.
                    if not model._meta.can_migrate(connection):
                        continue
                    if self.verbosity >= 3:
                        self.stdout.write(
                            '    Processing %s.%s model' % (app_name, model._meta.object_name)
                        )
                    if self.verbosity >= 1:
                        self.stdout.write('    Creating table %s' % model._meta.db_table)
                    editor.create_model(model)

            # Deferred SQL is executed when exiting the editor's context.
            if self.verbosity >= 1:
                self.stdout.write('    Running deferred SQL...')

    @staticmethod
    def describe_operation(operation, backwards):
        """Return a string that describes a migration operation for --plan."""
        prefix = ''
        is_error = False
        if hasattr(operation, 'code'):
            code = operation.reverse_code if backwards else operation.code
            action = (code.__doc__ or '') if code else None
        elif hasattr(operation, 'sql'):
            action = operation.reverse_sql if backwards else operation.sql
        else:
            action = ''
            if backwards:
                prefix = 'Undo '
        if action is not None:
            action = str(action).replace('\n', '')
        elif backwards:
            action = 'IRREVERSIBLE'
            is_error = True
        if action:
            action = ' -> ' + action
        truncated = Truncator(action)
        return prefix + operation.describe() + truncated.chars(40), is_error

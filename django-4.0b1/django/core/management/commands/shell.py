import os
import select
import sys
import traceback

from django.core.management import BaseCommand, CommandError
from django.utils.datastructures import OrderedSet


class Command(BaseCommand):
    help = (
        "Runs a Python interactive interpreter. Tries to use IPython or "
        "bpython, if one of them is available. Any standard input is executed "
        "as code."
    )

    requires_system_checks = []
    shells = ['ipython', 'bpython', 'python']

    def add_arguments(self, parser):
        parser.add_argument(
            '--no-startup', action='store_true',
            help='When using plain Python, ignore the PYTHONSTARTUP environment variable and ~/.pythonrc.py script.',
        )
        # 该参数用于指定交互环境使用哪种解释器
        parser.add_argument(
            '-i', '--interface', choices=self.shells,
            help='Specify an interactive interpreter interface. Available options: "ipython", "bpython", and "python"',
        )
        # 使用该参数时，不会运行一个交互shell，而是会将传递进来的命令直接运行
        parser.add_argument(
            '-c', '--command',
            help='Instead of opening an interactive shell, run a command as Django and exit.',
        )

    def ipython(self, options):
        """将ipython作为shell的解释器"""
        from IPython import start_ipython
        start_ipython(argv=[])

    def bpython(self, options):
        import bpython
        bpython.embed()

    def python(self, options):
        import code

        # Set up a dictionary to serve as the environment for the shell.
        imported_objects = {}

        # We want to honor both $PYTHONSTARTUP and .pythonrc.py, so follow system
        # conventions and get $PYTHONSTARTUP first then .pythonrc.py.
        # 尝试获取环境变量PYTHONSTARTUP的值以及通过expanduser获取用户家目录下的pythonrc.py文件
        # 目前尝试过，PYTHONSTARTUP为空，家目录下不存在pythonrc.py文件
        if not options['no_startup']:
            for pythonrc in OrderedSet([os.environ.get("PYTHONSTARTUP"), os.path.expanduser('~/.pythonrc.py')]):
                if not pythonrc:
                    continue
                if not os.path.isfile(pythonrc):
                    continue
                with open(pythonrc) as handle:
                    pythonrc_code = handle.read()
                # Match the behavior of the cpython shell where an error in
                # PYTHONSTARTUP prints an exception and continues.
                try:
                    exec(compile(pythonrc_code, pythonrc, 'exec'), imported_objects)
                except Exception:
                    traceback.print_exc()

        # By default, this will set up readline to do tab completion and to read and
        # write history to the .python_history file, but this can be overridden by
        # $PYTHONSTARTUP or ~/.pythonrc.py.
        # python_history.py会记录交互shell中执行的命令
        # hook的作用可能是使得环境能够使用tab来进行命令补齐
        try:
            hook = sys.__interactivehook__
        except AttributeError:
            # Match the behavior of the cpython shell where a missing
            # sys.__interactivehook__ is ignored.
            pass
        else:
            try:
                hook()
            except Exception:
                # Match the behavior of the cpython shell where an error in
                # sys.__interactivehook__ prints a warning and the exception
                # and continues.
                print('Failed calling sys.__interactivehook__')
                traceback.print_exc()

        # Set up tab completion for objects imported by $PYTHONSTARTUP or
        # ~/.pythonrc.py.
        try:
            import readline
            import rlcompleter
            readline.set_completer(rlcompleter.Completer(imported_objects).complete)
        except ImportError:
            pass

        # Start the interactive interpreter.
        # 通过这个命令来进入交互环境
        # imported_objects是一个字典，当进入shell后可以直接调用这些内容
        # 可以尝试在这个字典中这个键值对"hello": "world"，进入shell后可以直接输出hello的值
        code.interact(local=imported_objects)

    def handle(self, **options):
        # Execute the command and exit.
        if options['command']:
            exec(options['command'], globals())
            return

        # Execute stdin if it has anything to read and exit.
        # Not supported on Windows due to select.select() limitations.
        if sys.platform != 'win32' and not sys.stdin.isatty() and select.select([sys.stdin], [], [], 0)[0]:
            exec(sys.stdin.read(), globals())
            return

        # 检查有哪些终端解释器可以使用，如果有设置interface参数则使用interface参数指定的
        available_shells = [options['interface']] if options['interface'] else self.shells

        # 遍历available_shells，看哪一个解释器可以使用
        for shell in available_shells:
            try:
                # 在当前对象中找到shell变量并执行(也就是找到上面的ipython、bpython等函数)
                return getattr(self, shell)(options)
            except ImportError:
                pass
        raise CommandError("Couldn't import {} interface.".format(shell))

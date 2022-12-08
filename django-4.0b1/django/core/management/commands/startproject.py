from django.core.checks.security.base import SECRET_KEY_INSECURE_PREFIX
from django.core.management.templates import TemplateCommand

from ..utils import get_random_secret_key


class Command(TemplateCommand):
    help = (
        "Creates a Django project directory structure for the given project "
        "name in the current directory or optionally in the given directory."
    )
    missing_args_message = "You must provide a project name."

    def handle(self, **options):
        # 将之前传入的项目名弹出
        project_name = options.pop('name')
        # 尝试获取传入的文件夹路径(用于在指定文件夹中创建项目)
        target = options.pop('directory')

        # 创建项目密钥
        # Create a random SECRET_KEY to put it in the main settings.
        options['secret_key'] = SECRET_KEY_INSECURE_PREFIX + get_random_secret_key()

        super().handle('project', project_name, target, **options)

import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "inti_project.settings")
django.setup()

from django.template import Template, Context

try:
    print(Template('{{ val|make_list|first|default:"A" }}').render(Context({'val': ''})))
    print("Success")
except Exception as e:
    import traceback
    traceback.print_exc()

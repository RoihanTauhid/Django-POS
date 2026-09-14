import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "inti_project.settings")
django.setup()

from django.contrib.auth.models import AnonymousUser

anon = AnonymousUser()
try:
    print(anon.groups.filter(name__in=["Kasir"]).exists())
except Exception as e:
    print("Error:", e)

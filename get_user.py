import os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "inti_project.settings")
django.setup()
from django.contrib.auth.models import User
for u in User.objects.all():
    print(u.username, u.groups.all())

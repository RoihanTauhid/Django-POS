import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "inti_project.settings")
django.setup()

from django.contrib.auth.models import User
user = User.objects.get(username="owner")
user.set_password("owner123")
user.save()
print("Password reset")

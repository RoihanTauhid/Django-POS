from django.core.management.base import BaseCommand
from django.contrib.auth.models import Group

class Command(BaseCommand):
    help = 'Membuat default roles/grup untuk sistem POS'

    def handle(self, *args, **options):
        roles = [
            'Kasir',
            'Supervisor',
            'Admin Gudang',
            'Owner'
        ]

        for role in roles:
            group, created = Group.objects.get_or_create(name=role)
            if created:
                self.stdout.write(self.style.SUCCESS(f'Berhasil membuat role: {role}'))
            else:
                self.stdout.write(self.style.WARNING(f'Role sudah ada: {role}'))
        
        self.stdout.write(self.style.SUCCESS('Selesai mengatur roles!'))

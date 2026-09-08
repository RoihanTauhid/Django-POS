from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import PermissionDenied

def group_required(*group_names):
    """
    Decorator untuk memastikan user berada di salah satu grup yang diizinkan.
    Superuser selalu diizinkan.
    Jika tidak memiliki akses, raise PermissionDenied (403).
    """
    def in_groups(user):
        if user.is_superuser:
            return True
        if bool(user.groups.filter(name__in=group_names)):
            return True
        raise PermissionDenied
        
    return user_passes_test(in_groups)

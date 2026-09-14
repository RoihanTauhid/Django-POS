from django import template

register = template.Library()

@register.filter(name='has_group')
def has_group(user, group_names):
    """
    Check if a user is in a comma-separated list of groups.
    Usage: {% if request.user|has_group:"Kasir,Supervisor" %}
    """
    # Split by comma in case multiple groups are provided
    group_list = [name.strip() for name in group_names.split(',')]
    return user.groups.filter(name__in=group_list).exists()

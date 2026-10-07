from django import template

register = template.Library()


@register.filter
def get_item(mapping, key):
    """dict lookup in templates: {{ selected|get_item:question.pk }}"""
    return mapping.get(key)

from django import template

register = template.Library()


@register.filter
def plural_ru(number, forms):
    one, few, many = forms.split(",")
    number = abs(int(number))
    if 11 <= number % 100 <= 14:
        return many
    if number % 10 == 1:
        return one
    return few if 2 <= number % 10 <= 4 else many

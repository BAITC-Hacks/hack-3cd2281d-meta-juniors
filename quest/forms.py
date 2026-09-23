from django import forms
from django.core.validators import URLValidator


class DevelopmentRequestForm(forms.Form):
    skill = forms.ChoiceField(label="Какой навык хочешь развить")
    note = forms.CharField(
        label="Что поможет тебе в работе",
        required=False,
        max_length=2000,
        widget=forms.Textarea(
            attrs={"rows": 3, "placeholder": "Например: хочу разобрать архитектуру сервиса с наставником."}
        ),
    )

    def __init__(self, *args, gaps, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["skill"].choices = [
            (g["skill_id"], f"{g['name']}: {g['current']} → {g['required']}") for g in gaps
        ]


class PracticePublicationForm(forms.Form):
    title = forms.CharField(label="Название практики", min_length=5, max_length=300)
    instructions = forms.CharField(
        label="Задание и шаги выполнения",
        min_length=80,
        max_length=12000,
        widget=forms.Textarea(attrs={"rows": 10}),
    )
    deliverable = forms.CharField(
        label="Что сотрудник должен прислать",
        min_length=20,
        max_length=3000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    criteria = forms.CharField(
        label="Критерии проверки — каждый с новой строки",
        max_length=4000,
        widget=forms.Textarea(attrs={"rows": 5}),
    )
    duration_hours = forms.FloatField(
        label="Ожидаемая нагрузка, часов", min_value=0.5, max_value=80, initial=4
    )
    gain = forms.IntegerField(label="Прирост после подтверждения", min_value=1, max_value=5, initial=1)
    max_level = forms.IntegerField(label="До какого уровня развивает практика", min_value=1, max_value=5)

    def clean_criteria(self):
        rows = [r.strip() for r in self.cleaned_data["criteria"].splitlines() if r.strip()]
        if (
            not 3 <= len(rows) <= 8
            or len(set(rows)) != len(rows)
            or any(len(r) < 10 or len(r) > 500 for r in rows)
        ):
            raise forms.ValidationError("Укажи 3–8 разных критериев длиной от 10 до 500 символов.")
        return rows


class SubmissionForm(forms.Form):
    previous_attempt = forms.IntegerField(min_value=0, widget=forms.HiddenInput, initial=0)
    body = forms.CharField(
        label="Твой результат и принятые решения",
        min_length=80,
        max_length=20000,
        widget=forms.Textarea(
            attrs={
                "rows": 10,
                "placeholder": "Опиши, что сделано, почему выбрано такое решение и как его проверить.",
            }
        ),
    )
    artifact_url = forms.CharField(
        label="Ссылка на схему, документ или репозиторий",
        required=False,
        max_length=1000,
        validators=[URLValidator(schemes=["https", "http"])],
        widget=forms.URLInput(attrs={"placeholder": "https://…"}),
    )


class ReviewForm(forms.Form):
    decision = forms.ChoiceField(
        label="Решение", choices=[("return", "Вернуть с комментарием"), ("approve", "Подтвердить результат")]
    )
    checked_criteria = forms.MultipleChoiceField(
        label="Подтверждённые критерии", required=False, widget=forms.CheckboxSelectMultiple
    )
    feedback = forms.CharField(
        label="Обратная связь сотруднику",
        min_length=10,
        max_length=5000,
        widget=forms.Textarea(attrs={"rows": 4}),
    )

    def __init__(self, *args, criteria, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["checked_criteria"].choices = [(str(i), text) for i, text in enumerate(criteria)]

    def clean(self):
        data = super().clean()
        if data.get("decision") == "approve" and len(data.get("checked_criteria", [])) != len(
            self.fields["checked_criteria"].choices
        ):
            raise forms.ValidationError(
                "Для подтверждения проверь все критерии. Иначе верни работу на доработку."
            )
        return data

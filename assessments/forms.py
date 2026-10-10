"""
Input validation for lecturers. Forms never carry course, creator, status, score or any
timestamp that the server sets: those come from the URL object and the session.
"""

from django import forms
from django.utils import timezone

from .models import MAX_DURATION_MINUTES, Question

MAX_CHOICES = 5
MIN_CHOICES = 2
_DT_FORMATS = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"]


class TestForm(forms.Form):
    __test__ = False  # not a unittest case, despite the name

    title = forms.CharField(max_length=200)
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), required=False, max_length=5000)
    duration_minutes = forms.IntegerField(min_value=1, max_value=MAX_DURATION_MINUTES, initial=30)
    opens_at = forms.DateTimeField(
        required=False, input_formats=_DT_FORMATS, widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")
    )
    closes_at = forms.DateTimeField(
        required=False, input_formats=_DT_FORMATS, widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")
    )

    def clean_title(self):
        title = self.cleaned_data["title"].strip()
        if not title:
            raise forms.ValidationError("Title is required.")
        return title

    def clean(self):
        cleaned = super().clean()
        opens, closes = cleaned.get("opens_at"), cleaned.get("closes_at")
        if opens and closes and closes <= opens:
            self.add_error("closes_at", "The closing time must be after the opening time.")
        if closes and closes <= timezone.now():
            self.add_error("closes_at", "The closing time must be in the future.")
        return cleaned

    @classmethod
    def from_test(cls, test):
        return cls(
            initial={
                "title": test.title,
                "description": test.description,
                "duration_minutes": test.duration_minutes,
                "opens_at": timezone.localtime(test.opens_at) if test.opens_at else None,
                "closes_at": timezone.localtime(test.closes_at) if test.closes_at else None,
            }
        )


class QuestionForm(forms.Form):
    kind = forms.ChoiceField(choices=Question.Kind.choices, initial=Question.Kind.MCQ, label="Question type", required=False)
    text = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), max_length=2000)
    marks = forms.IntegerField(min_value=1, max_value=100, initial=1)
    marking_guide = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        max_length=2000,
        required=False,
        label="Marking guide (lecturers only)",
        help_text="Not multiple choice only. Shown to markers, never to students.",
    )
    choice_1 = forms.CharField(max_length=500, label="Choice A", required=False)
    choice_2 = forms.CharField(max_length=500, label="Choice B", required=False)
    choice_3 = forms.CharField(max_length=500, label="Choice C", required=False)
    choice_4 = forms.CharField(max_length=500, label="Choice D", required=False)
    choice_5 = forms.CharField(max_length=500, label="Choice E", required=False)
    correct = forms.ChoiceField(
        choices=[(str(i), chr(64 + i)) for i in range(1, MAX_CHOICES + 1)],
        widget=forms.RadioSelect,
        label="Correct choice",
        required=False,
    )

    def clean_text(self):
        text = self.cleaned_data["text"].strip()
        if not text:
            raise forms.ValidationError("Question text is required.")
        return text

    def clean_marking_guide(self):
        return self.cleaned_data["marking_guide"].strip()

    def clean(self):
        cleaned = super().clean()
        cleaned["kind"] = cleaned.get("kind") or Question.Kind.MCQ
        if cleaned["kind"] != Question.Kind.MCQ:
            # Choices make no sense for these kinds; ignore anything submitted.
            cleaned["choices"], cleaned["correct"] = [], None
            return cleaned
        cleaned["marking_guide"] = ""
        texts = []  # (position, text) for non-blank choices, positions kept so the key matches the letter
        for position in range(1, MAX_CHOICES + 1):
            value = (cleaned.get(f"choice_{position}") or "").strip()
            if value:
                texts.append((position, value))
        if len(texts) < MIN_CHOICES:
            raise forms.ValidationError(f"Provide at least {MIN_CHOICES} choices.")
        lowered = [t.lower() for _, t in texts]
        if len(set(lowered)) != len(lowered):
            raise forms.ValidationError("Choices must all be different.")
        correct = cleaned.get("correct")
        if not correct:
            self.add_error("correct", "Select the correct choice.")
        else:
            positions = [p for p, _ in texts]
            if int(correct) not in positions:
                self.add_error("correct", "The correct choice must be one of the choices you filled in.")
            else:
                # Re-number contiguously; the correct index follows its text.
                cleaned["choices"] = [t for _, t in texts]
                cleaned["correct"] = positions.index(int(correct)) + 1
        return cleaned

    @classmethod
    def from_question(cls, question):
        initial = {
            "kind": question.kind,
            "text": question.text,
            "marks": question.marks,
            "marking_guide": question.marking_guide,
        }
        for choice in question.choices.all():
            initial[f"choice_{choice.order}"] = choice.text
            if choice.is_correct:
                initial["correct"] = str(choice.order)
        return cls(initial=initial)

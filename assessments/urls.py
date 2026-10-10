from django.urls import path

from . import views

app_name = "assessments"

urlpatterns = [
    # Lecturer / admin authoring
    path("courses/<int:course_pk>/tests/new/", views.create_test, name="test_create"),
    path("tests/<int:pk>/manage/", views.test_manage, name="test_manage"),
    path("tests/<int:pk>/edit/", views.test_edit, name="test_edit"),
    path("tests/<int:pk>/questions/add/", views.question_add, name="question_add"),
    path("tests/<int:pk>/questions/<int:question_pk>/edit/", views.question_edit, name="question_edit"),
    path("tests/<int:pk>/questions/<int:question_pk>/delete/", views.question_delete, name="question_delete"),
    path("tests/<int:pk>/publish/", views.test_publish, name="test_publish"),
    path("tests/<int:pk>/archive/", views.test_archive, name="test_archive"),
    path("tests/<int:pk>/submissions/", views.submissions, name="submissions"),
    path("tests/<int:pk>/submissions/<int:attempt_pk>/", views.grade, name="grade"),
    # Student
    path("tests/<int:pk>/", views.test_intro, name="test_intro"),
    path("tests/<int:pk>/start/", views.attempt_start, name="attempt_start"),
    path("tests/<int:test_pk>/attempts/<int:pk>/", views.attempt_detail, name="attempt_detail"),
    path("tests/<int:test_pk>/attempts/<int:pk>/save/", views.attempt_save, name="attempt_save"),
    path("tests/<int:test_pk>/attempts/<int:pk>/submit/", views.attempt_submit, name="attempt_submit"),
    path("tests/<int:test_pk>/attempts/<int:pk>/files/<int:answer_pk>/", views.answer_file, name="answer_file"),
]

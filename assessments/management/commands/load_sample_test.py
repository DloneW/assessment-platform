"""
Load a ready-made sample test (60 marks) into an existing course.

    python manage.py load_sample_test                       # course titled "Introduction to Cybersecurity and Strategy"
    python manage.py load_sample_test --course-code CYB101  # or name the course by code
    python manage.py load_sample_test --publish             # also publish it (otherwise it stays a draft)

Idempotent: if the course already has a test with the sample title, nothing is added.
The test is created by a lecturer assigned to the course (or --lecturer USERNAME).
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import Course
from assessments import services
from assessments.models import Question, Test

DEFAULT_COURSE_TITLE = "Introduction to Cybersecurity and Strategy"
TEST_TITLE = "Introduction to Cybersecurity and Strategy: Mid-Semester Assessment"
MCQ, SHORT, FILE = Question.Kind.MCQ, Question.Kind.SHORT, Question.Kind.FILE

# (kind, marks, text, choices, correct 1-based, marking guide)
QUESTIONS = [
    (MCQ, 2, "In the CIA triad, what does the 'I' stand for?",
     ["Integrity", "Identity", "Intrusion", "Isolation"], 1, ""),
    (MCQ, 2, "Which of these is the best example of a phishing attack?",
     ["An email that imitates your bank and asks you to confirm your password on a fake page",
      "Flooding a website with traffic until it stops responding",
      "Guessing passwords by trying every combination",
      "Installing a security update on your phone"], 1, ""),
    (MCQ, 2, "Multi-factor authentication improves security mainly because it:",
     ["Requires more than one kind of proof of identity",
      "Makes passwords impossible to guess",
      "Encrypts all network traffic",
      "Removes the need for any password policy"], 1, ""),
    (MCQ, 2, "The principle of least privilege says that users should:",
     ["Have only the access they need to do their job",
      "Share one administrator account to keep things simple",
      "Receive full access once they pass a background check",
      "Change their password every day"], 1, ""),
    (MCQ, 2, "Ransomware is malicious software that:",
     ["Encrypts a victim's files and demands payment to restore them",
      "Speeds up a slow computer",
      "Blocks advertising on websites",
      "Backs up files to the cloud"], 1, ""),
    (MCQ, 2, "In risk management, risk is best described as the combination of:",
     ["Likelihood and impact", "Cost and time", "Hardware and software", "Policy and training"], 1, ""),
    (MCQ, 2, "Which control is a *detective* control rather than a preventive one?",
     ["Reviewing security logs for unusual sign-ins", "A firewall rule that blocks a port",
      "A locked server room door", "Mandatory password length"], 1, ""),
    (MCQ, 2, "'Defence in depth' means:",
     ["Using several layers of protection so one failure is not fatal",
      "Buying the most expensive firewall available",
      "Hiding the network from the internet",
      "Relying on a single strong password"], 1, ""),
    (SHORT, 6,
     "Explain the difference between a vulnerability, a threat and a risk. Use a simple example to show how the three relate.",
     None, None,
     "2 marks each term defined correctly. Look for: vulnerability = a weakness; threat = something that could exploit it; "
     "risk = chance of loss when both exist. Award marks for a coherent worked example."),
    (SHORT, 6,
     "Why is a cybersecurity strategy about more than technology? Give at least two non-technical elements an organisation needs.",
     None, None,
     "Accept people/training, policy and governance, incident response planning, leadership buy-in, budget, supplier risk. "
     "2 marks for the main argument, 2 marks per well-explained element (max 4)."),
    (SHORT, 5,
     "Describe three steps an organisation should take immediately after discovering a data breach.",
     None, None,
     "Accept: contain/isolate affected systems, preserve evidence, assess scope, notify the right people "
     "(management, regulators, affected users), recover, review lessons. About 1.5 marks per sensible step, 5 maximum."),
    (SHORT, 5,
     "Give two reasons why employees are often called the 'weakest link' in security, and one way to reduce that risk.",
     None, None,
     "2 marks per reason (e.g. social engineering, weak passwords, careless data handling, shadow IT); 1 mark for "
     "a realistic mitigation (training, MFA, least privilege, clear reporting channel)."),
    (FILE, 8,
     "Upload a clear diagram (image) of a simple network for a small business that shows at least a firewall, "
     "a separate guest Wi-Fi and a server. Mark where you would place two security controls.",
     None, None,
     "2 marks for a readable diagram with firewall, guest Wi-Fi segment and server; 2 marks for correct placement; "
     "2 marks per justified control placement (max 4) - total capped at 8."),
    (FILE, 14,
     "Upload a document (PDF or Word, about one to two pages) proposing a short cybersecurity strategy for a university "
     "department: name the top three risks, the controls you would use, and how you would measure success.",
     None, None,
     "4 marks: three realistic risks identified and prioritised. 5 marks: controls match the risks and mix "
     "technical and non-technical measures. 3 marks: measurable success indicators. 2 marks: clarity and structure."),
]


class Command(BaseCommand):
    help = "Load a 60-mark sample test (multiple choice, short answer and file upload) into a course."

    def add_arguments(self, parser):
        parser.add_argument("--course-code", help="Course code. Default: the course titled "
                            f'"{DEFAULT_COURSE_TITLE}".')
        parser.add_argument("--lecturer", help="Username of the lecturer who will own the test "
                            "(default: a lecturer assigned to the course).")
        parser.add_argument("--duration", type=int, default=60, help="Minutes allowed (default 60).")
        parser.add_argument("--publish", action="store_true", help="Publish the test after loading it.")

    def handle(self, *args, **opts):
        course = self._course(opts["course_code"])
        creator = self._creator(course, opts["lecturer"])
        if Test.objects.filter(course=course, title=TEST_TITLE).exists():
            self.stdout.write(f"{course.code} already has the sample test; nothing added.")
            return
        total = sum(q[1] for q in QUESTIONS)
        with transaction.atomic():
            test = services.create_test(
                course=course,
                creator=creator,
                cleaned={
                    "title": TEST_TITLE,
                    "description": "Multiple choice questions are marked automatically. Short-answer and file-upload "
                    "questions are marked by your lecturer, so your score may go up after you submit.",
                    "duration_minutes": opts["duration"],
                },
            )
            for kind, marks, text, choices, correct, guide in QUESTIONS:
                services.create_question(
                    test,
                    {"kind": kind, "text": text, "marks": marks, "marking_guide": guide,
                     "choices": choices, "correct": correct},
                )
            if opts["publish"]:
                services.publish_test(test)
        state = "published" if opts["publish"] else "saved as a draft (publish it from the test page)"
        self.stdout.write(self.style.SUCCESS(
            f"Added {len(QUESTIONS)} questions worth {total} marks to {course.code} - {course.title}; {state}."
        ))

    def _course(self, code):
        qs = Course.objects.all()
        if code:
            course = qs.filter(code__iexact=code.strip()).first()
        else:
            matches = list(qs.filter(title__iexact=DEFAULT_COURSE_TITLE)[:2])
            if len(matches) > 1:
                raise CommandError("More than one course has that title; pass --course-code.")
            course = matches[0] if matches else None
        if course is None:
            raise CommandError('Course not found. Create it in the Administration Console first, or pass --course-code.')
        return course

    def _creator(self, course, username):
        lecturers = course.lecturers.filter(groups__name="LECTURER")
        if username:
            user = lecturers.filter(username=username).first()
            if user is None:
                raise CommandError(f"{username} is not a lecturer assigned to {course.code}.")
            return user
        user = lecturers.order_by("pk").first()
        if user is None:
            raise CommandError(f"{course.code} has no assigned lecturer. Assign one in the Administration Console.")
        return user

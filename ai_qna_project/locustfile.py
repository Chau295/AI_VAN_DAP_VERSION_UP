from locust import HttpUser, task, between
import re


class StudentExamUser(HttpUser):
    wait_time = between(1, 3)

    username = "SV2024141"
    password = "123456"
    subject_code = "DS001"
    session_id = 1

    def get_csrf_token(self, response):
        match = re.search(
            r'name="csrfmiddlewaretoken" value="(.+?)"',
            response.text
        )
        return match.group(1) if match else ""

    def on_start(self):
        response = self.client.get("/accounts/login/")
        csrf_token = self.get_csrf_token(response)

        self.client.post(
            "/accounts/login/",
            data={
                "username": self.username,
                "password": self.password,
                "csrfmiddlewaretoken": csrf_token,
            },
            headers={
                "Referer": self.host + "/accounts/login/"
            }
        )

    @task(3)
    def open_exam_page(self):
        self.client.get(f"/student/exam_page/{self.subject_code}/")

    @task(5)
    def heartbeat(self):
        csrf_token = self.client.cookies.get("csrftoken", "")

        self.client.post(
            f"/student/exam/{self.session_id}/heartbeat/",
            headers={
                "X-CSRFToken": csrf_token,
                "Referer": self.host + f"/student/exam_page/{self.subject_code}/",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
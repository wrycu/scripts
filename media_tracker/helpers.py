import httpx
from bs4 import BeautifulSoup
from configparser import ConfigParser
from datetime import datetime
from slack_sdk import WebClient

class YamTracker:
    def __init__(self, username, password, url):
        self.username = username
        self.password = password
        self.url = url
        self.csrf_token = None
        self.csrf_middleware_token = None
        self.session_id = None
        self.headers = {}
        self.cookies = {}

    @staticmethod
    def extract_csrf_middleware_token(reply_text):
        soup = BeautifulSoup(reply_text, "html.parser")
        return soup.find("input", {"name": "csrfmiddlewaretoken"}).get("value")

    def login(self):
        # get initial CSRF token
        reply = httpx.get(f"{self.url}/accounts/login/")
        self.csrf_token = reply.cookies["csrftoken"]
        self.csrf_middleware_token = self.extract_csrf_middleware_token(reply.text)

        reply = httpx.post(
            f"{self.url}/accounts/login/",
            data={
                "csrfmiddlewaretoken": self.csrf_middleware_token,
                "login": self.username,
                "password": self.password,
                "next": "/",
            },
            cookies={
                "csrftoken": self.csrf_token,
            },
            headers={
                "Referer": f"{self.url}/accounts/login/"
            },
        )
        self.session_id = reply.cookies["sessionid"]
        self.cookies = {
            "csrftoken": self.csrf_token,
            "sessionid": self.session_id,
        }

        reply = httpx.get(
            f"{self.url}/",
            cookies=self.cookies,
        )
        self.csrf_middleware_token = self.extract_csrf_middleware_token(reply.text)

        print(reply.status_code)

    def rate_media(self, media_id, media_type, rating):
        reply = httpx.post(
            f"{self.url}/media_save",
            data={
                "csrfmiddlewaretoken": self.csrf_middleware_token,
                "media_type": media_type,
                "source": "tmdb",
                "media_id": media_id,
                "score": int(rating) * 2,
                "repeats": 0,
                "status": "Completed",
                "start_date": str(datetime.today().date()),
                "end_date": str(datetime.today().date()),
                "notes": "rating submitted via Slack",
            },
            cookies=self.cookies,
            headers={
                "Referer": f"{self.url}/"
            },
            #follow_redirects=True,
        )


def rate_media(media_id, media_type, rating):
    config_obj = ConfigParser()
    config_obj.read("../config.ini")
    conf = {
        "user": config_obj.get("media_tracker", "username"),
        "password": config_obj.get("media_tracker", "password"),
        "URL": config_obj.get("media_tracker", "URL"),
    }
    tracker = YamTracker(
        username=conf['user'],
        password=conf['password'],
        url=conf['URL'],
    )
    tracker.login()
    tracker.rate_media(media_id, media_type, rating)

def setup_tracker(conf):
    tracker = YamTracker(
        username=conf['user'],
        password=conf['password'],
        url=conf['URL'],
    )
    tracker.login()
    # top gun
    #tracker.rate_media(361743, "movie" 5)
    # 3 body problem
    tracker.rate_media(108545, "tv", 4)

def extract_media_from_event(event_json):
    if event_json['Event'] != "MarkPlayed":
        print("Disregarding non-play event")
        return
    if event_json['Item']['Type'] == "Movie":
        media_type = "movie"
        media_id = event_json['Item']['ProviderIds']['Tmdb']
        media_name = event_json['Item']['Name']
    elif "Series" in event_json.keys():
        media_type = "series"
        media_id = event_json['Series']['ProviderIds']['Tmdb']
        media_name = event_json['Series']['Name']
    else:
        print("Single episode or other unknown watch event")
        return
    return {
        "type": media_type,
        "id": media_id,
        "name": media_name,
    }

def query_user(media_id, media_type, media_name):
    # load the config
    config_obj = ConfigParser()
    config_obj.read("../config.ini")
    conf = {
        "user": config_obj.get("media_tracker", "username"),
        "password": config_obj.get("media_tracker", "password"),
        "URL": config_obj.get("media_tracker", "URL"),
        "slack_client_secret": config_obj.get("media_tracker", "slack_bot_user_oauth_token"),
        "slack_user_id": config_obj.get("media_tracker", "slack_user_id"),
    }

    client = WebClient(conf['slack_client_secret'])
    response = client.conversations_open(users=conf['slack_user_id'])
    dm_channel = response.data["channel"]["id"]

    question = f"I see you finished {media_name}. How'd you like it?"
    buttons = [
        {
            "type": "button",
            "text": {
                "type": "plain_text",
                "text": "1",
                "emoji": True
            },
            "style": "danger",
            "value": f"1.{media_id}.{media_type}"
        },
        {
            "type": "button",
            "text": {
                "type": "plain_text",
                "text": "2",
                "emoji": True
            },
            "value": f"2.{media_id}.{media_type}"
        },
        {
            "type": "button",
            "text": {
                "type": "plain_text",
                "text": "3",
                "emoji": True
            },
            "value": f"3.{media_id}.{media_type}"
        },
        {
            "type": "button",
            "text": {
                "type": "plain_text",
                "text": "4",
                "emoji": True
            },
            "value": f"4.{media_id}.{media_type}"
        },
        {
            "type": "button",
            "text": {
                "type": "plain_text",
                "text": "5",
                "emoji": True
            },
            "style": "primary",
            "value": f"5.{media_id}.{media_type}",
        }
    ]

    client.chat_postMessage(
        channel=dm_channel,
        text=question,
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": question
                }
            },
            {
                "type": "actions",
                "block_id": "question_block",
                "elements": buttons
            }
        ]
    )

def ack_rating():
    # load the config
    config_obj = ConfigParser()
    config_obj.read("../config.ini")
    conf = {
        "user": config_obj.get("media_tracker", "username"),
        "password": config_obj.get("media_tracker", "password"),
        "URL": config_obj.get("media_tracker", "URL"),
        "slack_client_secret": config_obj.get("media_tracker", "slack_bot_user_oauth_token"),
        "slack_user_id": config_obj.get("media_tracker", "slack_user_id"),
    }

    client = WebClient(conf['slack_client_secret'])
    response = client.conversations_open(users=conf['slack_user_id'])
    dm_channel = response.data["channel"]["id"]
    content = "Thanks! I've saved your rating."
    client.chat_postMessage(
        channel=dm_channel,
        text=content,
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": content
                }
            },
        ]
    )

from flask import Flask, Response, request
from slack_sdk.signature import SignatureVerifier
from configparser import ConfigParser
import json
import helpers

app = Flask(__name__)


@app.route('/slack', methods=['POST'])
def capture_response():
    config_obj = ConfigParser()
    config_obj.read("../config.ini")
    signature_verifier = SignatureVerifier(
        signing_secret=config_obj.get("media_tracker", "slack_signing_secret"),
    )
    print(request.headers)
    if not signature_verifier.is_valid_request(
        body=request.get_data(),
        headers=dict(request.headers),
    ):
        return Response("Invalid signature", status=401)

    resp_payload = json.loads(request.form['payload'])
    for action in resp_payload['actions']:
        value = action['value']
        rating = value.split('.')[0]
        media_id = value.split('.')[1]
        media_type = value.split('.')[2]
        helpers.rate_media(media_id, media_type, rating)
        helpers.ack_rating()
        break
    return Response("Ok", 200)

@app.route("/watch", methods=['POST'])
def capture_watch():
    event_info = helpers.extract_media_from_event(request.json)
    helpers.query_user(
        event_info['id'],
        event_info['type'],
        event_info['name'],
    )
    return Response("Ok", 200)


if __name__ == '__main__':
    # swap to 0.0.0.0 if testing the incoming Slack webhook portion
    app.run(host="127.0.0.1", port=5190)

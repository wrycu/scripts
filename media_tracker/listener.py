from flask import Flask, Response, jsonify, request
from slack_sdk.signature import SignatureVerifier
import json
import logging

import helpers
import navidrome

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)


def _dispatch_action(value):
    """Route a Slack button click to the right backend.

    Button values are namespaced by source:
        jellyfin.<rating>.<media_id>.<media_type>
        navidrome.<like|dislike>.<event_id>
    """
    parts = value.split('.')
    source = parts[0]

    if source == navidrome.SOURCE:
        rating, event_id = parts[1], parts[2]
        navidrome.handle_rating(rating, event_id)
    elif source == "jellyfin":
        rating, media_id, media_type = parts[1], parts[2], parts[3]
        helpers.rate_media(media_id, media_type, rating)
        helpers.ack_rating()
    elif source.isdigit():
        # Prompts sent before button values were namespaced: <rating>.<id>.<type>.
        rating, media_id, media_type = parts[0], parts[1], parts[2]
        helpers.rate_media(media_id, media_type, rating)
        helpers.ack_rating()
    else:
        logger.warning("unroutable button value %r", value)


@app.route('/slack', methods=['POST'])
def capture_response():
    config_obj = helpers.load_config()
    signature_verifier = SignatureVerifier(
        signing_secret=config_obj.get("media_tracker", "slack_signing_secret"),
    )
    if not signature_verifier.is_valid_request(
        body=request.get_data(),
        headers=dict(request.headers),
    ):
        return Response("Invalid signature", status=401)

    resp_payload = json.loads(request.form['payload'])
    for action in resp_payload['actions']:
        _dispatch_action(action['value'])
        break
    return Response("Ok", 200)


@app.route("/watch", methods=['POST'])
def capture_watch():
    print("caught jellyfin watch event")
    event_info = helpers.extract_media_from_event(request.json)
    if event_info is None:
        # Unfinished playback, or an episode/type we don't track.
        return Response("Ignored", 200)
    helpers.query_user(
        event_info['id'],
        event_info['type'],
        event_info['name'],
    )
    return Response("Ok", 200)


@app.route("/scrobble", methods=['POST'])
def capture_scrobble():
    """Completed play forwarded by the Navidrome plugin."""
    if not navidrome.secret_is_valid(request.headers.get("X-Plugin-Secret", "")):
        return Response("Invalid secret", status=401)

    body, status = navidrome.handle_scrobble(request.get_json(silent=True) or {})
    return jsonify(body), status


if __name__ == '__main__':
    # swap to 0.0.0.0 if testing the incoming Slack webhook portion
    app.run(host="127.0.0.1", port=5190)

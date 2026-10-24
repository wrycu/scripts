from flask import Flask, Response, jsonify, request
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web import WebClient
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


def _on_socket_request(client: SocketModeClient, req: SocketModeRequest):
    """Handle a button click delivered over the Slack Socket Mode websocket."""
    if req.type != "interactive":
        return
    # Ack before doing the work: Slack wants it within 3 seconds, and rating
    # media can involve a slow round-trip to the backend.
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
    for action in req.payload.get('actions', []):
        try:
            _dispatch_action(action['value'])
        except Exception:
            logger.exception("failed to handle button value %r", action.get('value'))
        break


def start_socket_mode():
    """Open the Socket Mode connection. Non-blocking; reconnects on its own."""
    config_obj = helpers.load_config()
    client = SocketModeClient(
        app_token=config_obj.get("media_tracker", "slack_app_token"),
        web_client=WebClient(config_obj.get("media_tracker", "slack_bot_user_oauth_token")),
    )
    client.socket_mode_request_listeners.append(_on_socket_request)
    client.connect()
    return client


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
    # Slack clicks arrive over Socket Mode; Flask only serves /watch and /scrobble.
    socket_client = start_socket_mode()
    app.run(host="127.0.0.1", port=5190)

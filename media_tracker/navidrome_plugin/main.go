// Package main implements a Navidrome Scrobbler plugin that forwards completed
// plays to the like/dislike backend over HTTP. It is deliberately thin: the
// interactive Slack round-trip and persistence live in the external service,
// because a WASM plugin is sandboxed and stateless per call.
package main

import (
	"encoding/json"
	"fmt"
	"time"

	"github.com/navidrome/navidrome/plugins/pdk/go/host"
	"github.com/navidrome/navidrome/plugins/pdk/go/pdk"
	"github.com/navidrome/navidrome/plugins/pdk/go/scrobbler"
)

// Config keys read from Navidrome's plugin configuration.
const (
	cfgBackendURL = "backend_url"   // e.g. http://127.0.0.1:8080/events/scrobble
	cfgSecret     = "shared_secret" // must match the backend's PLUGIN_SHARED_SECRET
)

// event is the JSON payload POSTed to the backend. Field names match what
// backend/app.py reads from the request body.
type event struct {
	User            string `json:"user"`
	TrackID         string `json:"track_id"`
	Title           string `json:"title"`
	Artist          string `json:"artist"`
	Album           string `json:"album"`
	Timestamp       string `json:"timestamp"`
	PlaylistContext string `json:"playlist_context,omitempty"`
}

type likeDislikePlugin struct{}

func init() { scrobbler.Register(likeDislikePlugin{}) }

func main() {}

// IsAuthorized: accept every assigned user. Which users the plugin sees is
// controlled by Navidrome's per-plugin user assignment (the `users` permission).
func (likeDislikePlugin) IsAuthorized(scrobbler.IsAuthorizedRequest) (bool, error) {
	return true, nil
}

// NowPlaying / PlaybackReport are not interesting to us — we only prompt once a
// play has actually completed (Scrobble).
func (likeDislikePlugin) NowPlaying(scrobbler.NowPlayingRequest) error { return nil }

func (likeDislikePlugin) PlaybackReport(scrobbler.PlaybackReportRequest) error { return nil }

// Scrobble fires when a play completes. We forward it to the backend and always
// return nil so Navidrome does not retry on backend/Slack hiccups.
func (likeDislikePlugin) Scrobble(req scrobbler.ScrobbleRequest) error {
	backendURL, ok := host.ConfigGet(cfgBackendURL)
	if !ok || backendURL == "" {
		pdk.Log(pdk.LogWarn, "like-dislike: "+cfgBackendURL+" not configured; skipping")
		return nil
	}
	secret, _ := host.ConfigGet(cfgSecret)

	payload := event{
		User:            req.Username,
		TrackID:         req.Track.ID,
		Title:           req.Track.Title,
		Artist:          req.Track.Artist,
		Album:           req.Track.Album,
		Timestamp:       time.Unix(req.Timestamp, 0).UTC().Format(time.RFC3339),
		PlaylistContext: playlistContext(),
	}

	body, err := json.Marshal(payload)
	if err != nil {
		pdk.Log(pdk.LogError, "like-dislike: failed to marshal payload: "+err.Error())
		return nil
	}

	resp, err := host.HTTPSend(host.HTTPRequest{
		Method: "POST",
		URL:    backendURL,
		Headers: map[string]string{
			"Content-Type":   "application/json",
			"X-Plugin-Secret": secret,
		},
		Body:      body,
		TimeoutMs: 5000,
	})
	if err != nil {
		pdk.Log(pdk.LogError, "like-dislike: backend request failed: "+err.Error())
		return nil
	}
	if resp.StatusCode >= 300 {
		pdk.Log(pdk.LogWarn, fmt.Sprintf("like-dislike: backend returned %d", resp.StatusCode))
	}
	return nil
}

// playlistContext is a best-effort hint about where the play came from. Navidrome
// does not record the originating playlist, so we look at the user's saved play
// queue via the Subsonic API. Any failure yields an empty string.
func playlistContext() string {
	raw, err := host.SubsonicAPICall("getPlayQueue")
	if err != nil || raw == "" {
		return ""
	}
	var env struct {
		SR struct {
			PlayQueue struct {
				Entry []struct {
					ID string `json:"id"`
				} `json:"entry"`
			} `json:"playQueue"`
		} `json:"subsonic-response"`
	}
	if err := json.Unmarshal([]byte(raw), &env); err != nil {
		return ""
	}
	n := len(env.SR.PlayQueue.Entry)
	if n == 0 {
		return ""
	}
	return fmt.Sprintf("Play queue (%d tracks)", n)
}

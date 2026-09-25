from locust import HttpUser, task, between
import random
import time
from urllib.parse import urljoin, urlparse


# ============================================================
# Configuration
# ============================================================

VIDEO_URLS = [
    f"/videos/{i}/index.m3u8"
    for i in range(1, 401)
]

INITIAL_BUFFER_SEGMENTS = 3
BUFFER_AHEAD_SEGMENTS = 2

USER_START_DELAY = (0.5, 2.0)


class VideoUser(HttpUser):

    wait_time = between(1, 3)

    # ========================================================
    # Extract hostname / streaming node
    # ========================================================

    def get_node_name(self, url):

        try:
            hostname = urlparse(url).hostname

            if hostname:
                return hostname

        except Exception:
            pass

        return "unknown-node"

    # ========================================================
    # Playlist
    # ========================================================

    def get_playlist(self, url):

        response = self.client.get(
            url,
            name="/playlist",
            allow_redirects=True,
        )

        if not response.ok:
            return None

        return response

    # ========================================================
    # HLS playlist parser
    # ========================================================

    def parse_playlist(self, playlist_text):

        segments = []

        duration = None

        for line in playlist_text.splitlines():

            line = line.strip()

            if not line:
                continue

            if line.startswith("#EXTINF:"):

                try:
                    value = line.split(":", 1)[1]
                    duration = float(
                        value.split(",", 1)[0]
                    )

                except (ValueError, IndexError):
                    duration = None

            elif not line.startswith("#"):

                segments.append(
                    {
                        "url": line,
                        "duration": duration or 0,
                    }
                )

                duration = None

        return segments

    # ========================================================
    # Download segment
    # ========================================================

    def download_segment(
        self,
        segment_url,
        index,
        node_name,
    ):

        response = self.client.get(
            segment_url,

            # Example:
            #
            # segment | streaming-node-01
            #
            name=f"/segment | {node_name}",
        )

        if not response.ok:
            return False

        return True

    # ========================================================
    # Watch video
    # ========================================================

    @task
    def watch_video(self):

        # ----------------------------------------------------
        # Select random video
        # ----------------------------------------------------

        playlist_url = random.choice(
            VIDEO_URLS
        )

        time.sleep(
            random.uniform(
                USER_START_DELAY[0],
                USER_START_DELAY[1],
            )
        )

        # ----------------------------------------------------
        # Request playlist
        # ----------------------------------------------------

        playlist_response = self.get_playlist(
            playlist_url
        )

        if playlist_response is None:
            return

        # ----------------------------------------------------
        # Get FINAL redirected URL
        # ----------------------------------------------------

        redirected_playlist_url = (
            playlist_response.url
        )

        # ----------------------------------------------------
        # Identify streaming node
        # ----------------------------------------------------

        node_name = self.get_node_name(
            redirected_playlist_url
        )

        # ----------------------------------------------------
        # Parse playlist
        # ----------------------------------------------------

        segments = self.parse_playlist(
            playlist_response.text
        )

        if not segments:
            return

        # ----------------------------------------------------
        # Convert segment URLs
        # ----------------------------------------------------

        for segment in segments:

            segment["url"] = urljoin(
                redirected_playlist_url,
                segment["url"],
            )

        # ----------------------------------------------------
        # Buffer
        # ----------------------------------------------------

        buffer_seconds = 0
        segment_index = 0

        # ====================================================
        # INITIAL BUFFER
        # ====================================================

        initial_count = min(
            INITIAL_BUFFER_SEGMENTS,
            len(segments),
        )

        for _ in range(initial_count):

            segment = segments[
                segment_index
            ]

            success = self.download_segment(
                segment["url"],
                segment_index,
                node_name,
            )

            if not success:
                return

            buffer_seconds += (
                segment["duration"]
            )

            segment_index += 1

        # ====================================================
        # PLAYBACK
        # ====================================================

        while segment_index < len(segments):

            # ------------------------------------------------
            # Consume playback buffer
            # ------------------------------------------------

            if buffer_seconds > 0:

                playback_time = min(
                    buffer_seconds,
                    1.0,
                )

                time.sleep(
                    playback_time
                )

                buffer_seconds -= (
                    playback_time
                )

            # ------------------------------------------------
            # Keep buffer ahead
            # ------------------------------------------------

            while (
                segment_index < len(segments)
                and buffer_seconds
                < self.get_buffer_target(
                    segments,
                    segment_index,
                )
            ):

                segment = segments[
                    segment_index
                ]

                success = self.download_segment(
                    segment["url"],
                    segment_index,
                    node_name,
                )

                if not success:
                    return

                buffer_seconds += (
                    segment["duration"]
                )

                segment_index += 1

            # ------------------------------------------------
            # Rebuffering
            # ------------------------------------------------

            if buffer_seconds <= 0:

                if segment_index >= len(segments):
                    break

                segment = segments[
                    segment_index
                ]

                success = self.download_segment(
                    segment["url"],
                    segment_index,
                    node_name,
                )

                if not success:
                    return

                buffer_seconds += (
                    segment["duration"]
                )

                segment_index += 1

    # ========================================================
    # Buffer target
    # ========================================================

    def get_buffer_target(
        self,
        segments,
        current_index,
    ):

        end_index = min(
            current_index + BUFFER_AHEAD_SEGMENTS,
            len(segments),
        )

        target = 0

        for i in range(
            current_index,
            end_index,
        ):

            target += segments[i][
                "duration"
            ]

        return max(target, 1)

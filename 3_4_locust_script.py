from locust import HttpUser, task, between
import random
from urllib.parse import urljoin

VIDEO_URLS = [f"/videos/{i}/index.m3u8" for i in range(1, 401)]


class VideoUser(HttpUser):
    wait_time = between(0.5, 1)

    def get_playlist(self, url):
        response = self.client.get(
            url,
            allow_redirects=True,
            # name="/playlist"
        )

        # Do not process the response body if the request failed
        if not response.ok:
            return None

        return response

    def parse_playlist(self, playlist_text):
        segments = []
        lines = playlist_text.splitlines()

        duration = None

        for line in lines:
            if line.startswith("#EXTINF"):
                duration = float(line.split(":")[1].replace(",", ""))

            elif line and not line.startswith("#"):
                segments.append((line.strip(), duration))

        return segments

    @task
    def watch_video(self):

        # Request playlist from MAIN server
        playlist_url = random.choice(VIDEO_URLS)

        playlist_response = self.get_playlist(playlist_url)

        # Stop if playlist request failed
        if playlist_response is None:
            return

        # FINAL redirected URL
        redirected_playlist_url = playlist_response.url

        segments = self.parse_playlist(playlist_response.text)

        if not segments:
            return

        # Download TS files from redirected host
        for segment, duration in segments:

            segment_url = urljoin(
                redirected_playlist_url,
                segment
            )

            response = self.client.get(
                segment_url,
                # name="/segment"
            )

            # Optional: stop watching if a segment fails
            if not response.ok:
                break

            if duration:
                self.wait_time = lambda: duration
                self.wait()

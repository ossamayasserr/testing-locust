from locust import HttpUser, task, between, events
import random
import time
import threading
from collections import deque
from urllib.parse import urljoin


# ============================================================
# CONFIGURATION
# ============================================================

# ------------------------------------------------------------
# Object Storages
# ------------------------------------------------------------
#
# IMPORTANT:
# Every storage has its own hostname.
#
# All storages should contain the same videos:
#
#   /1/index.m3u8
#   /2/index.m3u8
#   ...
#   /400/index.m3u8
#
# Example:
#
# agent48:
# https://hel1.your-objectstorage.com/agent48/
#
# agent49:
# https://fsn1.your-objectstorage.com/agent49/
#
# Change these to your real endpoints.
#
# Do NOT put credentials in this file.
# ------------------------------------------------------------

# OBJECT_STORAGES = {
#     "agent47": "https://hel1.your-objectstorage.com/agent47/",
#     "agent48": "https://fsn1.your-objectstorage.com/agent48/",
#     "agent49": "https://nbg1.your-objectstorage.com/agent49/",
#     "agent50": "https://fsn1.your-objectstorage.com/agent50/",
#     "agent51": "https://fsn1.your-objectstorage.com/agent51/",
# }
OBJECT_STORAGES = {
    "ManagerVM": "http://62.238.43.236:8000/videos/",
}


# ------------------------------------------------------------
# Videos
# ------------------------------------------------------------

VIDEO_COUNT = 400


# ------------------------------------------------------------
# HLS buffering
# ------------------------------------------------------------

INITIAL_BUFFER_SEGMENTS = 3
BUFFER_AHEAD_SEGMENTS = 2


# ------------------------------------------------------------
# User startup delay
# ------------------------------------------------------------

USER_START_DELAY = (0.5, 2.0)


# ------------------------------------------------------------
# Locust user wait time
# ------------------------------------------------------------

WAIT_TIME = (1, 3)


# ============================================================
# ADAPTIVE ROUTING CONFIGURATION
# ============================================================

# How often the storage weights are recalculated.

WEIGHT_UPDATE_INTERVAL = 10


# Number of recent successful requests used to calculate P95.

LATENCY_WINDOW = 100


# Minimum percentage of traffic any storage can receive.
#
# Example:
#
# 0.05 = at least 5% of users go to every storage.
#
# This prevents a slow storage from being completely
# abandoned.

MIN_STORAGE_SHARE = 0.05


# Initial latency before enough measurements exist.

INITIAL_LATENCY_MS = 300.0


# Target latency.

TARGET_LATENCY_MS = 1000.0


# ============================================================
# RANDOM SEED
# ============================================================
#
# Set to an integer if you want the same video-selection
# sequence every time a single Locust worker starts.
#
# Example:
#
# RANDOM_SEED = 12345
#
# Set to None for normal random behavior.
#
# NOTE:
# With multiple Locust workers, each worker has its own
# random generator. Therefore the combined global sequence
# will not be identical between distributed runs.
# ============================================================

RANDOM_SEED = None


# ============================================================
# GLOBAL STORAGE STATE
# ============================================================

storage_state = {}

state_lock = threading.Lock()


def initialize_storage_state():

    for storage_name in OBJECT_STORAGES:

        storage_state[storage_name] = {
            "latencies": deque(
                maxlen=LATENCY_WINDOW
            ),

            "latency_ema": INITIAL_LATENCY_MS,

            "p95": INITIAL_LATENCY_MS,

            "weight": 1.0,

            "requests": 0,

            "failures": 0,

            "last_weight_update": 0,
        }


initialize_storage_state()


# ============================================================
# PERCENTILE
# ============================================================

def calculate_percentile(values, percentile):

    if not values:
        return INITIAL_LATENCY_MS

    sorted_values = sorted(values)

    index = int(
        len(sorted_values) * percentile
    )

    index = min(
        index,
        len(sorted_values) - 1,
    )

    return sorted_values[index]


# ============================================================
# RECORD STORAGE RESULT
# ============================================================

def record_storage_result(
    storage_name,
    response_time_ms,
    success,
):

    with state_lock:

        state = storage_state[
            storage_name
        ]

        state["requests"] += 1

        if not success:

            state["failures"] += 1

            return

        # ----------------------------------------------------
        # Keep recent latency samples
        # ----------------------------------------------------

        state["latencies"].append(
            response_time_ms
        )

        # ----------------------------------------------------
        # EMA
        # ----------------------------------------------------

        alpha = 0.20

        state["latency_ema"] = (
            (
                1.0 - alpha
            )
            * state["latency_ema"]
            +
            alpha
            * response_time_ms
        )

        # ----------------------------------------------------
        # P95
        # ----------------------------------------------------

        state["p95"] = calculate_percentile(
            state["latencies"],
            0.95,
        )


# ============================================================
# UPDATE ROUTING WEIGHTS
# ============================================================

def update_storage_weights():

    now = time.time()

    with state_lock:

        # ----------------------------------------------------
        # Check whether update is required
        # ----------------------------------------------------

        should_update = False

        for state in storage_state.values():

            if (
                now
                - state["last_weight_update"]
                >= WEIGHT_UPDATE_INTERVAL
            ):

                should_update = True
                break

        if not should_update:

            return


        # ----------------------------------------------------
        # Calculate raw weights
        # ----------------------------------------------------

        raw_weights = {}

        for storage_name, state in (
            storage_state.items()
        ):

            latency = max(
                state["latency_ema"],
                1.0,
            )

            # -----------------------------------------------
            # Faster storage = higher weight.
            #
            # sqrt prevents a very fast storage from
            # receiving practically all traffic.
            # -----------------------------------------------

            weight = (
                1.0
                /
                (latency ** 0.5)
            )

            # -----------------------------------------------
            # Stronger penalty once latency exceeds 1 sec.
            # -----------------------------------------------

            if latency > TARGET_LATENCY_MS:

                penalty = (
                    TARGET_LATENCY_MS
                    /
                    latency
                )

                weight *= (
                    penalty ** 3
                )

            raw_weights[
                storage_name
            ] = weight


        # ----------------------------------------------------
        # Normalize
        # ----------------------------------------------------

        total_weight = sum(
            raw_weights.values()
        )

        if total_weight <= 0:

            return


        normalized = {}

        for storage_name, weight in (
            raw_weights.items()
        ):

            normalized[
                storage_name
            ] = (
                weight
                /
                total_weight
            )


        # ----------------------------------------------------
        # Minimum traffic share
        # ----------------------------------------------------

        storage_count = len(
            normalized
        )

        minimum_total = (
            storage_count
            * MIN_STORAGE_SHARE
        )

        if minimum_total >= 1.0:

            equal_share = (
                1.0
                /
                storage_count
            )

            for storage_name in normalized:

                normalized[
                    storage_name
                ] = equal_share

        else:

            remaining = (
                1.0
                - minimum_total
            )

            for storage_name in normalized:

                normalized[
                    storage_name
                ] = (
                    MIN_STORAGE_SHARE
                    +
                    normalized[
                        storage_name
                    ]
                    * remaining
                )


        # ----------------------------------------------------
        # Save weights
        # ----------------------------------------------------

        for storage_name, weight in (
            normalized.items()
        ):

            storage_state[
                storage_name
            ]["weight"] = weight

            storage_state[
                storage_name
            ]["last_weight_update"] = now


# ============================================================
# CHOOSE STORAGE
# ============================================================

def choose_storage():

    update_storage_weights()

    with state_lock:

        names = list(
            storage_state.keys()
        )

        weights = [
            storage_state[name]["weight"]
            for name in names
        ]

    return random.choices(
        names,
        weights=weights,
        k=1,
    )[0]


# ============================================================
# PRINT STORAGE STATUS
# ============================================================

def print_storage_status():

    while True:

        time.sleep(
            WEIGHT_UPDATE_INTERVAL
        )

        update_storage_weights()

        with state_lock:

            print()
            print(
                "=" * 90
            )

            print(
                "OBJECT STORAGE ADAPTIVE ROUTING"
            )

            print(
                "=" * 90
            )

            print(
                f"{'Storage':<15}"
                f"{'EMA':>12}"
                f"{'P95':>12}"
                f"{'Weight':>12}"
                f"{'Requests':>12}"
                f"{'Failures':>12}"
            )

            print(
                "-" * 90
            )

            for (
                storage_name,
                state
            ) in storage_state.items():

                print(
                    f"{storage_name:<15}"
                    f"{state['latency_ema']:>9.0f} ms"
                    f"{state['p95']:>9.0f} ms"
                    f"{state['weight'] * 100:>10.1f}%"
                    f"{state['requests']:>12}"
                    f"{state['failures']:>12}"
                )

            print(
                "=" * 90
            )


# ============================================================
# LOCUST TEST START
# ============================================================

@events.test_start.add_listener
def on_test_start(
    environment,
    **kwargs,
):

    if RANDOM_SEED is not None:

        random.seed(
            RANDOM_SEED
        )

    # --------------------------------------------------------
    # Start status thread.
    #
    # We only need one status thread per Locust process.
    # --------------------------------------------------------

    thread = threading.Thread(
        target=print_storage_status,
        daemon=True,
    )

    thread.start()


# ============================================================
# VIDEO USER
# ============================================================

class VideoUser(HttpUser):

    wait_time = between(
        WAIT_TIME[0],
        WAIT_TIME[1],
    )


    # ========================================================
    # Build playlist URL
    # ========================================================

    def build_playlist_url(
        self,
        storage_name,
    ):

        base_url = OBJECT_STORAGES[
            storage_name
        ]

        video_id = random.randint(
            1,
            VIDEO_COUNT,
        )

        return urljoin(
            base_url,
            f"{video_id}/index.m3u8",
        )


    # ========================================================
    # Playlist
    # ========================================================

    def get_playlist(
        self,
        url,
        storage_name,
    ):

        start = time.perf_counter()

        response = self.client.get(
            url,

            name=(
                f"/playlist | "
                f"{storage_name}"
            ),

            allow_redirects=True,
        )

        elapsed_ms = (
            time.perf_counter()
            - start
        ) * 1000


        # ----------------------------------------------------
        # Record playlist statistics.
        #
        # We record them for reporting but they DO NOT
        # influence adaptive storage routing.
        # ----------------------------------------------------

        with state_lock:

            state = storage_state[
                storage_name
            ]

            state["requests"] += 1

            if not response.ok:

                state["failures"] += 1


        if not response.ok:

            return None


        return response


    # ========================================================
    # Parse HLS playlist
    # ========================================================

    def parse_playlist(
        self,
        playlist_text,
    ):

        segments = []

        duration = None

        for line in (
            playlist_text.splitlines()
        ):

            line = line.strip()

            if not line:

                continue


            # ------------------------------------------------
            # EXTINF
            # ------------------------------------------------

            if line.startswith(
                "#EXTINF:"
            ):

                try:

                    value = (
                        line
                        .split(
                            ":",
                            1,
                        )[1]
                    )

                    duration = float(
                        value
                        .split(
                            ",",
                            1,
                        )[0]
                    )

                except (
                    ValueError,
                    IndexError,
                ):

                    duration = None


            # ------------------------------------------------
            # Segment
            # ------------------------------------------------

            elif not line.startswith(
                "#"
            ):

                segments.append(
                    {
                        "url": line,
                        "duration":
                            duration or 0,
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
        segment_index,
        storage_name,
    ):

        start = time.perf_counter()

        response = self.client.get(
            segment_url,

            name=(
                f"/segment | "
                f"{storage_name}"
            ),
        )

        elapsed_ms = (
            time.perf_counter()
            - start
        ) * 1000


        # ----------------------------------------------------
        # IMPORTANT:
        #
        # Segment latency is what controls adaptive routing.
        # ----------------------------------------------------

        record_storage_result(
            storage_name,
            elapsed_ms,
            response.ok,
        )


        return response.ok


    # ========================================================
    # Watch Video
    # ========================================================

    @task
    def watch_video(self):

        # ----------------------------------------------------
        # Select Object Storage
        # ----------------------------------------------------

        storage_name = choose_storage()


        # ----------------------------------------------------
        # Select Video
        # ----------------------------------------------------

        playlist_url = (
            self.build_playlist_url(
                storage_name
            )
        )


        # ----------------------------------------------------
        # Simulate user startup delay
        # ----------------------------------------------------

        time.sleep(
            random.uniform(
                USER_START_DELAY[0],
                USER_START_DELAY[1],
            )
        )


        # ----------------------------------------------------
        # Request Playlist
        # ----------------------------------------------------

        playlist_response = (
            self.get_playlist(
                playlist_url,
                storage_name,
            )
        )


        if playlist_response is None:

            return


        # ----------------------------------------------------
        # Get final URL after redirect
        # ----------------------------------------------------

        redirected_playlist_url = (
            playlist_response.url
        )


        # ----------------------------------------------------
        # Parse playlist
        # ----------------------------------------------------

        segments = (
            self.parse_playlist(
                playlist_response.text
            )
        )


        if not segments:

            return


        # ----------------------------------------------------
        # Convert relative URLs to absolute URLs
        # ----------------------------------------------------

        for segment in segments:

            segment["url"] = urljoin(
                redirected_playlist_url,
                segment["url"],
            )


        # ----------------------------------------------------
        # Playback buffer
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


        for _ in range(
            initial_count
        ):

            segment = segments[
                segment_index
            ]


            success = (
                self.download_segment(
                    segment["url"],
                    segment_index,
                    storage_name,
                )
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

        while (
            segment_index
            < len(segments)
        ):

            # ------------------------------------------------
            # Consume buffer
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
                segment_index
                < len(segments)

                and

                buffer_seconds
                <
                self.get_buffer_target(
                    segments,
                    segment_index,
                )
            ):

                segment = segments[
                    segment_index
                ]


                success = (
                    self.download_segment(
                        segment["url"],
                        segment_index,
                        storage_name,
                    )
                )


                if not success:

                    return


                buffer_seconds += (
                    segment["duration"]
                )

                segment_index += 1


            # ------------------------------------------------
            # Rebuffer
            # ------------------------------------------------

            if buffer_seconds <= 0:

                if (
                    segment_index
                    >= len(segments)
                ):

                    break


                segment = segments[
                    segment_index
                ]


                success = (
                    self.download_segment(
                        segment["url"],
                        segment_index,
                        storage_name,
                    )
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
            current_index
            + BUFFER_AHEAD_SEGMENTS,
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

        return max(
            target,
            1,
        )

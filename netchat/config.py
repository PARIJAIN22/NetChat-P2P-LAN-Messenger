"""
config.py - every tunable number of NetChat lives here.

Keeping them in one place means we never have "magic numbers" scattered
around the code, and it is easy to explain / change them in the viva.
"""

# ---- Ports -----------------------------------------------------------
DEFAULT_TCP_PORT = 5000      # chat messages travel over TCP on this port
DEFAULT_UDP_PORT = 5001      # discovery (HELLO / REPLY / BYE) uses UDP on this port

# ---- Timing (in seconds) ---------------------------------------------
HEARTBEAT_INTERVAL = 10      # how often we re-broadcast HELLO ("I am still here")
PEER_TIMEOUT = 30            # forget a peer we have not heard from for this long
SOCKET_TIMEOUT = 1.0         # recv/accept wake up this often so threads can notice "stop"
CONNECT_TIMEOUT = 3.0        # give up connecting / sending to a peer after this long
BYE_MAX_AGE = 600            # a signed BYE older than this (seconds) is ignored (replay; allows clock skew)
STOP_TIMEOUT = 3.0           # stop() waits at most this long in total for threads, so closing never hangs

# ---- Sizes -----------------------------------------------------------
BUFFER_SIZE = 4096           # bytes we ask recv() for at a time
MAX_LINE_BYTES = 65536       # a single message line may not be bigger than this
MAX_TEXT_LEN = 1000          # longest chat message the user may type (characters)
MAX_NAME_LEN = 20            # longest username

# ---- Reliability (TCP layer) ------------------------------------------
HANDSHAKE_TIMEOUT = 5.0      # a new TCP connection must finish its HI exchange within this time
SEND_TIMEOUT = 30.0          # sendall() gives up after this long (a stuck peer must not hang us forever)
ACK_TIMEOUT = 10.0           # no ACK for a message after this long -> shown as "failed" (Retry)
PING_INTERVAL = 15           # RTT measurement: PING every peer this often
TYPING_THROTTLE = 2.0        # send at most one TYPING frame per peer in this time
TYPING_EXPIRE = 3.0          # "X is typing..." disappears after this long without a new TYPING frame
RECONNECT_DELAYS = (0.5, 1, 2, 4, 8)   # pauses before each reconnection attempt after a dropped connection
SEEN_IDS_KEEP = 5000         # how many message ids we remember to drop duplicates (retries)

# ---- Files -------------------------------------------------------------
FILE_CHUNK_SIZE = 64 * 1024  # file data travels in chunks of this size (flow control + progress)
DOWNLOAD_SUBDIR = ("Downloads", "NetChat")   # inside the user's home folder
PROGRESS_INTERVAL = 0.1      # progress events at most every 100 ms
PERSIST_INTERVAL = 1.0       # save transfer progress to the database at most once a second
MAX_CHUNK_RETRIES = 3        # a transfer fails after this many damaged chunks
RESUME_DELAYS = (0.5, 1, 2, 4, 8)      # pauses before each attempt to resume an interrupted transfer

# ---- Files on disk -----------------------------------------------------
DB_FILE = "netchat.db"       # chat history + offline queue (sqlite3)
SETTINGS_FILE = "settings.json"

# ---- Network addresses -----------------------------------------------
BROADCAST_ADDRESS = "255.255.255.255"   # "everybody on this LAN"
LOOPBACK_ADDRESS = "127.0.0.1"          # "this same computer" (used as a fallback)

# ---- Logging ---------------------------------------------------------
LOG_FILE = "netchat.log"

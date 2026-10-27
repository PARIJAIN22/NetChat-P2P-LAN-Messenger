"""
gui_common.py - small helpers and constants shared by gui.py and its mixin modules.
"""

EMPTY_PEERS_TEXT = "No peers found yet. Waiting for others on this network..."

NOTICE_SECONDS = 6          # how long a temporary message stays in the status bar

FLASH_MS = 900              # speed of the blinking window title

MIN_CHAT_HEIGHT = 260       # opening the log grows the window until the chat has this much room

HISTORY_LIMIT = 500         # messages loaded when a chat is opened

def user_chat(name):
    """Chat key of a one-to-one chat. (Group chats are 'g:<id>'.)"""
    return "u:" + name

def group_chat(group_id):
    return "g:" + group_id

EVERYONE = {"id": "everyone", "name": "Everyone", "members": []}

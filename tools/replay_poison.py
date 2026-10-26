"""Moves the messages of the poison queue back to the event queue, so the app reads them again.

Run it after the cause is fixed (the database or Blob was down for days, a missing role, ...). It needs the same
settings as the app (AZURE_STORAGE_ACCOUNT_URL or AZURE_STORAGE_CONNECTION_STRING, and EVENT_QUEUE_NAME) and your
own sign-in (az login) with the role Storage Queue Data Contributor on both queues:

    uv run python tools/replay_poison.py
"""
import os
import sys

from azure.identity import DefaultAzureCredential
from azure.storage.queue import QueueClient


def main() -> int:
    name = os.environ.get("EVENT_QUEUE_NAME")
    connection = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    blob_url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
    if not name or not (connection or blob_url):
        print("Set EVENT_QUEUE_NAME and AZURE_STORAGE_ACCOUNT_URL (or the connection string).", file=sys.stderr)
        return 1
    if connection:
        poison = QueueClient.from_connection_string(connection, f"{name}-poison")
        queue = QueueClient.from_connection_string(connection, name)
    else:
        url, credential = blob_url.replace(".blob.", ".queue."), DefaultAzureCredential()
        poison = QueueClient(url, f"{name}-poison", credential=credential)
        queue = QueueClient(url, name, credential=credential)
    moved = 0
    for message in poison.receive_messages(messages_per_page=10, visibility_timeout=60):
        queue.send_message(message.content)        # copy first, then delete: a crash in between only repeats a message
        poison.delete_message(message)
        moved += 1
    print(f"moved {moved} message(s) back to {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

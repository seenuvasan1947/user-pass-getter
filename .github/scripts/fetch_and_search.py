import os
import shutil
import struct
import subprocess
import tempfile
from base64 import urlsafe_b64encode
from pathlib import Path

from pyrogram import Client
from pyrogram.raw.functions.messages import GetHistory
from pyrogram.raw.types import InputPeerChat

DATA_GROUP_ID = -5259911981
RESULTS_GROUP_ID = -1003951031545
MAX_FILES_PER_TYPE = 2
TMP_DIR = Path(tempfile.gettempdir())


def get_search_target():
    target = Path("search-target.txt").read_text().strip()
    if not target:
        raise ValueError("search-target.txt is empty")
    return target


def check_disk_space(required_bytes, path="/tmp"):
    free = shutil.disk_usage(path).free
    return free - required_bytes > 500 * 1024 * 1024


def classify_file(file_name):
    ext = Path(file_name).suffix.lower()
    if ext == ".zip":
        return "zip"
    elif ext == ".7z":
        return "7z"
    elif ext == ".rar":
        return "rar"
    elif ext in (".txt", ".csv", ".json", ".log"):
        return "text"
    return None


def search_file(file_path, search_string, file_type):
    matches = []
    try:
        if file_type == "zip":
            result = subprocess.run(
                ["zipgrep", search_string, str(file_path)],
                capture_output=True, text=True, timeout=300
            )
            if result.stdout.strip():
                matches = result.stdout.strip().splitlines()
        elif file_type == "7z":
            proc = subprocess.run(
                f'7z e -so "{file_path}" 2>/dev/null | grep -n "{search_string}"',
                shell=True, capture_output=True, text=True, timeout=300
            )
            if proc.stdout.strip():
                matches = proc.stdout.strip().splitlines()
        elif file_type == "rar":
            proc = subprocess.run(
                f'unrar p -inul "{file_path}" 2>/dev/null | grep -n "{search_string}"',
                shell=True, capture_output=True, text=True, timeout=300
            )
            if proc.stdout.strip():
                matches = proc.stdout.strip().splitlines()
        elif file_type == "text":
            result = subprocess.run(
                ["grep", "-n", search_string, str(file_path)],
                capture_output=True, text=True, timeout=300
            )
            if result.stdout.strip():
                matches = result.stdout.strip().splitlines()
    except subprocess.TimeoutExpired:
        matches = [f"[TIMEOUT] Search timed out for {file_path.name}"]
    except Exception as e:
        matches = [f"[ERROR] {e}"]
    return matches


def extract_doc_info(raw_msg):
    """Extract file_name, file_size, and raw document from a raw Message."""
    if not hasattr(raw_msg, 'media') or raw_msg.media is None:
        return None, None, None
    doc = getattr(raw_msg.media, 'document', None)
    if doc is None:
        return None, None, None
    file_name = None
    for attr in doc.attributes:
        name = getattr(attr, 'file_name', None)
        if name:
            file_name = name
            break
    return file_name, doc.size, doc


async def download_raw_document(app, doc, file_name, download_path):
    """Download a file using raw document info via Pyrogram's internal downloader."""
    from pyrogram.raw.types import InputDocumentFileLocation

    location = InputDocumentFileLocation(
        id=doc.id,
        access_hash=doc.access_hash,
        file_reference=doc.file_reference,
        thumb_size=""
    )

    # Use Pyrogram's internal get_file method
    r = await app.handle_download(
        (location, doc.size, file_name, str(download_path), None, None)
    )
    return r


async def main():
    search_string = get_search_target()
    print(f"Searching for: {search_string}")

    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    session_string = os.environ["TELEGRAM_SESSION"]

    app = Client("searcher", api_id=api_id, api_hash=api_hash,
                 session_string=session_string, no_updates=True)

    results_file = TMP_DIR / "search_results.txt"
    total_matches = 0
    type_counts = {"zip": 0, "7z": 0, "rar": 0, "text": 0}

    peer = InputPeerChat(chat_id=abs(DATA_GROUP_ID))

    print("Connecting to Telegram...")
    async with app:
        me = await app.get_me()
        print(f"Logged in as: {me.first_name} (id: {me.id})")
        # Test access
        print(f"Testing access to data group {DATA_GROUP_ID}...")
        try:
            test = await app.invoke(
                GetHistory(peer=peer, offset_id=0, offset_date=0, add_offset=0,
                           limit=1, max_id=0, min_id=0, hash=0)
            )
            msg_count = getattr(test, 'count', len(test.messages))
            print(f"Access OK. Messages in group: {msg_count}")
        except Exception as e:
            print(f"ERROR: Cannot access data group: {e}")
            return

        with open(results_file, "w") as rf:
            rf.write(f"Search target: {search_string}\n")
            rf.write("=" * 60 + "\n\n")

            offset_id = 0
            done = False
            while not done:
                raw_history = await app.invoke(
                    GetHistory(
                        peer=peer,
                        offset_id=offset_id, offset_date=0, add_offset=0,
                        limit=50, max_id=0, min_id=0, hash=0
                    )
                )
                raw_messages = raw_history.messages
                if not raw_messages:
                    break

                for raw_msg in raw_messages:
                    offset_id = raw_msg.id

                    file_name, file_size, doc = extract_doc_info(raw_msg)
                    if not file_name or not doc:
                        continue

                    file_type = classify_file(file_name)
                    if file_type is None:
                        continue

                    if type_counts[file_type] >= MAX_FILES_PER_TYPE:
                        if all(c >= MAX_FILES_PER_TYPE for c in type_counts.values()):
                            done = True
                            break
                        continue

                    file_size = file_size or 0
                    print(f"Processing: {file_name} ({file_size / 1024 / 1024:.1f} MB) [{file_type}]")

                    if not check_disk_space(file_size):
                        print(f"  Skipping: not enough disk space")
                        rf.write(f"[SKIPPED] {file_name}: insufficient disk space\n")
                        continue

                    download_path = TMP_DIR / file_name
                    try:
                        # Download using raw document location
                        from pyrogram.raw.types import InputDocumentFileLocation
                        from pyrogram.raw.functions.upload import GetFile as RawGetFile

                        location = InputDocumentFileLocation(
                            id=doc.id,
                            access_hash=doc.access_hash,
                            file_reference=doc.file_reference,
                            thumb_size=""
                        )

                        # Download in chunks
                        offset = 0
                        chunk_size = 1024 * 1024  # 1MB chunks
                        with open(download_path, "wb") as f:
                            while True:
                                chunk = await app.invoke(
                                    RawGetFile(
                                        location=location,
                                        offset=offset,
                                        limit=chunk_size
                                    )
                                )
                                if not chunk.bytes:
                                    break
                                f.write(chunk.bytes)
                                offset += len(chunk.bytes)
                                if len(chunk.bytes) < chunk_size:
                                    break

                        print(f"  Downloaded {offset / 1024 / 1024:.1f} MB")
                        type_counts[file_type] += 1

                        matches = search_file(download_path, search_string, file_type)

                        if matches:
                            rf.write(f"File: {file_name}\n")
                            rf.write(f"Type: {file_type} | Size: {file_size / 1024 / 1024:.1f} MB\n")
                            rf.write(f"Matches: {len(matches)}\n")
                            for line in matches[:100]:
                                rf.write(f"  {line}\n")
                            rf.write("\n")
                            total_matches += len(matches)
                            print(f"  Found {len(matches)} matches")
                        else:
                            print(f"  No matches")

                    except Exception as e:
                        print(f"  Error processing {file_name}: {e}")
                    finally:
                        if download_path.exists():
                            download_path.unlink()
                            print(f"  Cleaned up {file_name}")

            rf.write(f"\n{'=' * 60}\n")
            rf.write(f"Total matches: {total_matches}\n")
            rf.write(f"Files processed: {type_counts}\n")

        # Send results
        print(f"\nSending results to Telegram ({total_matches} total matches)...")
        try:
            if results_file.stat().st_size > 0:
                await app.send_document(
                    RESULTS_GROUP_ID,
                    document=str(results_file),
                    caption=f"Search results for: {search_string}\nTotal matches: {total_matches}"
                )
            else:
                await app.send_message(
                    RESULTS_GROUP_ID,
                    f"No matches found for: {search_string}"
                )
        except Exception as e:
            print(f"Failed to send results: {e}")
            print("Results:")
            print(results_file.read_text())

    print("Done!")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

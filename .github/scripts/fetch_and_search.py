import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from pyrogram import Client

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
    # Keep 500MB buffer
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
    """Search inside a file and return matching lines."""
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


async def main():
    search_string = get_search_target()
    print(f"Searching for: {search_string}")

    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    session_string = os.environ["TELEGRAM_SESSION"]

    app = Client("searcher", api_id=api_id, api_hash=api_hash,
                 session_string=session_string)

    results_file = TMP_DIR / "search_results.txt"
    total_matches = 0
    type_counts = {"zip": 0, "7z": 0, "rar": 0, "text": 0}

    async with app:
        with open(results_file, "w") as rf:
            rf.write(f"Search target: {search_string}\n")
            rf.write("=" * 60 + "\n\n")

            async for message in app.get_chat_history(DATA_GROUP_ID):
                if not message.document:
                    continue

                file_name = message.document.file_name or "unknown"
                file_size = message.document.file_size or 0
                file_type = classify_file(file_name)

                if file_type is None:
                    continue

                if type_counts[file_type] >= MAX_FILES_PER_TYPE:
                    continue

                # Check if we've processed enough files
                if all(c >= MAX_FILES_PER_TYPE for c in type_counts.values()):
                    break

                print(f"Processing: {file_name} ({file_size / 1024 / 1024:.1f} MB) [{file_type}]")

                if not check_disk_space(file_size):
                    print(f"  Skipping {file_name}: not enough disk space")
                    rf.write(f"[SKIPPED] {file_name}: insufficient disk space\n")
                    continue

                download_path = TMP_DIR / file_name
                try:
                    await message.download(file_name=str(download_path))
                    type_counts[file_type] += 1

                    matches = search_file(download_path, search_string, file_type)

                    if matches:
                        rf.write(f"File: {file_name}\n")
                        rf.write(f"Type: {file_type} | Size: {file_size / 1024 / 1024:.1f} MB\n")
                        rf.write(f"Matches: {len(matches)}\n")
                        for line in matches[:100]:  # cap per file
                            rf.write(f"  {line}\n")
                        rf.write("\n")
                        total_matches += len(matches)
                        print(f"  Found {len(matches)} matches")
                    else:
                        print(f"  No matches")

                finally:
                    if download_path.exists():
                        download_path.unlink()
                        print(f"  Cleaned up {file_name}")

            rf.write(f"\n{'=' * 60}\n")
            rf.write(f"Total matches: {total_matches}\n")
            rf.write(f"Files processed: {type_counts}\n")

        # Send results
        print(f"\nSending results to Telegram ({total_matches} total matches)...")
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

    print("Done!")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

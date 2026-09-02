import os
import sqlite3
import json
import logging
import tempfile

logger = logging.getLogger('COOK')

def extract_and_save_cookies(config):
    """
    Extracts specified cookies from a Firefox SQLite database and saves them to a JSON file.

    Reads configuration for the database path, target JSON path, and cookie names.
    Copies the database to a temporary location to avoid locking issues.
    Connects to the temporary database, queries for the specified cookies
    associated with the '.tiktok.com' host, and writes the found cookies
    to the target JSON file. Cleans up the temporary database file afterwards.

    Args:
        config (dict): A dictionary containing the configuration:
                       'firefox_cookie_db_path' (str): Full path to cookies.sqlite.
                       'target_cookie_json_path' (str): Path to save cookies.json.
                       'cookies_to_extract' (list): List of cookie names (str).

    Returns:
        bool: True if cookies were extracted and saved successfully, False otherwise.
    """
    firefox_path_raw = config.get('firefox_cookie_db_path')
    sqlite_path = firefox_path_raw
    json_path = config.get('target_cookie_json_path')
    cookies_to_extract = config.get('cookies_to_extract', [])

    if not all([sqlite_path, json_path, cookies_to_extract]):
        logger.error("Cookie extraction configuration is incomplete. Missing db path, json path, or cookies list.")
        return False

    if not os.path.exists(sqlite_path):
        logger.error(f"Firefox cookie database not found at: {sqlite_path}")
        return False

    conn = None
    extracted_cookies = {}
    success = False

    try:
        with tempfile.TemporaryDirectory(prefix="tklivetracker-cookies-") as temp_dir:
            temp_sqlite_path = os.path.join(temp_dir, "cookies.sqlite")
            fd = os.open(temp_sqlite_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)

            logger.info("Creating a private snapshot of the Firefox cookie database")
            source = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
            try:
                destination = sqlite3.connect(temp_sqlite_path)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            finally:
                source.close()

            conn = sqlite3.connect(temp_sqlite_path)
            cursor = conn.cursor()

            logger.info(f"Attempting to extract cookies: {', '.join(cookies_to_extract)}")
            for cookie_name in cookies_to_extract:
            # Query for the cookie, ordering by lastAccessed to get the most recent one
            # if multiple entries exist (though LIMIT 1 makes this less critical).
                cursor.execute("""
                    SELECT value FROM moz_cookies
                    WHERE name = ? AND host = '.tiktok.com' AND path = '/'
                    ORDER BY lastAccessed DESC
                    LIMIT 1
                """, (cookie_name,))
                result = cursor.fetchone()
                if result:
                    extracted_cookies[cookie_name] = result[0]
                    logger.debug(f"Successfully extracted cookie: {cookie_name}")
                else:
                    logger.warning(f"Cookie '{cookie_name}' not found for host '.tiktok.com'.")

            conn.close()
            conn = None

        if not extracted_cookies:
             logger.warning("No specified cookies were found in the database.")
             # Still proceed to write an empty JSON if the file doesn't exist,
             # or overwrite if it does, to reflect the state.

        logger.info(f"Attempting to write extracted cookies to: {json_path}")
        target_path = os.path.abspath(json_path)
        target_dir = os.path.dirname(target_path)
        os.makedirs(target_dir, exist_ok=True)
        output_fd, temporary_json_path = tempfile.mkstemp(
            prefix=".cookies-", suffix=".tmp", dir=target_dir
        )
        try:
            os.fchmod(output_fd, 0o600)
            with os.fdopen(output_fd, 'w', encoding='utf-8') as output_file:
                json.dump(extracted_cookies, output_file, indent=2)
                output_file.flush()
                os.fsync(output_file.fileno())
            os.replace(temporary_json_path, target_path)
            os.chmod(target_path, 0o600)
        except Exception:
            try:
                os.close(output_fd)
            except OSError:
                pass
            try:
                os.unlink(temporary_json_path)
            except FileNotFoundError:
                pass
            raise
        logger.info(f"Cookies successfully written to {json_path}")
        success = True

    except sqlite3.Error as db_err:
        logger.exception(f"Database error during cookie extraction: {db_err}")
    except IOError as io_err:
        logger.exception(f"File I/O error during cookie extraction or saving: {io_err}")
    except Exception as e:
        logger.exception(f"An unexpected error occurred during cookie extraction: {e}")
    finally:
        if conn:
            try:
                conn.close()
                logger.debug("Closed connection to temporary cookie database.")
            except sqlite3.Error as close_err:
                 logger.error(f"Error closing temporary database connection: {close_err}")

    return success

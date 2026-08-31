import email
import imaplib
import logging
import math
import os
import random
import re
import secrets
import signal
import smtplib
import time
from datetime import datetime, timedelta
from email.message import EmailMessage

import httpx
import pytz

logging.basicConfig(
    level=os.environ.get('LOGLEVEL', 'INFO').upper(),
    format='%(asctime)s %(levelname)s %(message)s'
)
logger = logging.getLogger(__name__)

CONFIG = {
    "ua_str": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 OPR/116.0.0.0",
    "login_url": "https://onlinebusiness.icbc.com/deas-api/v1/webLogin/webLogin",
    "appointments_url": "https://onlinebusiness.icbc.com/deas-api/v1/web/getAvailableAppointments",
    "lock_url": "https://onlinebusiness.icbc.com/deas-api/v1/web/lock",
    "send_otp_url": "https://onlinebusiness.icbc.com/deas-api/v1/web/sendOTP",
    "verify_otp_url": "https://onlinebusiness.icbc.com/deas-api/v1/web/verifyOTP",
    "book_url": "https://onlinebusiness.icbc.com/deas-api/v1/web/book",

    "credentials": {
        "drvrLastName": os.getenv("USER_LAST_NAME"),
        "licenceNumber": os.getenv("USER_LICENSE_NUMBER"),
        "keyword": os.getenv("USER_KEYWORD")
    },

    "appointment_request_base": {
        "examType": "7-R-1",
        "examDate": datetime.now(pytz.timezone("America/Vancouver")).strftime("%Y-%m-%d"),
        "prfDaysOfWeek": "[0,1,2,3,4,5,6]",
        "prfPartsOfDay": "[0,1]",
        "lastName": os.getenv("USER_LAST_NAME"),
        "licenseNumber": os.getenv("USER_LICENSE_NUMBER")
    },

    # Wayburne
    "location_ids": [274],

    "gmail": {
        "email": os.getenv("USER_GMAIL"),
        "password": os.getenv("USER_GMAIL_APP_PASSWORD"),
        "imap_server": "imap.gmail.com",
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 465
    },

    "desired_date_range": {
        "start": os.getenv("DESIRED_DATE_START", "2025-06-24"),
        "end": os.getenv("DESIRED_DATE_END", "2025-06-30")
    },

    "timezone": "America/Vancouver",
    "check_interval": 5,
    "token_refresh_interval": 1500,
    "approval_timeout": int(os.getenv("APPROVAL_TIMEOUT", "300")),
    "approval_poll_interval": 10,
    "action": os.getenv("ACTION", "look")  # Default action is "look"
}

current_token = None
last_token_refresh = None
drvr_id = None
shutdown_requested = False


def handle_shutdown(signum, frame):
    global shutdown_requested
    signal_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
    logger.info(f"Received {signal_name}, shutting down gracefully...")
    shutdown_requested = True


def sleep_with_shutdown(seconds):
    if seconds <= 0:
        return

    end_time = time.monotonic() + seconds
    while not shutdown_requested:
        remaining = end_time - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1, remaining))


def validate_config():
    """Validate that all required environment variables are set"""
    required_vars = [
        "USER_LAST_NAME",
        "USER_LICENSE_NUMBER", 
        "USER_KEYWORD",
        "USER_GMAIL",
        "USER_GMAIL_APP_PASSWORD"
    ]
    
    optional_vars = ["DESIRED_DATE_START", "DESIRED_DATE_END"]
    
    missing_vars = []
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        logger.error(f"Error: Missing required environment variables: {', '.join(missing_vars)}")
        logger.info("Please set these variables in your .env file or environment")
        return False
    
    return True


def refresh_token():
    global current_token, last_token_refresh, drvr_id
    try:
        with httpx.Client() as client:
            logger.info(f"Refreshing token... {CONFIG['credentials']['drvrLastName']} {CONFIG['credentials']['licenceNumber']}")
            response = client.put(
                CONFIG["login_url"],
                json=CONFIG["credentials"],
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": CONFIG["ua_str"]
                }
            )
            response.raise_for_status()

            auth_header = response.headers.get('Authorization')
            if auth_header and auth_header.startswith('Bearer '):
                current_token = auth_header
                last_token_refresh = datetime.now(pytz.timezone(CONFIG['timezone']))

                try:
                    login_data = response.json()
                    drvr_id = login_data.get('drvrId')
                    logger.info(f"Token refreshed. drvrID: {drvr_id}")
                except:
                    logger.info("Failed to get drvrID from response")

                return True

        logger.info("Failed to get token from headers")
        return False
    except Exception as e:
        logger.info(f"Error refreshing token: {e}")
        return False


def get_earliest_appointment():
    global current_token
    
    logger.debug("Base request data: " + str(CONFIG["appointment_request_base"]))

    if not current_token:
        if not refresh_token():
            return None

    try:
        earliest_appointment = None
        desired_start = datetime.strptime(CONFIG["desired_date_range"]["start"], "%Y-%m-%d").date()
        desired_end = datetime.strptime(CONFIG["desired_date_range"]["end"], "%Y-%m-%d").date()

        with httpx.Client() as client:
            for location_id in CONFIG["location_ids"]:
                request_data = CONFIG["appointment_request_base"].copy()
                request_data["aPosID"] = location_id

                response = client.post(
                    CONFIG["appointments_url"],
                    json=request_data,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": current_token,
                        "User-Agent": CONFIG["ua_str"]
                    }
                )
                response.raise_for_status()

                appointments = response.json()
                logger.info(f"Response for location {location_id}: {appointments}")
                logger.info(f"Found {len(appointments)} available dates for location {location_id}")

                for appointment in appointments:
                    if "appointmentDt" in appointment:
                        appointment_date = datetime.strptime(appointment["appointmentDt"]["date"], "%Y-%m-%d").date()

                        if desired_start <= appointment_date <= desired_end:
                            if (earliest_appointment is None or
                                    appointment_date < datetime.strptime(earliest_appointment["appointmentDt"]["date"],
                                                                         "%Y-%m-%d").date()):
                                earliest_appointment = appointment

        return earliest_appointment

    except Exception as e:
        logger.info(f"Error checking available dates: {e}")
        current_token = None
        return None


def lock_appointment(appointment):
    global current_token, drvr_id

    if not current_token or not drvr_id:
        if not refresh_token():
            return None

    try:
        booked_ts = datetime.now(pytz.timezone(CONFIG['timezone'])).strftime("%Y-%m-%dT%H:%M:%S")

        unlock_data = {"appointmentDt": {}, "dlExam": {}, "drvrDriver": {"drvrId": drvr_id}, "drscDrvSchl": {}}

        lock_data = {
            "appointmentDt": appointment["appointmentDt"],
            "dlExam": appointment["dlExam"],
            "drvrDriver": {"drvrId": drvr_id},
            "drscDrvSchl": {},
            "instructorDlNum": None,
            "bookedTs": booked_ts,
            "startTm": appointment["startTm"],
            "endTm": appointment["endTm"],
            "posId": appointment["posId"],
            "resourceId": appointment["resourceId"],
            "signature": appointment["signature"]
        }

        with httpx.Client() as client:
            response = client.put(
                CONFIG["lock_url"],
                json=unlock_data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": current_token,
                    "User-Agent": CONFIG["ua_str"]
                }
            )
            response.raise_for_status()

            sleep_with_shutdown(10)

            response = client.put(
                CONFIG["lock_url"],
                json=lock_data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": current_token,
                    "User-Agent": CONFIG["ua_str"]
                }
            )
            response.raise_for_status()

            resulting_timezone = response.json()

            logger.info(f"Date {appointment['appointmentDt']['date']} successfully locked")
            return resulting_timezone["bookedTs"]

    except Exception as e:
        logger.info(f"Error locking appointment: {e}")
        return None


def send_otp_email(booked_ts):
    global current_token, drvr_id

    try:
        otp_data = {
            "bookedTs": booked_ts,
            "drvrID": drvr_id,
            "method": "E"
        }

        timeout = httpx.Timeout(15.0, read=None)
        with httpx.Client() as client:
            response = client.post(
                CONFIG["send_otp_url"],
                json=otp_data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": current_token,
                    "User-Agent": CONFIG["ua_str"]
                },
                timeout=timeout
            )

            response.raise_for_status()

            result = response.json()
            if result.get("code") == "success":
                logger.info("OTP code sent to email")
                return True
            else:
                logger.info("Failed to send OTP code")
                return False

    except Exception as e:
        logger.info(f"Error sending OTP code: {e}")
        return False


def logout_imap(mail):
    try:
        mail.logout()
    except imaplib.IMAP4.abort as e:
        logger.debug(f"Gmail IMAP session already closed: {e}")
    except (imaplib.IMAP4.error, OSError) as e:
        logger.warning(f"Failed to log out of Gmail IMAP session: {e}")


def get_otp_from_email():
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(CONFIG["gmail"]["imap_server"])
        mail.login(CONFIG["gmail"]["email"], CONFIG["gmail"]["password"])
        mail.select("inbox")

        status, messages = mail.search(None, '(FROM "roadtests-donotreply@icbc.com")')
        if status != "OK":
            logger.info("Failed to find emails from ICBC")
            return None

        message_ids = messages[0].split()
        if not message_ids:
            logger.info("No new emails from ICBC")
            return None

        latest_email_id = message_ids[-1]
        status, msg_data = mail.fetch(latest_email_id, "(RFC822)")
        if status != "OK":
            logger.info("Failed to read email")
            return None

        raw_email = msg_data[0][1]
        email_message = email.message_from_bytes(raw_email)

        for part in email_message.walk():
            if part.get_content_type() == "text/html":
                html_content = part.get_payload(decode=True).decode()
                match = re.search(r'<h2[^>]*>(\d{6})</h2>', html_content)
                if match:
                    return match.group(1)

        logger.info("Failed to find OTP code in email")
        return None

    except Exception as e:
        logger.info(f"Error getting OTP code from email: {e}")
        return None
    finally:
        if mail is not None:
            logout_imap(mail)


def send_approval_email(appointment):
    approval_id = secrets.token_hex(8)
    message = EmailMessage()
    message["From"] = CONFIG["gmail"]["email"]
    message["To"] = CONFIG["gmail"]["email"]
    message["Subject"] = f"ICBC booking approval [{approval_id}]"
    message["Importance"] = "high"
    message["Priority"] = "urgent"
    message["X-Priority"] = "1"
    message.set_content(
        "An ICBC road test slot is locked and awaiting your approval.\n\n"
        f"Date: {appointment['appointmentDt']['date']}\n"
        f"Time: {appointment['startTm']} - {appointment['endTm']}\n\n"
        "Reply with ACCEPT to book it, or IGNORE to skip it.\n"
        f"This request expires in {CONFIG['approval_timeout']} seconds."
    )

    try:
        with smtplib.SMTP_SSL(
                CONFIG["gmail"]["smtp_server"], CONFIG["gmail"]["smtp_port"]) as smtp:
            smtp.login(CONFIG["gmail"]["email"], CONFIG["gmail"]["password"])
            smtp.send_message(message)
        logger.info("Approval email sent. Reply with ACCEPT or IGNORE.")
        return approval_id
    except Exception as e:
        logger.info(f"Error sending approval email: {e}")
        return None


def extract_decision_from_text(text):
    for line in text.splitlines():
        reply_line = line.strip()
        if (reply_line.startswith((">", "-----Original Message-----")) or
                (reply_line.startswith("On ") and reply_line.endswith("wrote:"))):
            break
        decision = reply_line.lower()
        if decision in ("accept", "ignore"):
            return decision
    return None


def extract_approval_decision(email_message):
    for part in email_message.walk():
        if part.get_content_type() != "text/plain" or part.get_filename():
            continue

        payload = part.get_payload(decode=True)
        if payload is not None:
            charset = part.get_content_charset() or "utf-8"
            decision = extract_decision_from_text(payload.decode(charset, errors="replace"))
            if decision:
                return decision

    return None


def get_approval_reply(approval_id):
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(CONFIG["gmail"]["imap_server"])
        mail.login(CONFIG["gmail"]["email"], CONFIG["gmail"]["password"])
        mail.select("inbox")

        status, messages = mail.search(
            None,
            f'(FROM "{CONFIG["gmail"]["email"]}" SUBJECT "{approval_id}")'
        )
        if status != "OK":
            return None

        for message_id in reversed(messages[0].split()):
            status, msg_data = mail.fetch(message_id, "(RFC822)")
            if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            decision = extract_approval_decision(email.message_from_bytes(msg_data[0][1]))
            if decision:
                return decision
        return None
    except Exception as e:
        logger.info(f"Error checking approval reply: {e}")
        return None
    finally:
        if mail is not None:
            logout_imap(mail)


def wait_for_user_approval(appointment):
    approval_id = send_approval_email(appointment)
    if not approval_id:
        return False

    deadline = time.monotonic() + CONFIG["approval_timeout"]
    while not shutdown_requested and time.monotonic() < deadline:
        decision = get_approval_reply(approval_id)
        if decision == "accept":
            logger.info("User accepted appointment by email")
            return True
        if decision == "ignore":
            logger.info("User ignored appointment by email")
            return False
        sleep_with_shutdown(CONFIG["approval_poll_interval"])

    if shutdown_requested:
        logger.info("Shutdown requested while waiting for user approval")
    else:
        logger.info("Approval request expired without a reply")
    return False


def verify_otp(booked_ts, otp_code):
    global current_token, drvr_id

    try:
        verify_data = {
            "bookedTs": booked_ts,
            "drvrID": drvr_id,
            "code": otp_code
        }

        with httpx.Client() as client:
            response = client.put(
                CONFIG["verify_otp_url"],
                json=verify_data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": current_token,
                    "User-Agent": CONFIG["ua_str"]
                }
            )

            response.raise_for_status()

            result = response.json()
            if result.get("status") == "VERIFIED":
                logger.info("OTP code successfully verified")
                return True
            else:
                logger.info("Invalid OTP code")
                return False

    except Exception as e:
        logger.info(f"Error verifying OTP code: {e}")
        return False


def book_appointment(booked_ts):
    global current_token, drvr_id

    try:
        book_data = {
            "userId": f"WEBD:{drvr_id}",
            "appointment": {
                "drvrDriver": {"drvrId": drvr_id}
            }
        }

        with httpx.Client() as client:
            response = client.put(
                CONFIG["book_url"],
                json=book_data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": current_token,
                    "User-Agent": CONFIG["ua_str"]
                }
            )

            response.raise_for_status()

            result = response.json()
            if result.get("code") == "success":
                logger.info("Booking completed successfully!")
                return True
            else:
                logger.info("Failed to complete booking")
                return False

    except Exception as e:
        logger.info(f"Error completing booking: {e}")
        return False


def auto_book_earliest_appointment():
    appointment = get_earliest_appointment()
    if not appointment:
        logger.info("No suitable dates available for booking")
        return False

    logger.info(f"Found early date: {appointment['appointmentDt']['date']}")

    booked_ts = lock_appointment(appointment)
    if not booked_ts:
        return False

    if not send_otp_email(booked_ts):
        return False

    otp_code = None
    for _ in range(20):
        if shutdown_requested:
            logger.info("Shutdown requested while waiting for OTP code")
            return False
        sleep_with_shutdown(10)
        otp_code = get_otp_from_email()
        if otp_code:
            break

    if not otp_code:
        logger.info("Failed to get OTP code from email")
        return False

    if not wait_for_user_approval(appointment):
        return False

    if not verify_otp(booked_ts, otp_code):
        return False

    if not book_appointment(booked_ts):
        return False

    return True

def auto_look_earliest_appointment():
    appointment = get_earliest_appointment()
    if not appointment:
        logger.info("No suitable dates available for booking")
        return False

    logger.info(f"Found early date: {appointment['appointmentDt']['date']}")

    booked_ts = lock_appointment(appointment)
    if not booked_ts:
        return False
    return True


def get_next_check_time():
    now = datetime.now(pytz.timezone(CONFIG["timezone"]))
    for minute in (14, 29, 44, 59):
        next_run = now.replace(minute=minute, second=50, microsecond=0)
        if next_run > now:
            return next_run
    return (now + timedelta(hours=1)).replace(minute=14, second=50, microsecond=0)


def run_hourly_check_window():
    last_token_time = time.time()

    for _ in range(5):
        if shutdown_requested:
            logger.info("Shutdown requested during hourly check window")
            return False

        current_time = time.time()

        if current_time - last_token_time >= CONFIG["token_refresh_interval"]:
            refresh_token()
            last_token_time = current_time

        if CONFIG["action"] == "book" and auto_book_earliest_appointment():
            logger.info("Booking completed successfully! Script terminating.")
            return True
        if CONFIG["action"] == "look" and auto_look_earliest_appointment():
            logger.info("Found and locked an appointment! Script sleeping until the next hourly window.")
            print("\a") # Beep sound
            sleep_with_shutdown(.1)
            print("\a") # Beep sound
            sleep_with_shutdown(.1)
            print("\a") # Beep sound

        sleep_with_shutdown(CONFIG["check_interval"]+ math.floor(random.random() * 5))

    return True


def main():
    global shutdown_requested

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    if not validate_config():
        logger.error("Startup aborted: required environment variables are missing.")
        return

    logger.info("Starting ICBC checker...")
    while not shutdown_requested:
        if refresh_token():
            break
        logger.warning("Failed to get token. Retrying in 30 seconds. Check your ICBC credentials and network.")
        sleep_with_shutdown(30)

    if shutdown_requested:
        logger.info("Shutdown requested before startup completed.")
        return

    next_check_time = get_next_check_time()

    logger.info("Script started. Monitoring will run at :14, :29, :44, and :59 for 5 intervals each.")

    try:
        while not shutdown_requested:
            now = datetime.now(pytz.timezone(CONFIG["timezone"]))
            if now >= next_check_time:
                logger.info(f"Starting hourly check window at {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                run_hourly_check_window()
                next_check_time = get_next_check_time()
                continue

            sleep_with_shutdown(5)

    except KeyboardInterrupt:
        logger.info("\nScript stopped by user")
    finally:
        if shutdown_requested:
            logger.info("Shutdown complete. Exiting cleanly.")
        else:
            logger.info("Script exited normally.")


if __name__ == "__main__":
    main()

"""
Sends reminder and booking-confirmation emails via Gmail SMTP.
"""

import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _smtp_connection():
    gmail_address = os.environ["GMAIL_ADDRESS"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]
    server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
    server.login(gmail_address, app_password)
    return server, gmail_address


def send_reminder(event_name: str, next_due: datetime, days_until: int, booking_url: str):
    """Send a reminder email that an event is coming up."""
    recipient = os.environ["REMINDER_EMAIL"]
    server, sender = _smtp_connection()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Reminder: {event_name} due in {days_until} day(s)"
    msg["From"] = sender
    msg["To"] = recipient

    due_str = next_due.strftime("%A, %B %-d")
    text = f"""\
Hi,

This is a reminder that your {event_name} is due around {due_str} ({days_until} days away).

Book your appointment here: {booking_url}

-- Calendar Assistant
"""
    html = f"""\
<html><body>
<p>Hi,</p>
<p>Your <strong>{event_name}</strong> is due around <strong>{due_str}</strong> ({days_until} days away).</p>
<p><a href="{booking_url}">Book your appointment</a></p>
<p style="color:#888">— Calendar Assistant</p>
</body></html>
"""
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with server:
        server.sendmail(sender, recipient, msg.as_string())
    print(f"  ✓ Reminder email sent to {recipient}")


def send_booking_confirmation(event_name: str, confirmation_details: str):
    """Send a confirmation email after a booking is made."""
    recipient = os.environ["REMINDER_EMAIL"]
    server, sender = _smtp_connection()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Booked: {event_name} appointment confirmed"
    msg["From"] = sender
    msg["To"] = recipient

    text = f"""\
Hi,

Your {event_name} appointment has been booked!

Details:
{confirmation_details}

-- Calendar Assistant
"""
    html = f"""\
<html><body>
<p>Hi,</p>
<p>Your <strong>{event_name}</strong> appointment has been booked!</p>
<pre style="background:#f4f4f4;padding:12px;border-radius:4px">{confirmation_details}</pre>
<p style="color:#888">— Calendar Assistant</p>
</body></html>
"""
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with server:
        server.sendmail(sender, recipient, msg.as_string())
    print(f"  ✓ Confirmation email sent to {recipient}")

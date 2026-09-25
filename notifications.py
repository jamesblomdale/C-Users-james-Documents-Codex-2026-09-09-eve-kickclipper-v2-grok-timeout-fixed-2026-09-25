"""Optional completion notifications. No external dependency is required.

Browser notifications are triggered by the dashboard when it observes a job
finish. Email is best-effort and enabled only when SMTP_* settings are present.
"""
import smtplib
from email.message import EmailMessage


def send_job_email(config, recipient: str, subject: str, body: str) -> bool:
    host = getattr(config, 'SMTP_HOST', '')
    sender = getattr(config, 'SMTP_FROM', '')
    if not host or not sender or not recipient:
        return False
    msg = EmailMessage()
    msg['From'], msg['To'], msg['Subject'] = sender, recipient, subject
    msg.set_content(body)
    try:
        port = int(getattr(config, 'SMTP_PORT', 587))
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            if getattr(config, 'SMTP_STARTTLS', True):
                smtp.starttls()
            user = getattr(config, 'SMTP_USERNAME', '')
            password = getattr(config, 'SMTP_PASSWORD', '')
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
        return True
    except Exception as exc:
        print(f'[notify] email unavailable: {type(exc).__name__}', flush=True)
        return False

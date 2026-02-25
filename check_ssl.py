import os
import ssl
import socket
import smtplib
from datetime import datetime
from dotenv import load_dotenv
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Load .env file if present
load_dotenv()

# Environment variables
DOMAINS = os.getenv("DOMAINS", "")
THRESHOLD_DAYS = int(os.getenv("THRESHOLD_DAYS", 30))

SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_TO = os.getenv("EMAIL_TO")

domains_list = [d.strip() for d in DOMAINS.split(",") if d.strip()]


def get_ssl_expiry(domain):
    context = ssl.create_default_context()
    with socket.create_connection((domain, 443), timeout=10) as sock:
        with context.wrap_socket(sock, server_hostname=domain) as secure_sock:
            certificate = secure_sock.getpeercert()
            expiry_date = datetime.strptime(
                certificate["notAfter"], "%b %d %H:%M:%S %Y %Z"
            )
            return expiry_date


def build_html_email(results, warning_count, expired_count):
    current_date = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    rows = ""
    for r in results:
        color = "#28a745"  # green
        if r["status"] == "EXPIRING SOON":
            color = "#ffc107"  # orange
        elif r["status"] == "EXPIRED":
            color = "#dc3545"  # red

        rows += f"""
        <tr>
            <td>{r['domain']}</td>
            <td>{r['expiry']}</td>
            <td style="text-align:center;">{r['days']} days</td>
            <td style="color:{color}; font-weight:bold;">{r['status']}</td>
        </tr>
        """

    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; font-size: 14px;">
        <h2>SSL Certificate Monitoring Report</h2>
        <p><strong>Generated on:</strong> {current_date}</p>

        <h3>Summary</h3>
        <ul>
            <li>Total domains checked: {len(results)}</li>
            <li>Expiring soon: {warning_count}</li>
            <li>Expired: {expired_count}</li>
        </ul>

        <h3>Details</h3>
        <table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse;">
            <thead style="background-color: #f2f2f2;">
                <tr>
                    <th>Domain</th>
                    <th>Expiration Date</th>
                    <th>Days Remaining</th>
                    <th>Status</th>
                </tr>
            </thead>
            <tbody>
                {rows}
            </tbody>
        </table>

        <p style="margin-top:20px; font-size:12px; color:gray;">
            Alert threshold configured: {THRESHOLD_DAYS} days<br>
        </p>
    </body>
    </html>
    """

    return html


def send_email(subject, html_body):
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = EMAIL_FROM
    message["To"] = EMAIL_TO

    part = MIMEText(html_body, "html")
    message.attach(part)

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, message.as_string())
        print("Email sent successfully")
    except Exception as e:
        print(f"Failed to send email: {e}")


def main():
    results = []
    warning_count = 0
    expired_count = 0

    for domain in domains_list:
        try:
            expiry_date = get_ssl_expiry(domain)
            days_left = (expiry_date - datetime.utcnow()).days
            expiry_str = expiry_date.strftime("%Y-%m-%d")

            if days_left < 0:
                status = "EXPIRED"
                expired_count += 1
            elif days_left <= THRESHOLD_DAYS:
                status = "EXPIRING SOON"
                warning_count += 1
            else:
                status = "VALID"

            print(f"{domain}: {days_left} days remaining - {status}")

            results.append({
                "domain": domain,
                "expiry": expiry_str,
                "days": days_left,
                "status": status
            })

        except Exception as error:
            print(f"{domain}: ERROR - {error}")

    if results:
        html_body = build_html_email(results, warning_count, expired_count)

        # Optional: dynamic subject if critical
        subject = "SSL Certificate Monitoring Report"
        if expired_count > 0:
            subject = "[CRITICAL] SSL Certificate Expired"
        elif warning_count > 0:
            subject = "[WARNING] SSL Certificate Expiring Soon"

        send_email(subject, html_body)


if __name__ == "__main__":
    main()

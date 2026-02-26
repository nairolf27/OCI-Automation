import os, smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv
from sslyze import Scanner, ServerScanRequest, ScanCommand, ServerNetworkLocation

load_dotenv()

DOMAINS      = [d.strip() for d in os.getenv("DOMAINS", "").split(",") if d.strip()]
THRESHOLD    = int(os.getenv("THRESHOLD_DAYS", 30))
SMTP_SERVER  = os.getenv("SMTP_SERVER")
SMTP_PORT    = int(os.getenv("SMTP_PORT", 587))
SMTP_USER    = os.getenv("SMTP_USERNAME")
SMTP_PASS    = os.getenv("SMTP_PASSWORD")
EMAIL_FROM   = os.getenv("EMAIL_FROM")
EMAIL_TO     = os.getenv("EMAIL_TO")


def scan_domains(domains):
    requests = [
        ServerScanRequest(
            server_location=ServerNetworkLocation(d, 443),
            scan_commands={ScanCommand.CERTIFICATE_INFO}
        )
        for d in domains
    ]    
    scanner = Scanner()
    scanner.queue_scans(requests)

    results = []
    for r in scanner.get_results():
        domain = r.server_location.hostname
        try:
            cert = r.scan_result.certificate_info.result.certificate_deployments[0].received_certificate_chain[0]
            days = (cert.not_valid_after_utc - datetime.now(timezone.utc)).days
            status = "EXPIRED" if days < 0 else "EXPIRING SOON" if days <= THRESHOLD else "VALID"
            print(f"{domain}: {days}d — {status}")
            results.append({"domain": domain, "expiry": cert.not_valid_after_utc.strftime("%Y-%m-%d"), "days": days, "status": status})
        except Exception as e:
            print(f"{domain}: ERROR — {e}")
    return results


def send_email(results):
    colors = {"VALID": "#28a745", "EXPIRING SOON": "#ffc107", "EXPIRED": "#dc3545"}
    rows = "".join(
        f"<tr><td>{r['domain']}</td><td>{r['expiry']}</td><td>{r['days']}d</td>"
        f"<td style='color:{colors[r['status']]}'><b>{r['status']}</b></td></tr>"
        for r in results
    )
    expired = sum(1 for r in results if r["status"] == "EXPIRED")
    warning = sum(1 for r in results if r["status"] == "EXPIRING SOON")

    subject = ("[CRITICAL] SSL Certificate Expired" if expired else
               "[WARNING] SSL Certificate Expiring Soon" if warning else
               "SSL Certificate Monitoring Report")

    html = f"""<html><body style="font-family:Arial;font-size:14px">
        <h2>SSL Certificate Report — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</h2>
        <p>Checked: {len(results)} | Expiring soon: {warning} | Expired: {expired} | Threshold: {THRESHOLD}d</p>
        <table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse">
            <tr style="background:#f2f2f2"><th>Domain</th><th>Expiry</th><th>Days Left</th><th>Status</th></tr>
            {rows}
        </table>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, EMAIL_FROM, EMAIL_TO
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    print("Email sent.")


if __name__ == "__main__":
    if not DOMAINS:
        print("No domains set. Configure DOMAINS env var.")
    else:
        results = scan_domains(DOMAINS)
        if results:
            send_email(results)

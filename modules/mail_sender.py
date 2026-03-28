import os
import base64
from email.message import EmailMessage
from googleapiclient.discovery import build

def send_employee_mail(employee_email, pdf_path, emp_name, month, creds):
    """
    Sends a personalized email to the employee using the dynamically logged-in user.
    'creds' is passed from the production OAuth session.
    """
    try:
        # Initialize the Gmail service with dynamic credentials
        service = build('gmail', 'v1', credentials=creds)

        # Create the email container
        msg = EmailMessage()
        
        # Personalized Subject
        msg['Subject'] = f"Monthly Payslip - {month} | {emp_name}"
        
        # In Gmail API, when using userId='me', the 'From' header is 
        # automatically handled by the authenticated account.
        msg['To'] = employee_email
        
        # Personalized Body Content
        body_content = f"""Dear {emp_name},

Please find attached your payslip for the month of {month}.

This is an automated email sent from the HR Payroll System. If you find any discrepancies in your payslip, please reach out to the HR department.

Best Regards,
HR Operations Team
MainstreamTek Private Limited"""

        msg.set_content(body_content)

        # Attach PDF
        file_name = os.path.basename(pdf_path)
        if os.path.exists(pdf_path):
            with open(pdf_path, 'rb') as f:
                pdf_data = f.read()
                msg.add_attachment(
                    pdf_data, 
                    maintype='application', 
                    subtype='pdf', 
                    filename=file_name
                )
        else:
            print(f"   - Attachment error: File not found at {pdf_path}")
            return False

        # Base64 Encoding (Required by Gmail API send method)
        encoded_message = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')
        
        # Send using the 'me' shortcut for the logged-in user
        service.users().messages().send(
            userId="me", 
            body={'raw': encoded_message}
        ).execute()
        
        return True
        
    except Exception as e:
        # This will show up in your Render.com logs
        print(f"   - Failed to send mail to {emp_name} ({employee_email}): {e}")
        return False
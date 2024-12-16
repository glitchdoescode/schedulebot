# chatbot/twilio/handlers.py

from flask import Response
from chatbot.conversation import scheduler
import logging
from twilio.twiml.messaging_response import MessagingResponse
import os

logger = logging.getLogger(__name__)
if not logger.hasHandlers():
    logging.basicConfig(level=logging.INFO)

def handle_incoming_message(request):
    from_number = request.form.get('From', '').strip()
    body = request.form.get('Body', '').strip()

    if not from_number or not body:
        resp = MessagingResponse()
        resp.message("Missing 'From' or 'Body' in the request.")
        logger.warning("Received message with missing 'From' or 'Body'.")
        return Response(str(resp), mimetype='application/xml'), 400

    logger.info(f"Received message from {from_number}: {body}")

    try:
        # Delegate message handling to the scheduler
        scheduler.message_handler.receive_message(from_number, body)
        logger.info(f"Message from {from_number} processed successfully.")
    except Exception as e:
        logger.error(f"Error processing message from {from_number}: {str(e)}")
        resp = MessagingResponse()
        resp.message("An error occurred while processing your message. Please try again later.")
        return Response(str(resp), mimetype='application/xml'), 500

    return Response("", status=200)

def initialize_conversation(interviewer_name, interviewer_number, interviewer_email, interviewees_data, superior_flag, meeting_duration, role_to_contact_name, role_to_contact_number, role_to_contact_email, company_details):
    """
    Initializes a new conversation by calling the InterviewScheduler's start_conversation method.
    Ensures that each interviewee has a 'jd_title'.
    """
    # Validate interviewees_data to ensure each interviewee has 'jd_title'
    for idx, interviewee in enumerate(interviewees_data):
        if 'jd_title' not in interviewee:
            error_msg = f"Interviewee at index {idx} is missing 'jd_title'."
            logger.error(error_msg)
            raise ValueError(error_msg)
        elif not isinstance(interviewee['jd_title'], str) or not interviewee['jd_title'].strip():
            error_msg = f"Interviewee at index {idx} has an invalid 'jd_title'. It must be a non-empty string."
            logger.error(error_msg)
            raise ValueError(error_msg)

    # Optionally, further validate 'jd_title' against a list of allowed titles
    # valid_jd_titles = ["Software Engineer", "Data Analyst", "Project Manager", "QA Tester"]
    # for idx, interviewee in enumerate(interviewees_data):
    #     if interviewee['jd_title'] not in valid_jd_titles:
    #         error_msg = f"Interviewee at index {idx} has an invalid 'jd_title': '{interviewee['jd_title']}'. Must be one of {valid_jd_titles}."
    #         logger.error(error_msg)
    #         raise ValueError(error_msg)

    try:
        conversation_id = scheduler.start_conversation(
            interviewer_name=interviewer_name,
            interviewer_number=interviewer_number,
            interviewer_email=interviewer_email,
            interviewees_data=interviewees_data,
            superior_flag=superior_flag,
            meeting_duration=meeting_duration,
            role_to_contact_name=role_to_contact_name,
            role_to_contact_number=role_to_contact_number,
            role_to_contact_email=role_to_contact_email,
            company_details=company_details
        )
        logger.info(f"Initialized conversation {conversation_id} with {len(interviewees_data)} interviewees.")
        return conversation_id
    except Exception as e:
        logger.error(f"Error initializing conversation: {str(e)}")
        raise

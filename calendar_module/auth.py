# calendar_module/auth.py

import os
import json
from flask import request, jsonify
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from dotenv import load_dotenv
import logging

# Load environment variables from .env
load_dotenv()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")

SCOPES = ['https://www.googleapis.com/auth/calendar']

# File path to save the global credentials
TOKEN_FILE = 'app_token.json'

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def authenticate() -> str:
    """
    Initiates the OAuth2 flow to authenticate the application with Google Calendar.
    Returns the authorization URL for the user to complete the authentication.
    """
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": GOOGLE_REDIRECT_URI,
            }
        },
        scopes=SCOPES,
    )
    flow.redirect_uri = GOOGLE_REDIRECT_URI

    authorization_url, _ = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )

    logger.info("Generated authorization URL for the application.")
    return authorization_url


def oauth2callback():
    """
    Handles the OAuth2 callback from Google after user consents.
    Fetches and saves the application-wide credentials.
    """
    authorization_response = request.url
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": GOOGLE_REDIRECT_URI,
            }
        },
        scopes=SCOPES,
    )
    flow.redirect_uri = GOOGLE_REDIRECT_URI

    try:
        flow.fetch_token(authorization_response=authorization_response)
    except Exception as e:
        logger.error(f"Error fetching token: {str(e)}")
        return jsonify({"error": "Failed to fetch token."}), 400

    credentials = flow.credentials

    # Save the credentials for global access
    save_credentials(credentials)
    logger.info("Application-wide authentication successful.")
    return jsonify({"message": "Authentication successful. You can close this window."}), 200


def save_credentials(credentials: Credentials):
    """
    Saves the credentials to a file for global access.
    """
    token_data = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": credentials.scopes,
        "expiry": credentials.expiry.isoformat() if credentials.expiry else None
    }
    with open(TOKEN_FILE, 'w') as token_file:
        json.dump(token_data, token_file)
    logger.info("Credentials saved to file.")


def load_credentials() -> Credentials:
    """
    Loads credentials from the saved file and refreshes if needed.
    Returns:
        Credentials: The Google API credentials
    Raises:
        Exception: If the application has not been authenticated yet.
    """
    if not os.path.exists(TOKEN_FILE):
        raise Exception("Application not authenticated. Please authenticate first.")

    with open(TOKEN_FILE, 'r') as token_file:
        token_data = json.load(token_file)

    creds = Credentials(
        token=token_data["token"],
        refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=token_data["scopes"]
    )

    # Refresh token if expired
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_credentials(creds)  # Update the file with the new token
            logger.info("Credentials refreshed and saved.")
        except Exception as e:
            logger.error(f"Failed to refresh credentials: {str(e)}")
            raise Exception("Failed to refresh credentials. Please re-authenticate.")

    return creds

# chatbot/utils.py

import json
from datetime import datetime, timezone, timedelta
import pytz
import logging
from langchain.prompts import PromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from dotenv import load_dotenv
import re

load_dotenv()

# Configure logging
logger = logging.getLogger(__name__)
if not logger.hasHandlers():
    logging.basicConfig(level=logging.INFO)

def normalize_number(number):
    return number.lower().replace('whatsapp:', '').strip()

def parse_llm_json_output(llm_output: str) -> dict:
    """
    Parses LLM output containing JSON within markdown code blocks into a Python dictionary.
    """
    clean_json = (
        llm_output
        .replace('```json', '')
        .replace('```', '')
        .strip()
    )

    try:
        data = json.loads(clean_json)
        return {
            "time_slots": data.get("time_slots", []),
            "timezone": data.get("timezone", "UTC")
        }
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON: {e}")
        return {"time_slots": [], "timezone": "UTC"}
    
def extract_slots_and_timezone(message, phone_number, participant_history, meeting_duration):
    """
    Extracts time slots and timezone from the participant's message, utilizing the participant's conversation history for context only.
    Handles multiple timezone patterns and ISO time format conversion.
    """

    current_date = datetime.now(timezone.utc)

    json1 = """```json
    {{
      "time_slots": [
        {{
          "start_time": "YYYY-MM-DDTHH:MM:SS",
          "end_time": "YYYY-MM-DDTHH:MM:SS"
        }},
        ...
      ],
      "timezone": "Timezone/Region or Unspecified"
    }}
    ```"""
    json2 = """
    ```json
    {{
      "time_slots": [
        {{
          "start_time": "2024-11-01T15:00:00",
          "end_time": "2024-11-01T16:00:00"
        }},
        {{
          "start_time": "2024-11-01T16:00:00",
          "end_time": "2024-11-01T17:00:00"
        }},
        {{
          "start_time": "2024-11-02T10:00:00",
          "end_time": "unspecified"
        }}
      ],
      "timezone": "America/New_York"
    }}
    ```"""
    json3 = """```json
    {{
      "time_slots": [
        {{
          "start_time": "2024-11-06T09:00:00",
          "end_time": "2024-11-06T10:00:00"
        }},
        {{
          "start_time": "2024-11-06T10:30:00",
          "end_time": "2024-11-06T11:30:00"
        }},
        {{
          "start_time": "2024-11-07T16:00:00",
          "end_time": "unspecified"
        }}
      ],
      "timezone": "Europe/London"
    }}
    ```"""

    json4="""
{{
  "time_slots": [
    {{
      "start_time": "2024-11-01T10:00:00",
      "end_time": "2024-11-01T11:00:00"
    }},
    {{
      "start_time": "2024-11-01T11:30:00",
      "end_time": "2024-11-01T12:30:00"
    }},
    {{
      "start_time": "2024-11-01T12:30:00",
      "end_time": "2024-11-01T13:30:00"
    }},
    {{
      "start_time": "2024-11-01T14:00:00",
      "end_time": "2024-11-01T15:00:00"
    }}
  ],
  "timezone": "unspecified"
}}
"""
    json5 = """
    {{}}
    """

    PROMPT_TEMPLATE = f"""
## Role

Act as an expert natural language processor specializing in date and time extraction from conversational text. Utilize your advanced understanding of time expressions, human-like language patterns, and date/time structures to parse complex and nuanced language inputs. Use the conversation history for additional context only if relevant to interpret the participant’s intent.

## Task

1. **Extract Time Slots and Timezone**:
   - Extract time slots and timezone information from the participant's message. Use context from the conversation history to clarify ambiguous timing references or timezone indications. **Support all input languages** for parsing.
   - Ensure that the extracted timezone is a valid IANA time zone name (e.g., "America/New_York", "Europe/London", "Asia/Kolkata"). If the timezone provided does not match any IANA time zone, set it to "unspecified".
   - If the message contains vague timing references such as "second half of the day," "after midnight," or similar expressions without specific times, return an empty JSON data structure: {json5}.

2. **Handle Confirmations**:
   - Detect if the user message indicates confirmation (e.g., "yes," "yeah that works," "that works for me") and check the conversation history to identify what the confirmation refers to.
   - If the confirmation pertains to a previously suggested time slot, assign the confirmed time and include it in the extracted results.

3. **Split Time Slots for Multiple Interviewees**:
   - If a time range is provided that is longer than the meeting duration, split the time range into multiple slots of the meeting duration. For example, if the user provides a slot "1 PM to 3 PM" and the meeting time is 60 minutes, extract two separate slots: "1 PM to 2 PM" and "2 PM to 3 PM."

4. **Handle Gaps Between Interviewees**:
   - If the message includes a gap between slots (e.g., "with gaps of 30 minutes between each interviewee"), extract multiple time slots with the specified gap. For example, if the user provides "10 AM to 10 PM with gaps of 30 minutes" and the meeting duration is 60 minutes, generate slots like:
     - "10:00 AM to 11:00 AM"
     - Break for 30 minutes
     - "11:30 AM to 12:30 PM"
     - Break for 30 minutes
     - And so on until the end of the provided time range.

5. **Output Results**:
   - Provide all extracted time slots, inferred timezones, and confirmed times (if applicable) in a well-structured JSON format. Ensure the output is in English and can be easily parsed with the `json` Python library.
   - For vague timing references, return the following JSON structure:
     ```json
     {json5}
     ```

## Specifics

- Detect and extract all possible time slots mentioned by the participant, considering broader conversation context as needed.
- Recognize various time expressions (e.g., "tomorrow at 3 PM," "next Monday from 2-4 PM," or "anytime after 6 PM") in any input language.
- Ensure that vague expressions like "second half of the day" or "after midnight" result in an empty JSON data structure (`{json5}`).
- Convert all extracted times to a standard timestamp format (ISO 8601).
- Handle cases where only a start time is provided by setting `end_time` to "unspecified."
- If there are multiple slots in a range, split the time range into distinct slots, ensuring no overlap, and respecting the provided meeting duration.
- If the message indicates confirmation, cross-reference it with the **Participant's Conversation History** to identify the confirmed time slot and include it as `confirmed_time`.

## Output Format
```json
{json1}
```
### Output JSON Structure:
- `time_slots`: A list of objects with `start_time` and `end_time` for each slot.
- `timezone`: A string indicating the inferred timezone or "unspecified" if not provided.
- `confirmed_time`: The confirmed time slot, if applicable, structured as an object with `start_time` and `end_time`. If no confirmation is detected, this field is absent.

## Examples

**Example 1:** Confirmation for a suggested time slot

**Input Message:** "Yes, that works for me."

**Participant History:** "Could you do Friday from 3 PM to 5 PM?"

**Output JSON:** 
```json
{json2}
```
**Example 2:** Explicit time slot and timezone extraction without confirmation

**Input Message:** "On se parle mercredi prochain de 9h à 11h, et peut-être jeudi à 16h. Mon contact est +44-7911-123456."

**Participant History:** "Je suis basé à Londres."

**Output JSON:** 
```json
{json3}
```
**Example 3:** Extracting multiple slots with gaps and durations

**Input Message:** "I am available from 10 AM to 10 PM on Friday with gaps of 30 minutes between each interviewee."

**Meeting Duration:** 60 minutes

**Output JSON:**

```json
  {json4}
```
Example 4: Vague timing references Input Message: "I am free tomorrow in the second half of the day." Output JSON: {json5}

Input Message: "I am free after midnight." Output JSON: 
```json
{json5}
```

Notes
Confirmation Handling:

Detect common confirmation phrases (e.g., "yes," "that works," "works for me") in any language.
Accurately identify the confirmed time slot by cross-referencing the conversation history.
Time Slot Extraction:

Convert all extracted times to a standard timestamp format (ISO 8601).
Handle cases where only a start time is provided by setting end_time to "unspecified."
Accurately parse multiple slots within a single message.
Timezone Handling:

Infer timezone based on the participant's phone number or explicit mentions in the conversation history.
Ensure that the timezone is a valid IANA time zone name.
Default to "unspecified" if no valid timezone information is available.
Ensure that the output is in English JSON format regardless of the input language.

Provide clear, reliable, and accurate information for scheduling purposes.

Input
Meeting Duration(in minutes)
{meeting_duration} minutes

Current_time (in UTC)
{{current_date}}

Input_Conversational_Message
{{message}}

User's Number
{{phone_number}}

Participant's Conversation History
{{participant_history}} """
    
    


    llm_model = ChatGoogleGenerativeAI(
        model="gemini-1.5-flash",
        temperature=0.7,
    )

    prompt_template = PromptTemplate(
        input_variables=['message', 'current_date', 'phone_number', 'participant_history'],
        template=PROMPT_TEMPLATE
    )

    chain = prompt_template | llm_model

    response = chain.invoke({
        'message': message,
        'current_date': current_date,
        'phone_number': phone_number,
        'participant_history': participant_history
    })

    logger.info(f"extract_slots_and_timezone: {response.content}")

    # Parse the LLM output directly into the required format
    return parse_llm_json_output(response.content)


def convert_slots_to_utc(slots):
    """
    Helper method to convert each time slot from local time to UTC.
    """
    timezone_str = slots.get('timezone', 'UTC')
    try:
        timezone = pytz.timezone(timezone_str)
    except pytz.UnknownTimeZoneError:
        logger.error(f"Unknown timezone: {timezone_str}. Defaulting to UTC.")
        timezone = pytz.UTC

    slots_utc = {"time_slots": []}

    for slot in slots.get("time_slots", []):
        try:
            # Parse and handle start time
            start = datetime.fromisoformat(slot["start_time"])
            if start.tzinfo is None:  # Only localize if naive
                start = timezone.localize(start)
            start_utc = start.astimezone(pytz.UTC)

            # Parse and handle end time
            end = None
            if slot.get("end_time") and slot["end_time"].lower() != "unspecified":
                end = datetime.fromisoformat(slot["end_time"])
                if end.tzinfo is None:  # Only localize if naive
                    end = timezone.localize(end)
                end_utc = end.astimezone(pytz.UTC)
            else:
                end_utc = start_utc + timedelta(hours=1)  # Default end time if unspecified

            slots_utc["time_slots"].append({
                "start_time": start_utc.isoformat(),
                "end_time": end_utc.isoformat()
            })
        except Exception as e:
            logger.error(f"Error processing slot {slot}: {e}")
            continue

    slots_utc["timezone"] = "UTC"  # Indicate that slots are now in UTC
    return slots_utc

def parse_llm_json_timezone(llm_output: str) -> dict:
    """
    Parses LLM output containing JSON within markdown code blocks into a Python dictionary.
    """
    clean_json = (
        llm_output
        .replace('```json', '')
        .replace('```', '')
        .strip()
    )

    try:
        data = json.loads(clean_json)
        return data
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON: {e}")
        return {}

def extract_timezone_from_number(phone_number: str) -> str:
    """
    Uses LLM to infer the timezone from the phone number.
    """
    json1="""
        {{
          "timezone": "Continent/City"
        }}
"""
    json2 = """
        {{
          "timezone": "America/New_York"
        }}
    """

    json3 = """
        {{
          "timezone": "Europe/London"
        }}
    """
    PROMPT_TEMPLATE = f"""
You are an expert assistant that helps infer the timezone of a user based on their phone number.

Given the following phone number: {phone_number}, determine the most likely timezone of the user.

Provide your answer in the following JSON format:

```json
{json1}
```
Ensure that the timezone provided is a valid IANA time zone name (e.g., "America/New_York", "Europe/London", "Asia/Kolkata").
If the timezone cannot be determined or is invalid, set "timezone" to "unspecified".
Examples:

Phone number: +1-202-555-0123 Output:
```json
{json2}
```
Phone number: +44 20 7946 0958 Output:
```json
{json3}
```
Now, determine the timezone for the following phone number.

Phone number: {{phone_number}} """

    # Instantiate LLM model
    llm_model = ChatGoogleGenerativeAI(
        model="gemini-1.5-flash",
        temperature=0.7,
    )

    prompt_template = PromptTemplate(
        input_variables=['phone_number'],
        template=PROMPT_TEMPLATE
    )

    chain = prompt_template | llm_model

    response = chain.invoke({
        'phone_number': phone_number
    })

    # Parse the LLM output
    result = parse_llm_json_timezone(response.content)

    timezone = result.get('timezone', 'unspecified')
    logger.info(f"timezone:{timezone}")
    return timezone

def extract_city_from_message(message: str) -> str:
    """ Uses LLM to extract the city from the user's message. """ 
    json1 = """
{{
  "city": "City Name"
}}
"""
    json2="""
{{ 
  "city": "New York" 
}}
"""
    json3 = """
{{
  "city": "unspecified" 
}}

"""
    PROMPT_TEMPLATE = f""" You are an assistant that extracts the city name from a user's message.

Given the following message, identify and return the city mentioned.

Provide your answer in the following JSON format:
```json
{json1}
```
- **If a city is identified, ensure it is a recognized city with a corresponding IANA time zone.**
- **If no city is found or the city cannot be associated with a valid timezone, set "city" to "unspecified".**

**Examples:**

Message: "I am based in New York and available for the interview."

Output:
```json
{json2}
```
Message: "Looking forward to our meeting."

Output:
```json
{json3}
```
Now, extract the city from the following message:

Message: {{message}} """

    llm_model = ChatGoogleGenerativeAI(
        model="gemini-1.5-flash",
        temperature=0.5,
    )

    prompt_template = PromptTemplate(
        input_variables=['message'],
        template=PROMPT_TEMPLATE
    )

    chain = prompt_template | llm_model

    response = chain.invoke({
        'message': message
    })

    # Parse the LLM output
    result = parse_llm_json_timezone(response.content)

    city = result.get('city', 'unspecified')
    logger.info(f"city: {city}")
    return city

def extract_timezone_from_city(city: str) -> str: 
    """ Uses LLM to infer the timezone from the city name. """ 
    if city.lower() == 'unspecified' or not city.strip(): 
        return 'unspecified'
    
    json1 = """
{{
  "timezone": "Continent/City"
}}
"""
    json2 = """
{{ 
  "timezone": "Asia/Tokyo" 
}}
"""
    json3 = """
{{ 
  "timezone": "Europe/London" 
}}
"""
    PROMPT_TEMPLATE = f"""
You are an expert assistant that determines the timezone of a city.

Given the following city name, provide its timezone in the following JSON format:
```json
{json1}
```
- **Ensure that the timezone provided is a valid IANA time zone name** (e.g., "America/New_York", "Europe/London", "Asia/Kolkata").
- If the timezone cannot be determined or is invalid, set "timezone" to "unspecified".

**Examples:**

City: Tokyo Output:
```json
{json2}
```

City: London Output:
```json
{json3}
```

Now, determine the timezone for the following city:

City: {{city}} """
    llm_model = ChatGoogleGenerativeAI(
        model="gemini-1.5-flash",
        temperature=0.7,
    )

    prompt_template = PromptTemplate(
        input_variables=['city'],
        template=PROMPT_TEMPLATE
    )

    chain = prompt_template | llm_model

    response = chain.invoke({
        'city': city
    })

    # Parse the LLM output
    result = parse_llm_json_timezone(response.content)

    timezone = result.get('timezone', 'unspecified')
    logger.info(f"timezone:{timezone}")
    return timezone

def sanitize_message(message: str) -> str:
    """
    Sanitizes the user message by removing special characters and emojis.
    
    Args:
        message (str): The raw message input from the user.
    
    Returns:
        str: The sanitized message with emojis and non-printable characters removed.
    """
    # Define a regex pattern to match emojis and various symbol ranges
    emoji_pattern = re.compile(
        "[" 
        "\U0001F600-\U0001F64F"  # Emoticons
        "\U0001F300-\U0001F5FF"  # Symbols & Pictographs
        "\U0001F680-\U0001F6FF"  # Transport & Map Symbols
        "\U0001F1E0-\U0001F1FF"  # Flags
        "]+", 
        flags=re.UNICODE
    )
    
    # Remove emojis using the regex pattern
    message = emoji_pattern.sub(r'', message)
    
    # Remove other non-printable characters
    message = ''.join(filter(lambda x: x.isprintable(), message))
    
    return message

def get_localized_current_time(timezone_str: str) -> str:
    """
    Returns the current time localized to the specified timezone.

    Args:
        timezone_str (str): Timezone string in the format 'Continent/City'.

    Returns:
        str: Formatted current time in the specified timezone.
    """
    try:
        tz = pytz.timezone(timezone_str)
    except pytz.UnknownTimeZoneError:
        logger.error(f"Unknown timezone: {timezone_str}. Defaulting to UTC.")
        tz = pytz.UTC
    localized_time = datetime.now(tz).strftime('%A, %B %d, %Y at %I:%M %p %Z')
    logger.info(f"localized_time:{localized_time}")
    return localized_time
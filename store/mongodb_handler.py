# mongodb_handler.py

from pymongo import MongoClient
from datetime import datetime
import pytz
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

class MongoDBHandler:
    def __init__(self, uri: str, db_name: str):
        """
        Initializes the MongoDB handler with the given URI and database name.
        
        Args:
            uri (str): The MongoDB connection URI.
            db_name (str): The name of the database to use.
        """
        self.client = MongoClient(uri)
        self.db = self.client[db_name]
        self.conversations = self.db.conversations
        self.attention_flags = self.db.attention_flags  # New collection for attention flags

    # ------------------ Conversation Methods ------------------

    def create_conversation(self, conversation_data: Dict[str, Any]) -> None:
        """
        Inserts a new conversation document into the database.
        
        Args:
            conversation_data (Dict[str, Any]): The conversation data to insert.
        """
        try:
            self.conversations.insert_one(conversation_data)
            logger.info(f"Conversation {conversation_data['conversation_id']} inserted into MongoDB.")
        except Exception as e:
            logger.error(f"Error inserting conversation into MongoDB: {e}")
            raise

    def get_conversation(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves a conversation document by conversation_id.
        
        Args:
            conversation_id (str): The unique identifier of the conversation.
        
        Returns:
            Optional[Dict[str, Any]]: The conversation document if found, else None.
        """
        try:
            conversation = self.conversations.find_one({'conversation_id': conversation_id})
            if conversation:
                logger.info(f"Conversation {conversation_id} retrieved from MongoDB.")
            else:
                logger.warning(f"Conversation {conversation_id} not found in MongoDB.")
            return conversation
        except Exception as e:
            logger.error(f"Error retrieving conversation from MongoDB: {e}")
            raise

    def get_all_conversations(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Retrieves all conversation documents, optionally filtered by status.
        
        Args:
            status (Optional[str], optional): The status to filter conversations by (e.g., 'active', 'completed'). Defaults to None.
        
        Returns:
            List[Dict[str, Any]]: A list of conversation documents.
        """
        try:
            query = {}
            if status:
                query['status'] = status
            conversations = list(self.conversations.find(query))
            logger.info(f"Retrieved {len(conversations)} conversations from MongoDB with status='{status}'.")
            return conversations
        except Exception as e:
            logger.error(f"Error retrieving conversations from MongoDB: {e}")
            raise

    def update_conversation(self, conversation_id: str, update_data: Dict[str, Any], filter_data: Optional[Dict[str, Any]] = None) -> None:
        """
        Updates a conversation document with new data.
        If filter_data is provided, it uses it as an additional filter.
        
        Args:
            conversation_id (str): The unique identifier of the conversation.
            update_data (Dict[str, Any]): The data to update in the conversation.
            filter_data (Optional[Dict[str, Any]], optional): Additional filter criteria. Defaults to None.
        """
        try:
            if filter_data:
                query = {'conversation_id': conversation_id}
                query.update(filter_data)
            else:
                query = {'conversation_id': conversation_id}
            
            result = self.conversations.update_one(query, {'$set': update_data})
            if result.matched_count:
                logger.info(f"Conversation {conversation_id} updated in MongoDB.")
            else:
                logger.warning(f"No matching conversation found to update for conversation_id: {conversation_id}.")
        except Exception as e:
            logger.error(f"Error updating conversation in MongoDB: {e}")
            raise

    def delete_conversation(self, conversation_id: str) -> bool:
        """
        Deletes a conversation document by conversation_id, along with its associated attention flags.
        
        Args:
            conversation_id (str): The unique identifier of the conversation to delete.
        
        Returns:
            bool: True if deletion was successful, False otherwise.
        """
        try:
            # Delete the conversation
            result = self.conversations.delete_one({'conversation_id': conversation_id})
            if result.deleted_count > 0:
                logger.info(f"Conversation {conversation_id} deleted from MongoDB.")
                
                # Also delete associated attention flags
                flags_deleted = self.attention_flags.delete_many({'conversation_id': conversation_id})
                logger.info(f"Deleted {flags_deleted.deleted_count} attention flags associated with conversation {conversation_id}.")
                return True
            else:
                logger.warning(f"Conversation {conversation_id} not found in MongoDB.")
                return False
        except Exception as e:
            logger.error(f"Error deleting conversation from MongoDB: {e}")
            raise

    def delete_conversations_past_scheduled_time(self) -> None:
        """
        Deletes conversations where all scheduled times have passed.
        """
        try:
            current_time = datetime.now(pytz.UTC).isoformat()
            result = self.conversations.delete_many({
                'interviewees': {
                    '$elemMatch': {
                        'scheduled_slot.end_time': {'$lt': current_time}
                    }
                }
            })
            logger.info(f"Deleted {result.deleted_count} conversations past scheduled time from MongoDB.")
        except Exception as e:
            logger.error(f"Error deleting past conversations from MongoDB: {e}")
            raise

    def find_conversation_by_number(self, number: str) -> Optional[Dict[str, Any]]:
        """
        Finds a single conversation that involves the given phone number, either as an interviewer or interviewee.
        Returns the conversation document if found, otherwise None.
        
        Args:
            number (str): The phone number to search for.
        
        Returns:
            Optional[Dict[str, Any]]: The conversation document if found, else None.
        """
        try:
            conversation = self.conversations.find_one({
                '$or': [
                    {'interviewer.number': number},
                    {'interviewees.number': number}
                ]
            })
            if conversation:
                logger.info(f"Found conversation containing number: {number}")
            else:
                logger.warning(f"No conversation found containing number: {number}")
            return conversation
        except Exception as e:
            logger.error(f"Error retrieving conversation by number {number} from MongoDB: {e}")
            raise

    def find_active_conversations_by_interviewer(self, interviewer_number: str) -> List[Dict[str, Any]]:
        """
        Finds all active conversations for a given interviewer number.
        Active conversations are those with status 'active'.
        
        Args:
            interviewer_number (str): The interviewer's phone number.
        
        Returns:
            List[Dict[str, Any]]: A list of active conversation documents.
        """
        try:
            conversations = list(self.conversations.find({
                'interviewer.number': interviewer_number,
                'status': 'active'
            }))
            logger.info(f"Found {len(conversations)} active conversations for interviewer {interviewer_number}.")
            return conversations
        except Exception as e:
            logger.error(f"Error retrieving active conversations for interviewer {interviewer_number} from MongoDB: {e}")
            raise

    def find_conversations_by_number(self, number: str) -> List[Dict[str, Any]]:
        """
        Finds all conversations that involve the given phone number, either as an interviewer or interviewee.
        
        Args:
            number (str): The phone number to search for.
        
        Returns:
            List[Dict[str, Any]]: A list of conversation documents.
        """
        try:
            conversations = list(self.conversations.find({
                '$or': [
                    {'interviewer.number': number},
                    {'interviewees.number': number}
                ]
            }))
            if conversations:
                logger.info(f"Found {len(conversations)} conversations containing number: {number}")
            else:
                logger.warning(f"No conversations found containing number: {number}")
            return conversations
        except Exception as e:
            logger.error(f"Error retrieving conversations by number {number} from MongoDB: {e}")
            raise

    # ------------------ Attention Flag Methods ------------------

    def create_attention_flag(self, flag_entry: Dict[str, Any]) -> None:
        """
        Inserts a new attention flag document into the database.
        
        Args:
            flag_entry (Dict[str, Any]): The attention flag data to insert.
        """
        try:
            self.attention_flags.insert_one(flag_entry)
            logger.info(f"Attention flag {flag_entry['id']} for conversation {flag_entry['conversation_id']} inserted into MongoDB.")
        except Exception as e:
            logger.error(f"Error inserting attention flag into MongoDB: {e}")
            raise

    def get_attention_flags(self, conversation_id: Optional[str] = None, resolved: Optional[bool] = None) -> List[Dict[str, Any]]:
        """
        Retrieves attention flag documents, optionally filtered by conversation_id and resolved status.
        
        Args:
            conversation_id (Optional[str], optional): The conversation ID to filter by. Defaults to None.
            resolved (Optional[bool], optional): The resolved status to filter by. Defaults to None.
        
        Returns:
            List[Dict[str, Any]]: A list of attention flag documents.
        """
        try:
            query = {}
            if conversation_id:
                query['conversation_id'] = conversation_id
            if resolved is not None:
                query['resolved'] = resolved
            flags = list(self.attention_flags.find(query))
            logger.info(f"Retrieved {len(flags)} attention flags from MongoDB with query: {query}.")
            return flags
        except Exception as e:
            logger.error(f"Error retrieving attention flags from MongoDB: {e}")
            raise

    def resolve_attention_flag(self, flag_id: str) -> bool:
        """
        Marks an attention flag as resolved.
        
        Args:
            flag_id (str): The unique identifier of the attention flag.
        
        Returns:
            bool: True if the flag was successfully resolved, False otherwise.
        """
        try:
            result = self.attention_flags.update_one(
                {'id': flag_id},
                {'$set': {'resolved': True, 'resolved_at': datetime.now(pytz.UTC).isoformat()}}
            )
            if result.modified_count > 0:
                logger.info(f"Attention flag {flag_id} marked as resolved in MongoDB.")
                return True
            else:
                logger.warning(f"Attention flag {flag_id} not found or already resolved in MongoDB.")
                return False
        except Exception as e:
            logger.error(f"Error resolving attention flag {flag_id} in MongoDB: {e}")
            raise

    def get_attention_flags_by_conversation(self, conversation_id: str) -> List[Dict[str, Any]]:
        """
        Retrieves all unresolved attention flags for a specific conversation.
        
        Args:
            conversation_id (str): The unique identifier of the conversation.
        
        Returns:
            List[Dict[str, Any]]: A list of attention flag documents.
        """
        try:
            flags = self.get_attention_flags(conversation_id=conversation_id, resolved=False)
            logger.info(f"Retrieved {len(flags)} unresolved attention flags for conversation {conversation_id}.")
            return flags
        except Exception as e:
            logger.error(f"Error retrieving attention flags for conversation {conversation_id}: {e}")
            raise

    # ------------------ Additional Utility Methods ------------------

    def find_completed_conversations(self) -> List[Dict[str, Any]]:
        """
        Retrieves all conversations marked as 'completed'.
        
        Returns:
            List[Dict[str, Any]]: A list of completed conversation documents.
        """
        try:
            conversations = list(self.conversations.find({'status': 'completed'}))
            logger.info(f"Retrieved {len(conversations)} completed conversations from MongoDB.")
            return conversations
        except Exception as e:
            logger.error(f"Error retrieving completed conversations from MongoDB: {e}")
            raise

    def get_all_attention_flags(self) -> List[Dict[str, Any]]:
        try:
            flags = list(self.attention_flags.find({"resolved": False}))
            # Convert ObjectId to string if necessary
            for flag in flags:
                flag['_id'] = str(flag['_id'])
            return flags
        except Exception as e:
            logger.error(f"Error retrieving all attention flags: {str(e)}")
            return []
        
    def get_completed_conversations(self) -> List[Dict[str, Any]]:
        try:
            conversations = list(self.conversations.find({"status": "completed"}))
            for convo in conversations:
                convo['_id'] = str(convo['_id'])
            return conversations
        except Exception as e:
            logger.error(f"Error retrieving completed conversations: {str(e)}")
            return []

    # ------------------ Example of Comprehensive Conversation Handling ------------------

    # Depending on your application's requirements, you might want to add more methods here.
    # For example, methods to archive conversations, fetch conversations based on date ranges, etc.


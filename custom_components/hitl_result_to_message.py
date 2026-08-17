from langflow.custom import Component
from langflow.io import DataInput, Output
from langflow.schema import Data
from langflow.schema.message import Message


class HITLResultToMessage(Component):
    display_name = "HITL Result to Message"
    description = "Converts the HITL result into a clean chat message."
    icon = "message-square"
    name = "HITLResultToMessage"

    inputs = [
        DataInput(
            name="result",
            display_name="HITL Result",
            info="The result returned by the Wait for Approval component.",
            required=True,
        )
    ]

    outputs = [
        Output(
            display_name="Message",
            name="message",
            method="build_message",
        )
    ]

    def build_message(self) -> Message:
        data = self.result.data
    
        status = data.get("status", "").lower()
        response = data.get("response", "")
    
        # APPROVED
        # Remove the Subject line and return only the body.
        if status == "approved":
            lines = response.splitlines()
    
            # Remove the first line if it starts with "Subject:"
            if lines and lines[0].strip().lower().startswith("subject:"):
                response = "\n".join(lines[1:]).lstrip()
    
            return Message(text=response)
    
        # REJECTED
        elif status == "rejected":
            return Message(
                text="❌ The reviewer rejected the AI response."
            )
    
        # PENDING / OTHER
        else:
            return Message(
                text="⏳ The AI response is awaiting human review."
            )
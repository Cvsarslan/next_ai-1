import os
import json
import frappe
import time
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from google.api_core.exceptions import ResourceExhausted, NotFound
from frappe import _
from next_ai.ai.prompt import PROMPTS, CHAT_SYSTEM_PROMPT
from next_ai.ai.structured_output import NEXTAIBaseModel
from next_ai.ai.utils import nextai_usage_log_create
from next_ai.ai.actions import TOOLS, run_action


@frappe.whitelist(allow_guest=True)
def test_gemini(**kwargs):

    if "GOOGLE_API_KEY" not in os.environ:
        return {"status": "error", "message": "GOOGLE_API_KEY not set in environment variables"}

    llm = ChatGoogleGenerativeAI(
        model="gemini-2.0-flash",
        temperature=0,
        max_tokens=None,
        timeout=None,
        max_retries=2,
        # other params...
    )

    messages = [
        (
            "system",
            "You are name is Next AI & Introduce like you Next AI. Ensure that you are working fine. giving a worm welcome to the user. In a simple text format only",
        ),
        ("human", "Who are you?"),
    ]
    ai_msg = llm.invoke(messages)
    ai_msg
    return {"status": "success", "message": ai_msg.content}


@frappe.whitelist(methods=["POST"])
def get_ai_response(**kwargs):
    nextai_llm = NextAILLM(template=PROMPTS[kwargs['type']], user_input=kwargs['value'], field_info=kwargs)
    message = nextai_llm.get_llm_response()
    return {"status_code":200, "status": "sucess", "message": message}


# CHAT_SYSTEM_PROMPT already contains all tool instructions


@frappe.whitelist(methods=["POST"])
def execute_action(tool_name: str, args: str = "{}"):
    """Direct action call for structured form submissions — no AI layer, no quota used."""
    try:
        args_dict = json.loads(args) if isinstance(args, str) else (args or {})
    except Exception:
        args_dict = {}
    return run_action(tool_name, args_dict)


@frappe.whitelist(methods=["POST"])
def chat_with_nextai(message: str, history: str = "[]"):
    """
    Chat endpoint with ERPNext action capabilities.
    Returns {status, message, action} where action is populated when a tool was executed.
    """
    if not message or not message.strip():
        frappe.throw(_("Message cannot be empty."))

    nextai_settings = frappe.get_doc("NextAI Settings")

    if not nextai_settings.api_key:
        frappe.throw(_("NextAI is not configured. Please contact the administrator."))

    api_key = nextai_settings.get_password("api_key")
    model_name = nextai_settings.model_name

    try:
        history_list = json.loads(history) if isinstance(history, str) else history
    except (ValueError, TypeError):
        history_list = []

    os.environ["GOOGLE_API_KEY"] = api_key

    llm = ChatGoogleGenerativeAI(model=model_name, temperature=0.7)
    llm_with_tools = llm.bind_tools(TOOLS)

    lc_messages = [SystemMessage(content=CHAT_SYSTEM_PROMPT)]
    for turn in history_list:
        role = turn.get("role", "")
        content = turn.get("content", "")
        if role == "user":
            lc_messages.append(HumanMessage(content=content))
        elif role == "assistant":
            lc_messages.append(AIMessage(content=content))
    lc_messages.append(HumanMessage(content=message))

    try:
        response = llm_with_tools.invoke(lc_messages)

        action_result = None

        # Check if the model wants to call a tool
        tool_calls = getattr(response, "tool_calls", None)
        if tool_calls:
            tool_call = tool_calls[0]
            tool_name = tool_call.get("name") or tool_call.get("function", {}).get("name")
            raw_args  = tool_call.get("args") or tool_call.get("function", {}).get("arguments", {})

            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except Exception:
                    raw_args = {}

            action_result = run_action(tool_name, raw_args)

            # Build reply from action result directly — no second API call needed
            if action_result.get("success"):
                reply = action_result.get("summary", f"{tool_name} completed successfully.")
            else:
                reply = f"Sorry, I couldn't complete that action: {action_result.get('error', 'Unknown error')}"
        else:
            reply = response.content

        frappe.enqueue(
            nextai_usage_log_create_internal,
            queue="short",
            platform=nextai_settings.platform,
            user=frappe.session.user,
            model_name=model_name,
            sub_type="Chat",
            question=message,
            prompt=CHAT_SYSTEM_PROMPT,
            response=reply,
            ref_doctype="",
            field_name="chat",
        )

        return {"status": "success", "message": reply, "action": action_result}

    except ResourceExhausted as e:
        frappe.log_error(frappe.get_traceback(), "NextAI Chat Error")
        # Extract retry delay from error message if available
        import re
        retry_match = re.search(r"retry in (\d+)", str(e))
        retry_hint = f" Please retry in {retry_match.group(1)} seconds." if retry_match else " Please wait a moment and try again."
        frappe.throw(_(
            "Gemini API rate limit reached for model <b>{0}</b> (free tier: 20 requests/day)."
            "{1} To increase the limit, upgrade your Google AI plan or switch to a paid model in "
            "<b>NextAI Settings</b>."
        ).format(model_name, retry_hint))
    except NotFound as e:
        frappe.log_error(frappe.get_traceback(), "NextAI Chat Error")
        frappe.throw(_("The configured model <b>{0}</b> was not found. Please update the model name in <b>NextAI Settings</b>.").format(model_name))
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "NextAI Chat Critical Error")
        frappe.throw(_("An error occurred: {0}").format(str(e)))


def get_delay_info(model_info, is_subscription, is_free):
    try:
        if is_subscription:
            delay = round(60/model_info.get('subscription_rpm'), 2)
        elif is_free:
            delay = round(60/model_info.get('free_rpm'), 2)
        else:
            delay = 3
    except ZeroDivisionError as e:
        delay = 3

    if not delay:
        frappe.throw(_("Delay information not found for the selected model."))

    return delay


class NextAILLM:
    def __init__(self, template: str = None, user_input: str = None, field_info: dict = None):

        self.template = template
        self.user_input = user_input
        self.field_info = field_info
        self.is_error = self.is_critical = False

        self.prompt = self.get_prompt() or template.format(input=user_input)
        self.validate_token()

        self.nextai_settings = self.get_nextai_settings()
        self.validate_settings()

        self.current_model = self.nextai_settings.model_name
        self.model_info = self.get_model_info()
        self.validate_model_info()

    def validate_model_info(self):
        if not self.model_info:
            frappe.log_error(frappe.get_traceback(), "No Active Model Info Found in NextAILLM.validate_model_info")
            frappe.throw(_("No active model info found for the platform {0}. Check NextAI Model Info Doctype.").format(self.nextai_settings.platform))
    
    def validate_token(self):
        if len(self.prompt) > 8000:
            frappe.throw(_("Prompt length exceeds the maximum limit of 8000 characters. Please shorten your prompt."))
    
    def validate_settings(self):
        if not self.nextai_settings.model_name:
            frappe.throw(_("Model name is not set in NextAI Settings. Please configure the model name."))
        if not self.nextai_settings.platform:
            frappe.throw(_("Platform is not set in NextAI Settings. Please configure the platform."))
        if not self.nextai_settings.get_password("api_key"):
            frappe.throw(_("API Key is not set in NextAI Settings. Please configure the API Key."))
    
    def get_prompt(self):
        
        if self.field_info.get('doctype'):
            query = f"""
            SELECT
                ref_doctype, field_name, field_type, prompt, is_user_specific
            FROM
                `tabNextAI Prompt`
            WHERE
                ref_doctype='{self.field_info.get('doctype')}'
                AND field_name='{self.field_info.get('key')}'
                AND user='{frappe.session.user}'
                AND enable=1
                AND is_user_specific=1
            
            UNION ALL

            SELECT
                ref_doctype, field_name, field_type, prompt, is_user_specific
            FROM
                `tabNextAI Prompt`
            WHERE
                ref_doctype='{self.field_info.get('doctype')}'
                AND field_name='{self.field_info.get('key')}'
                AND enable=1
                AND is_user_specific=0
            """
            prompt_list = frappe.db.sql(query, as_dict=True)
            user_prompt = global_prompt = None 
            for prompt in prompt_list:
                if prompt['is_user_specific']:
                    user_prompt = prompt['prompt']
                else:
                    global_prompt = prompt['prompt']
            if user_prompt:
                return user_prompt.format(input=self.user_input)
            elif global_prompt:
                return global_prompt.format(input=self.user_input)
        
    def get_nextai_settings(self):
        nextai_settings = frappe.get_doc('NextAI Settings')
        return nextai_settings

    def get_model_info(self):
        model_info = frappe.db.get_list(
            'NextAI Model Info',
            fields=['*'],
            filters={
                'platform': self.nextai_settings.platform,
                'is_active': 1
            },
            order_by='creation desc'
        )
        if not model_info:
            frappe.log_error(frappe.get_traceback(), "No Active Model Info Found in NextAILLM.get_model_info")
            frappe.throw(_(f"No active model info found for the platform {self.nextai_settings.platform}. Check NextAI Model Info Doctype."))
        return model_info

    def get_llm(self, model_name: str = None):
        model_name = model_name or self.nextai_settings.model_name
        try:
            if self.nextai_settings.platform == 'Gemini':
                os.environ['GOOGLE_API_KEY'] = self.nextai_settings.get_password("api_key")
                llm = ChatGoogleGenerativeAI(model=model_name)
                return llm
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), "Error in NextAILLM.get_llm")
            frappe.throw(_("Error in getting LLM: {0}").format(str(e)))
    
    def get_structured_output_llm(self, model_name: str = None):
        llm = self.get_llm(model_name=model_name)
        so_llm = llm.with_structured_output(NEXTAIBaseModel)
        return so_llm

    def get_next_model(self, current_model: str = None) -> str:
        is_next = False
        next_model = None
        for model in self.model_info:
            if model['model_name'] == current_model:
                is_next = True
                continue
            if is_next:
                next_model = model['model_name']
                break
        next_model = next_model or self.model_info[0]['model_name']

        if next_model == self.nextai_settings.model_name:
            frappe.log_error(frappe.get_traceback(), "RPM limit reached for all models in NextAILLM.get_llm_response")
            frappe.throw(_("<b>RPM limit</b> has been reached for all models. Please <b>try again later</b> or <b>upgrade your plan</b>."))
        
        return next_model
    
    def get_llm_response(self, model_name: str = None) -> str:
        try:
            so_llm = self.get_structured_output_llm(model_name=model_name)
            ai_msg = so_llm.invoke(self.prompt)

            if self.is_error:
                self.nextai_settings.model_name = self.current_model
                self.nextai_settings.save(
                    ignore_permissions=True,
                    ignore_version=True
                )
            
            frappe.enqueue(
                nextai_usage_log_create_internal,
                queue='short',
                platform=self.nextai_settings.platform,
                user=frappe.session.user,
                model_name=self.current_model,
                sub_type='Text-to-Text',
                question=self.user_input,
                prompt=self.prompt,
                response=ai_msg.response,
                ref_doctype=self.field_info.get('doctype'),
                field_name=self.field_info.get('key')
            )
            

            return ai_msg.response
        except (ResourceExhausted, NotFound) as e:
            if isinstance(e, NotFound):
                frappe.log_error(f"Model not found {self.current_model}\n {frappe.get_traceback()}", "Model NotFound")
                frappe.db.set_value('NextAI Model Info', self.current_model, 'is_active', 0)
            if isinstance(e, ResourceExhausted):
                frappe.log_error(frappe.get_traceback(), f"RPM limit reached {self.current_model} in NextAILLM.get_llm_response")

            if self.nextai_settings.auto_switch_model_on_rpm:
                self.current_model = self.get_next_model(self.current_model)
                self.is_error = True
                return self.get_llm_response(model_name=self.current_model)
            else:
                if isinstance(e, NotFound):
                    frappe.throw(_(f"The selected model <b>{self.current_model}</b> is not active. Please choose the active model in <b>NextAI Settings</b>."))
                frappe.throw(_(f"RPM limit reached for the current model {self.current_model}. Please try again later."))
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), "Critical NextAILLM.get_llm_response")
            if self.is_critical:
                frappe.throw(_("You have reached the <b>limit</b> or the system is <b>too busy</b>.  Please try again later, or contact <b>NextAI Support</b> if you see this message frequently."))
            self.is_critical = True
            self.current_model = self.get_next_model(self.current_model)
            return self.get_llm_response(model_name=self.current_model)



def nextai_usage_log_create_internal(**kwargs):
    """
    This function is used to create usage log for nextai usage it should contains the
    """
    try:
        nextai_usage_log_create(**kwargs)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Error in nextai_usage_log_create_internal")
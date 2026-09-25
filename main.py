import os
import asyncio
import json
import random
import secrets
import uuid
import time
import base64
import threading
import queue
from contextvars import ContextVar
from flask import Flask, request, jsonify, send_file, Response, stream_with_context, g
from flask_restx import Api, Resource, fields, Namespace
from agent_framework import Agent, AgentSession
from agent_framework import tool as ai_function
from agent_framework.openai import OpenAIChatClient
from azure.identity import DefaultAzureCredential
from typing import Annotated, List, Dict, Any, Optional
from pydantic import Field
import requests
import aiohttp
import jwt
from jwt import InvalidTokenError

app = Flask(__name__)

# Initialize Flask-RESTx for OpenAPI/Swagger documentation
api = Api(
    app,
    version='1.0',
    title='Microsoft Agent Framework API',
    description='AI-powered chat assistant with intelligent tool selection using Microsoft Agent Framework and Azure OpenAI',
    doc='/docs/',  # Swagger UI will be available at /docs/
    prefix='/api',  # All API routes will be prefixed with /api
    contact='Microsoft Agent Framework Team',
    license='MIT',
    license_url='https://opensource.org/licenses/MIT',
    authorizations={
        'Azure OpenAI': {
            'type': 'oauth2',
            'flow': 'clientCredentials',
            'description': 'Azure OpenAI authentication via Managed Identity'
        }
    }
)

# Define API namespaces
chat_ns = api.namespace('chat', description='Chat operations with AI agent')
system_ns = api.namespace('system', description='System information and health checks')
tools_ns = api.namespace('tools', description='AI tool management and discovery')

# Define API models for request/response schemas
chat_request_model = api.model('ChatRequest', {
    'prompt': fields.String(required=True, description='The user message or question', example='Execute print("Hello World")')
})

chat_response_model = api.model('ChatResponse', {
    'response': fields.String(required=True, description='AI agent response'),
    'agent': fields.String(required=True, description='Agent framework name'),
    'model': fields.String(required=True, description='AI model used'),
    'tools_used': fields.List(fields.Raw, description='List of tools that were used'),
    'tools_available': fields.List(fields.String, description='Available tools'),
    'conversation_length': fields.Integer(description='Number of messages in conversation')
})

health_response_model = api.model('HealthResponse', {
    'status': fields.String(required=True, description='System health status'),
    'framework': fields.String(required=True, description='Agent framework name'),
    'agent_name': fields.String(required=True, description='AI agent name'),
    'endpoint': fields.String(description='Azure OpenAI endpoint'),
    'model': fields.String(description='AI model deployment'),
    'tools_count': fields.Integer(description='Number of available tools'),
    'session_pool_configured': fields.Boolean(description='Whether Azure Container Apps session pool is configured'),
    'azure_configured': fields.Boolean(description='Whether Azure OpenAI is properly configured')
})

tool_info_model = api.model('ToolInfo', {
    'name': fields.String(required=True, description='Tool function name'),
    'description': fields.String(required=True, description='Tool description'),
    'parameters': fields.List(fields.String, description='Required parameters'),
    'example_usage': fields.String(description='Example usage query')
})

tools_response_model = api.model('ToolsResponse', {
    'tools': fields.List(fields.Nested(tool_info_model), description='Available AI tools'),
    'total_tools': fields.Integer(description='Total number of available tools'),
    'framework': fields.String(description='Agent framework name')
})

error_response_model = api.model('ErrorResponse', {
    'error': fields.String(required=True, description='Error message'),
    'code': fields.Integer(description='Error code'),
    'details': fields.String(description='Additional error details')
})

# Azure OpenAI setup
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT_NAME") or os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-35-turbo")

# Azure Container Apps Dynamic Sessions setup
SESSION_POOL_ENDPOINT = os.getenv("AZURE_CONTAINER_APPS_SESSION_POOL_ENDPOINT")
SUBSCRIPTION_ID = os.getenv("AZURE_SUBSCRIPTION_ID")
RESOURCE_GROUP = os.getenv("AZURE_RESOURCE_GROUP")
SESSION_POOL_NAME = os.getenv("AZURE_SESSION_POOL_NAME", "dynamic-session-pool")
SESSION_POOL_AUDIENCE = os.getenv("SESSION_POOL_AUDIENCE", "https://dynamicsessions.io/.default")
SESSION_COOKIE_NAME = "aca_sample_session"
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
SESSION_COOKIE_MAX_AGE_SECONDS = int(os.getenv("SESSION_COOKIE_MAX_AGE_SECONDS", "1800"))
MAX_CONVERSATION_THREADS = int(os.getenv("MAX_CONVERSATION_THREADS", "100"))
SESSION_SIGNING_KEY = os.getenv("SESSION_SIGNING_KEY") or secrets.token_hex(32)

# Chat endpoint authentication settings
ALLOW_UNAUTHENTICATED_CHAT = os.getenv("ALLOW_UNAUTHENTICATED_CHAT", "false").lower() in ("1", "true", "yes")
TRUST_EASYAUTH_HEADERS = os.getenv("TRUST_EASYAUTH_HEADERS", "false").lower() in ("1", "true", "yes")
CHAT_AUTH_TENANT_ID = os.getenv("CHAT_AUTH_TENANT_ID") or os.getenv("AZURE_TENANT_ID", "")
CHAT_AUTH_AUDIENCE = os.getenv("CHAT_AUTH_AUDIENCE", "")
CHAT_AUTH_CLIENT_ID = os.getenv("CHAT_AUTH_CLIENT_ID", "")
CHAT_AUTH_SCOPE = os.getenv("CHAT_AUTH_SCOPE", f"{CHAT_AUTH_AUDIENCE}/access_as_user" if CHAT_AUTH_AUDIENCE else "")

_entra_jwks_clients: Dict[str, Any] = {}

# Session management storage
active_sessions: Dict[str, Dict[str, Any]] = {}


def _get_request_tools() -> List[Dict[str, Any]]:
    tools = request_tools_used.get()
    if tools is None:
        tools = []
        request_tools_used.set(tools)
    return tools


def _decode_session_cookie(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        session_id = session_serializer.loads(
            value,
            max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
        )
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(session_id, str) or len(session_id) != 32:
        return None
    return session_id


def _cleanup_conversation_threads(now: float) -> None:
    expired_before = now - SESSION_COOKIE_MAX_AGE_SECONDS
    expired_ids = [
        session_id
        for session_id, state in conversation_threads.items()
        if state["last_used"] < expired_before
    ]
    for session_id in expired_ids:
        conversation_threads.pop(session_id, None)

    overflow = len(conversation_threads) - MAX_CONVERSATION_THREADS
    if overflow > 0:
        oldest_ids = sorted(
            conversation_threads,
            key=lambda session_id: conversation_threads[session_id]["last_used"],
        )[:overflow]
        for session_id in oldest_ids:
            conversation_threads.pop(session_id, None)


def _get_conversation_thread(session_id: str):
    now = time.monotonic()
    with conversation_threads_lock:
        _cleanup_conversation_threads(now)
        state = conversation_threads.get(session_id)
        if state is None:
            state = {
                "thread": agent.get_new_thread(),
                "last_used": now,
            }
            conversation_threads[session_id] = state
            _cleanup_conversation_threads(now)
        else:
            state["last_used"] = now
        return state["thread"]


@app.before_request
def assign_client_session() -> None:
    session_id = _decode_session_cookie(request.cookies.get(SESSION_COOKIE_NAME))
    if session_id is None:
        session_id = uuid.uuid4().hex
        g.set_session_cookie = True
    g.client_session_id = session_id


@app.after_request
def persist_client_session(response):
    if getattr(g, "clear_session_cookie", False):
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    elif getattr(g, "set_session_cookie", False):
        response.set_cookie(
            SESSION_COOKIE_NAME,
            session_serializer.dumps(g.client_session_id),
            httponly=True,
            secure=SESSION_COOKIE_SECURE,
            samesite="Lax",
            max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
            path="/",
        )
    return response


def _extract_bearer_token(auth_header: str) -> str:
    if not auth_header:
        return ""
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


def _get_entra_jwks_client(tenant_id: str) -> Any:
    if tenant_id not in _entra_jwks_clients:
        jwks_url = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
        _entra_jwks_clients[tenant_id] = jwt.PyJWKClient(jwks_url)
    return _entra_jwks_clients[tenant_id]


def _has_valid_entra_bearer_token() -> bool:
    if not CHAT_AUTH_TENANT_ID or not CHAT_AUTH_AUDIENCE:
        return False

    presented_token = _extract_bearer_token(request.headers.get("Authorization", ""))
    if not presented_token:
        return False

    try:
        jwks_client = _get_entra_jwks_client(CHAT_AUTH_TENANT_ID)
        signing_key = jwks_client.get_signing_key_from_jwt(presented_token).key
        expected_audiences = [CHAT_AUTH_AUDIENCE]
        if CHAT_AUTH_CLIENT_ID and CHAT_AUTH_CLIENT_ID not in expected_audiences:
            expected_audiences.append(CHAT_AUTH_CLIENT_ID)

        claims = jwt.decode(
            presented_token,
            signing_key,
            algorithms=["RS256"],
            audience=expected_audiences,
            options={"require": ["iss", "aud", "exp"]}
        )

        valid_issuers = {
            f"https://login.microsoftonline.com/{CHAT_AUTH_TENANT_ID}/v2.0",
            f"https://sts.windows.net/{CHAT_AUTH_TENANT_ID}/"
        }
        if claims.get("iss") not in valid_issuers:
            return False

        # Require a caller identity claim so only authenticated principals are accepted.
        return bool(claims.get("oid") or claims.get("sub"))
    except InvalidTokenError as token_error:
        print(f"❌ Chat auth token validation failed: {token_error}")
        return False
    except Exception as auth_error:
        print(f"❌ Chat auth processing error: {auth_error}")
        return False


def _has_trusted_easyauth_identity() -> bool:
    if not TRUST_EASYAUTH_HEADERS:
        return False

    principal_id = request.headers.get("X-MS-CLIENT-PRINCIPAL-ID", "")
    principal_provider = request.headers.get("X-MS-CLIENT-PRINCIPAL-IDP", "")
    return bool(principal_id) and principal_provider.lower() in ("aad", "entra")


def _is_authenticated_chat_request() -> bool:
    if ALLOW_UNAUTHENTICATED_CHAT:
        return True
    if _has_valid_entra_bearer_token():
        return True
    if _has_trusted_easyauth_identity():
        return True
    return False


@app.before_request
def require_auth_for_chat_endpoints():
    if request.path.startswith("/api/chat") and not _is_authenticated_chat_request():
        return jsonify({
            "error": "Authentication required for chat endpoints",
            "details": "Provide a valid Microsoft Entra Bearer token for CHAT_AUTH_AUDIENCE, or configure trusted platform authentication."
        }), 401

# Enhanced AI functions (tools) for the agent
@ai_function
def search_tools_available() -> str:
    """List all available tools and their capabilities."""
    _get_request_tools().append({"name": "search_tools_available", "icon": "🔧", "description": "Tool discovery"})
    print("🔧 TOOL CALLED: search_tools_available()")
    
    tools_info = """Available AI Tools:

🔍 search_tools_available(query) - Discover available tools and capabilities
📦 execute_in_dynamic_session(code) - Execute Python code in secure Azure Container Apps session

The AI agent automatically selects the appropriate tool(s) based on your request!"""
    
    print(f"🔧 TOOL RESPONSE: Listed available tools")
    return tools_info

def _trim_trailing_newlines(value: str) -> str:
    if not value:
        return value
    return value.rstrip("\n")

@ai_function
def execute_in_dynamic_session(
    code: Annotated[str, Field(description="Python code to execute in the secure session")]
) -> str:
    """Execute Python code securely in an Azure Container Apps dynamic session with custom container isolation.
    
    This tool executes Python code in isolated, secure containers. Always use Python for code execution.
    Each session runs in its own Hyper-V isolated container, providing enterprise-grade security.
    
    Examples:
    - execute_in_dynamic_session(code="print('hello world')")
    - execute_in_dynamic_session(code="x = 5\\nprint(x * 2)")
    """
    # Always use Python and isolate execution state by the current client session.
    
    # Write debug info to file to see if function is called
    import os
    debug_path = "/tmp/debug_session.log"
    try:
        with open(debug_path, "a") as f:
            f.write(f"FUNCTION CALLED: endpoint={SESSION_POOL_ENDPOINT}\n")
    except:
        pass  # Ignore file write errors
    
    try:
        session_id = current_session_id.get()
        if not session_id:
            return "Session error: No request session is available."

        _get_request_tools().append({
            "name": "execute_in_dynamic_session",
            "icon": "📦",
            "description": "Python Execution",
        })
        print(f"📦 TOOL CALLED: execute_in_dynamic_session()")
        print(f"🔍 SESSION_POOL_ENDPOINT: {SESSION_POOL_ENDPOINT}")
        
        # Check if session pool is configured
        if not SESSION_POOL_ENDPOINT:
            error_msg = "Azure Container Apps session pool not configured. Set AZURE_CONTAINER_APPS_SESSION_POOL_ENDPOINT environment variable."
            print(f"📦 TOOL ERROR: {error_msg}")
            return f"❌ Configuration Error: {error_msg}"
    except Exception as e:
        print(f"❌ EARLY ERROR in execute_in_dynamic_session: {str(e)}")
        return f"❌ Function Error: {str(e)}"
    
    try:
        # Get Azure authentication token for Container Apps Dynamic Sessions
        try:
            # Try using ManagedIdentityCredential explicitly first (for Container Apps)
            from azure.identity import ManagedIdentityCredential, DefaultAzureCredential
            
            # Check if we're running in Azure Container Apps (has managed identity)
            import os
            client_id = os.getenv('AZURE_CLIENT_ID')
            
            if client_id:
                print(f"🔐 Using managed identity: {client_id[:8]}...")
                credential = ManagedIdentityCredential(client_id=client_id)
            else:
                print(f"🔐 Using default credential chain...")
                credential = DefaultAzureCredential()
            
            # Acquire token for Session Pool management endpoint (audience configurable)
            token = credential.get_token(SESSION_POOL_AUDIENCE)
            print(f"🔑 Token acquired successfully")
        except Exception as auth_error:
            print(f"❌ Authentication failed: {auth_error}")
            return f"Authentication error: Unable to get access token. Error: {str(auth_error)}"
        
        headers = {
            "Authorization": f"Bearer {token.token}",
            "Content-Type": "application/json"
        }
        api_version = "2024-02-02-preview"  # Use working API version from documentation
        
        # Prepare execution request for Python code
        execution_payload = {
            "properties": {
                "codeInputType": "inline",
                "executionType": "synchronous",
                "timeoutInSeconds": 60,
                "code": code
            },
            # Add top-level fields as belt-and-suspenders for payload forwarding variations
            "code": code,
            "language": "python"
        }
        
        # Use custom container session API endpoint - Azure will create session automatically
        # According to Azure docs: <POOL_MANAGEMENT_ENDPOINT>/<API_PATH_EXPOSED_BY_CONTAINER>?identifier=<USER_ID>
        session_url = f"{SESSION_POOL_ENDPOINT}/execute?identifier={session_id}"
        
        # Execute request
        
        print("📦 Executing Python code in the current client session...")
        print(f"📋 Payload: {execution_payload}")
        try:
            print(f"🚀 Making authenticated request to: {session_url}")
            print(f"📦 Payload: {execution_payload}")
            
            response = requests.post(session_url, json=execution_payload, headers=headers, timeout=60)
            print(f"📊 Response Status: {response.status_code}")
            print(f"📝 Response Body: {response.text}")
        except requests.exceptions.RequestException as req_error:
            print(f"❌ Request failed: {req_error}")
            return f"Network error: Unable to connect to session pool. Error: {str(req_error)}"
        
        if response.status_code == 200:
            result = response.json()
            print(f"📊 DEBUG: Full response from session container: {result}")
            print(f"📊 DEBUG: Full response JSON: {json.dumps(result, indent=2)}")
            
            execution_state = {
                "last_stdout": "",
                "last_stderr": "",
            }
            
            # Extract execution result and capture stdout/stderr
            # Handle both formats: properties-based (Azure) and direct fields (our container)
            execution_result = ""
            if "properties" in result:
                # Azure Container Apps format
                props = result["properties"]
                print(f"📊 DEBUG: Properties from response: {props}")
                
                # Capture stdout and stderr
                stdout = _trim_trailing_newlines(props.get("stdout", ""))
                stderr = _trim_trailing_newlines(props.get("stderr", ""))
                status = props.get("status", "")
                return_code = props.get("returnCode", None)
                
                execution_state["last_stdout"] = stdout
                execution_state["last_stderr"] = stderr
                execution_state["last_returnCode"] = return_code
                
                # Determine if execution failed based on multiple signals
                # Check for error indicators in stdout (Python errors often go to stdout)
                has_error_in_stdout = any(error_keyword in stdout for error_keyword in [
                    "Error:", "Traceback", "Exception:", "ImportError:", "ModuleNotFoundError:",
                    "SyntaxError:", "NameError:", "TypeError:", "ValueError:", "AttributeError:"
                ])
                
                # If there's error content in stderr OR error patterns in stdout, mark as failed
                if stderr or has_error_in_stdout or status == "Failed" or (return_code and return_code != 0):
                    execution_state["last_status"] = "Failed"
                    # Move error from stdout to stderr if it contains error patterns
                    if has_error_in_stdout and not stderr:
                        execution_state["last_stderr"] = stdout
                        execution_state["last_stdout"] = ""
                else:
                    execution_state["last_status"] = "Success"
                
                print(f"📊 DEBUG: Raw props.stdout = {repr(stdout)}")
                print(f"📊 DEBUG: Raw props.stderr = {repr(stderr)}")
                print(f"📊 DEBUG: Status: '{status}', ReturnCode: {return_code}")
                print(f"📊 DEBUG: Has error in stdout: {has_error_in_stdout}")
                # Extract the execution result - use stderr if present, otherwise stdout
                execution_result = stderr if stderr else stdout
            else:
                # Direct format from our session container
                print(f"📊 DEBUG: Direct format (no properties wrapper): {result}")
                
                # Capture output and error
                stdout = _trim_trailing_newlines(result.get("output", ""))
                stderr = _trim_trailing_newlines(result.get("error", ""))
                return_code = result.get("return_code", 0)
                success = result.get("success", True)
                
                # Check for error indicators in stdout
                has_error_in_stdout = any(error_keyword in stdout for error_keyword in [
                    "Error:", "Traceback", "Exception:", "ImportError:", "ModuleNotFoundError:",
                    "SyntaxError:", "NameError:", "TypeError:", "ValueError:", "AttributeError:"
                ])
                
                # Determine actual status based on multiple signals
                if stderr or has_error_in_stdout or not success or return_code != 0:
                    # Move error from stdout to stderr if needed
                    if has_error_in_stdout and not stderr:
                        execution_state["last_stderr"] = stdout
                        execution_state["last_stdout"] = ""
                    else:
                        execution_state["last_stdout"] = stdout
                        execution_state["last_stderr"] = stderr
                    execution_state["last_status"] = "Failed"
                    execution_state["last_returnCode"] = return_code if return_code != 0 else 1
                else:
                    execution_state["last_stdout"] = stdout
                    execution_state["last_stderr"] = stderr
                    execution_state["last_status"] = "Success"
                    execution_state["last_returnCode"] = return_code
                
                print(f"📊 DEBUG: Captured stdout: '{stdout}', stderr: '{stderr}'")
                print(f"📊 DEBUG: Has error in stdout: {has_error_in_stdout}")
                print(f"📊 DEBUG: Final Status: '{execution_state['last_status']}', ReturnCode: {execution_state['last_returnCode']}")
                
                # Use stderr if present, otherwise stdout
                execution_result = execution_state["last_stderr"] if execution_state["last_stderr"] else execution_state["last_stdout"]
            
            return_code = execution_state.get("last_returnCode", 0)
            status = execution_state.get("last_status", "Success")
            stderr = execution_state.get("last_stderr", "")
            stdout = execution_state.get("last_stdout", "")
            
            # Format output with clear visual separation
            if status == "Failed" or return_code != 0 or stderr:
                # Execution failed
                formatted_output = f"""❌ **Code Execution Failed**

**Return Code:** {return_code}

**Code Executed:**
```python
{code}
```

**Error:**
```
{stderr if stderr else stdout}
```
"""
                print(f"📦 TOOL RESPONSE: Execution failed with return code {return_code}")
            else:
                # Execution successful
                formatted_output = f"""✅ **Code Execution Successful**

**Code Executed:**
```python
{code}
```

**Output:**
```
{stdout if stdout else '(no output)'}
```
"""
                print(f"📦 TOOL RESPONSE: Execution successful")
            
            return formatted_output
            
        elif response.status_code == 202:
            # Async execution - poll for result
            poll_url = response.headers.get("Location")
            if poll_url:
                print(f"📦 Polling for async result...")
                for _ in range(10):  # Poll up to 10 times
                    time.sleep(1)
                    poll_response = requests.get(poll_url, headers=headers)
                    if poll_response.status_code == 200:
                        result = poll_response.json()
                        if result.get("properties", {}).get("status") == "Completed":
                            execution_result = result.get("properties", {}).get("result", str(result))
                            success_msg = f"✅ Code executed successfully:\n\n{execution_result}"
                            print(f"📦 TOOL RESPONSE: Async execution successful")
                            return success_msg
                return "⏳ Code execution initiated but timed out waiting for result. Check session pool status."
            else:
                return "⏳ Code execution accepted but no polling URL provided."
        else:
            error_detail = response.text
            error_msg = f"Session execution failed (HTTP {response.status_code}): {error_detail}"
            print(f"📦 TOOL ERROR: {error_msg}")
            return f"❌ Execution Error: {error_msg}"
            
    except requests.exceptions.Timeout:
        error_msg = "Session execution timed out after 30 seconds"
        print(f"📦 TOOL ERROR: {error_msg}")
        return f"⏰ Timeout Error: {error_msg}"
    except Exception as e:
        error_msg = f"Unexpected error during session execution: {str(e)}"
        print(f"📦 TOOL ERROR: {error_msg}")
        return f"❌ System Error: {error_msg}"



# Create the main agent using Microsoft Agent Framework - following official docs pattern
agent = None
if AZURE_OPENAI_ENDPOINT:
    try:
        # Create Agent Framework client with managed identity - following official docs pattern
        chat_client = OpenAIChatClient(
            model=AZURE_OPENAI_DEPLOYMENT,
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            credential=DefaultAzureCredential()
        )
        
        # Create the agent (compatibility with multiple agent-framework versions)
        instructions = """You are an intelligent assistant with secure Python code execution in Azure Container Apps custom containers.

Key behaviors:
- Always analyze the user's request to determine which tool(s) are most appropriate
- Use tools when they can provide accurate, specific information
- Be conversational and helpful in your responses

Available capabilities:
- Tool discovery and help
- Secure Python code execution in Azure Container Apps dynamic sessions with custom containers

For mathematical calculations:
- CRITICAL: When a user asks for ANY mathematical calculation (addition, subtraction, multiplication, division, etc.), you MUST use execute_in_dynamic_session() to run Python code
- DO NOT just write out the math in text format - ALWAYS execute it as Python code
- DO NOT repeat the calculation result in your text response using LaTeX notation like \\(5 \\times 10\\) or mathematical symbols
- After executing the code, the result will be shown automatically in the formatted output - just provide a brief acknowledgment
- Example: If asked "what's 5 times 10", call execute_in_dynamic_session(code="result = 5 * 10\nprint(result)") and respond with "I've calculated that for you." or "Here's the result:"

For code execution requests:
- CRITICAL: When a user asks you to run, execute, or test Python code, you MUST immediately call execute_in_dynamic_session() - DO NOT warn about errors, DO NOT ask for confirmation, DO NOT suggest fixes first
- Just execute the code exactly as provided and let the execution results speak for themselves
- Do NOT ask the user what language to use - always use Python
- After calling the tool, include the execution results in your response
- Sessions are automatically managed and tracked
- If code fails, the error will be captured and displayed - that's expected and valuable feedback
- Let users learn from execution results rather than preventing them from running code

Always think step-by-step about which tools will best serve the user's needs."""
        tools = [search_tools_available, execute_in_dynamic_session]

        if hasattr(chat_client, "create_agent"):
            agent = chat_client.create_agent(
                instructions=instructions,
                tools=tools
            )
        else:
            agent = Agent(
                client=chat_client,
                instructions=instructions,
                name="SmartAssistant",
                tools=tools
            )
        print(f"✅ Connected to Azure OpenAI: {AZURE_OPENAI_ENDPOINT}")
        print(f"🤖 Using deployment: {AZURE_OPENAI_DEPLOYMENT}")
        print("🔧 Agent Framework agent created successfully using official pattern")
    except Exception as e:
        print(f"⚠️  Azure OpenAI connection failed: {e}")
        print("🔧 Running in demo mode without Azure OpenAI")
        agent = None
else:
    print("⚠️  AZURE_OPENAI_ENDPOINT not set")
    print("🔧 Set AZURE_OPENAI_ENDPOINT environment variable to use Azure OpenAI")
    print("🔧 Running in demo mode for now")


@app.route("/", methods=["GET"])
def index():
    """Simple chat interface for the Agent Framework"""
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Custom Container Agent</title>
    <style>
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 0;
            padding: 20px;
            background: #f5f5f5;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: flex-start;
        }
        .wrapper {
            display: flex;
            gap: 20px;
            max-width: 1400px;
            width: 100%;
        }
        .container {
            flex: 1;
            max-width: 800px;
            background: white;
            border-radius: 10px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.3);
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }
        .session-panel {
            width: 400px;
            background: white;
            border-radius: 10px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.3);
            padding: 20px;
            max-height: 450px;
            overflow-y: auto;
            align-self: flex-start;
        }
        .session-panel h3 {
            margin: 0 0 15px 0;
            color: #2c3e50;
            font-size: 18px;
        }
        .session-item {
            padding: 12px;
            margin: 12px 0;
            background: #f8f9fa;
            border-left: 4px solid #007bff;
            border-radius: 6px;
            font-size: 12px;
            font-family: 'Consolas', 'Monaco', monospace;
        }
        .session-id {
            font-weight: bold;
            color: #007bff;
            margin-bottom: 8px;
            font-size: 14px;
        }
        .session-item {
            margin-bottom: 15px;
            padding: 10px;
            background: #f8f9fa;
            border-radius: 6px;
            border-left: 3px solid #007bff;
        }
        .session-header {
            font-weight: bold;
            color: #0056b3;
            font-family: 'Consolas', 'Monaco', monospace;
            margin-bottom: 8px;
        }
        .session-json-toggle {
            margin-top: 10px;
            cursor: pointer;
        }
        .session-json-toggle summary {
            color: #007bff;
            font-size: 12px;
            font-weight: 500;
            padding: 4px 0;
            user-select: none;
        }
        .session-json-toggle summary:hover {
            color: #0056b3;
            text-decoration: underline;
        }
        .session-json {
            background: #2d2d2d;
            color: #d4d4d4;
            padding: 10px;
            border-radius: 4px;
            overflow-x: auto;
            white-space: pre-wrap;
            word-wrap: break-word;
            font-size: 11px;
            font-family: 'Consolas', 'Monaco', monospace;
            margin-top: 6px;
            max-height: 300px;
            overflow-y: auto;
        }
        .session-details {
            font-size: 12px;
            color: #495057;
        }
        .session-details > div {
            margin: 4px 0;
        }
        .output-section {
            margin-top: 8px;
            padding: 8px;
            background: #e7f3ff;
            border-radius: 4px;
        }
        .error-section {
            margin-top: 8px;
            padding: 8px;
            background: #ffe7e7;
            border-radius: 4px;
        }
        .output-section pre,
        .error-section pre {
            margin: 4px 0 0 0;
            padding: 6px;
            background: #2d2d2d;
            color: #d4d4d4;
            border-radius: 3px;
            font-family: 'Consolas', 'Monaco', monospace;
            font-size: 11px;
            overflow-x: auto;
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        .session-detail {
            color: #6c757d;
            font-size: 12px;
        }
        .no-sessions {
            color: #6c757d;
            font-style: italic;
            text-align: center;
            padding: 20px;
        }
        .header {
            background: #2c3e50;
            color: white;
            padding: 20px;
            text-align: center;
        }
        .header h1 {
            margin: 0;
            font-size: 24px;
        }
        .subtitle {
            margin: 5px 0 0 0;
            opacity: 0.8;
            font-size: 14px;
        }
        .chat-container {
            height: 500px;
            overflow-y: auto;
            padding: 20px;
            background: #f8f9fa;
        }
        .message {
            margin: 10px 0;
            padding: 10px 15px;
            border-radius: 10px;
            max-width: 70%;
            white-space: pre-wrap;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        .user-message {
            background: #007bff;
            color: white;
            margin-left: auto;
            text-align: right;
        }
        .bot-message {
            background: #e9ecef;
            color: #333;
        }
        .input-container {
            padding: 20px;
            border-top: 1px solid #ddd;
            display: flex;
            gap: 10px;
        }
        .input-field {
            flex: 1;
            padding: 12px;
            border: 1px solid #ddd;
            border-radius: 5px;
            font-size: 16px;
        }
        .send-button {
            padding: 12px 20px;
            background: #007bff;
            color: white;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            font-size: 16px;
        }
        .send-button:hover {
            background: #0056b3;
        }
        .secondary-button {
            background: #6c757d;
        }
        .secondary-button:hover {
            background: #545b62;
        }
        .send-button:disabled {
            background: #6c757d;
            cursor: not-allowed;
        }
        .tools-info {
            padding: 15px 20px;
            background: #fff3cd;
            border-left: 4px solid #ffc107;
            margin: 0;
            font-size: 14px;
        }
        .loading {
            opacity: 0.6;
        }
        .tools-used {
            margin-top: 8px;
            padding: 6px 12px;
            background: rgba(0, 123, 255, 0.05);
            border-left: 3px solid #007bff;
            border-radius: 0 6px 6px 0;
            font-size: 12px;
            color: #6c757d;
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }
        .tool-badge {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            background: rgba(0, 123, 255, 0.1);
            color: #0056b3;
            padding: 2px 6px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: 500;
        }
        .session-id {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            background: rgba(0, 123, 255, 0.1);
            color: #0056b3;
            padding: 2px 6px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: 500;
        }
        .bot-message code {
            background: #2d2d2d;
            color: #d4d4d4;
            padding: 1px 4px;
            border-radius: 3px;
            font-family: 'Consolas', 'Monaco', monospace;
            font-size: 13px;
        }
        .bot-message pre {
            background: #2d2d2d;
            color: #d4d4d4;
            padding: 12px;
            border-radius: 6px;
            overflow-x: auto;
            margin: 8px 0;
            font-family: 'Consolas', 'Monaco', monospace;
            font-size: 13px;
            line-height: 1.5;
        }
    </style>
</head>
<body>
    <div class="wrapper">
    <div class="container">
        <div class="header">
            <h1>🤖 Custom Container Agent</h1>
            <p class="subtitle">Dynamic Sessions + Agent Framework</p>
        </div>
        
        <div class="tools-info">
            <strong>Available Tools:</strong> Tool Discovery 🔧 | Python Execution 📦 | <a href="/docs/" style="color: #007bff;">📖 API Docs</a>
        </div>
        
        <div class="chat-container" id="chatContainer">
            <div class="message bot-message" style="white-space: normal;">
                Hello! I'm powered by Microsoft Agent Framework with Azure Container Apps dynamic sessions using custom containers. I can help you with secure Python code execution in isolated custom containers and tool discovery!
            </div>
        </div>
        
        <div class="input-container">
            <input type="text" id="messageInput" class="input-field" placeholder="Ask me to execute Python code or discover tools..." onkeypress="handleKeyPress(event)">
            <button id="loginButton" class="send-button secondary-button" onclick="handleLoginClick()" style="display: none;">Sign In</button>
            <button id="sendButton" class="send-button" onclick="sendMessage()">Send</button>
        </div>
    </div>
    </div>

    <script>
        const chatContainer = document.getElementById('chatContainer');
        const messageInput = document.getElementById('messageInput');
        const sendButton = document.getElementById('sendButton');
        const loginButton = document.getElementById('loginButton');

        let authRuntimeConfig = null;
        const TOKEN_STORAGE_KEY = 'entra_access_token';
        const TOKEN_EXPIRY_STORAGE_KEY = 'entra_access_token_expiry';
        const PKCE_VERIFIER_KEY = 'entra_pkce_verifier';
        const PKCE_STATE_KEY = 'entra_pkce_state';

        async function loadAuthConfig() {
            try {
                const response = await fetch('/api/system/auth-config', {
                    method: 'GET',
                    headers: { 'Content-Type': 'application/json' }
                });
                if (!response.ok) {
                    return null;
                }
                return await response.json();
            } catch (_error) {
                return null;
            }
        }

        function isBrowserAuthEnabled() {
            return !!(
                authRuntimeConfig &&
                authRuntimeConfig.enabled &&
                authRuntimeConfig.client_id &&
                authRuntimeConfig.tenant_id &&
                authRuntimeConfig.scope
            );
        }

        function hasValidAccessToken() {
            const token = sessionStorage.getItem(TOKEN_STORAGE_KEY);
            const expiry = Number(sessionStorage.getItem(TOKEN_EXPIRY_STORAGE_KEY) || '0');
            if (!token || !expiry) {
                return false;
            }
            return Date.now() < expiry;
        }

        function updateLoginButton() {
            if (!loginButton) {
                return;
            }
            if (!isBrowserAuthEnabled()) {
                loginButton.style.display = 'none';
                return;
            }

            loginButton.style.display = 'inline-block';
            loginButton.textContent = hasValidAccessToken() ? 'Sign Out' : 'Sign In';
        }

        async function toBase64Url(arrayBuffer) {
            const bytes = new Uint8Array(arrayBuffer);
            let binary = '';
            for (let i = 0; i < bytes.length; i++) {
                binary += String.fromCharCode(bytes[i]);
            }
            return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
        }

        function randomString(length = 64) {
            const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~';
            const random = new Uint8Array(length);
            crypto.getRandomValues(random);
            let out = '';
            for (let i = 0; i < length; i++) {
                out += chars[random[i] % chars.length];
            }
            return out;
        }

        async function beginPkceLogin() {
            const tenant = authRuntimeConfig.tenant_id;
            const clientId = authRuntimeConfig.client_id;
            const redirectUri = window.location.origin + '/';
            const scope = `${authRuntimeConfig.scope} openid profile`;

            const codeVerifier = randomString(96);
            const state = randomString(40);
            const challengeBuffer = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(codeVerifier));
            const codeChallenge = await toBase64Url(challengeBuffer);

            sessionStorage.setItem(PKCE_VERIFIER_KEY, codeVerifier);
            sessionStorage.setItem(PKCE_STATE_KEY, state);

            const authorizeUrl = new URL(`https://login.microsoftonline.com/${tenant}/oauth2/v2.0/authorize`);
            authorizeUrl.searchParams.set('client_id', clientId);
            authorizeUrl.searchParams.set('response_type', 'code');
            authorizeUrl.searchParams.set('redirect_uri', redirectUri);
            authorizeUrl.searchParams.set('response_mode', 'query');
            authorizeUrl.searchParams.set('scope', scope);
            authorizeUrl.searchParams.set('state', state);
            authorizeUrl.searchParams.set('code_challenge', codeChallenge);
            authorizeUrl.searchParams.set('code_challenge_method', 'S256');

            window.location.href = authorizeUrl.toString();
        }

        async function redeemAuthorizationCode(code, state) {
            const expectedState = sessionStorage.getItem(PKCE_STATE_KEY);
            const codeVerifier = sessionStorage.getItem(PKCE_VERIFIER_KEY);

            if (!expectedState || !codeVerifier || state !== expectedState) {
                throw new Error('Invalid sign-in state');
            }

            const tenant = authRuntimeConfig.tenant_id;
            const clientId = authRuntimeConfig.client_id;
            const redirectUri = window.location.origin + '/';

            const tokenUrl = `https://login.microsoftonline.com/${tenant}/oauth2/v2.0/token`;
            const body = new URLSearchParams();
            body.set('client_id', clientId);
            body.set('grant_type', 'authorization_code');
            body.set('code', code);
            body.set('redirect_uri', redirectUri);
            body.set('code_verifier', codeVerifier);
            body.set('scope', `${authRuntimeConfig.scope} openid profile`);

            const response = await fetch(tokenUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: body.toString()
            });

            if (!response.ok) {
                let errorMessage = 'Token exchange failed';
                try {
                    const errorPayload = await response.json();
                    errorMessage = errorPayload.error_description || errorPayload.error || errorMessage;
                } catch (_parseError) {
                    // Keep the generic message when the identity provider does not return JSON.
                }
                throw new Error(errorMessage);
            }

            const payload = await response.json();
            const expiresIn = Number(payload.expires_in || 3600);
            const expiryMs = Date.now() + (expiresIn - 60) * 1000;

            sessionStorage.setItem(TOKEN_STORAGE_KEY, payload.access_token);
            sessionStorage.setItem(TOKEN_EXPIRY_STORAGE_KEY, String(expiryMs));

            sessionStorage.removeItem(PKCE_VERIFIER_KEY);
            sessionStorage.removeItem(PKCE_STATE_KEY);
        }

        function clearTokenState() {
            sessionStorage.removeItem(TOKEN_STORAGE_KEY);
            sessionStorage.removeItem(TOKEN_EXPIRY_STORAGE_KEY);
            sessionStorage.removeItem(PKCE_VERIFIER_KEY);
            sessionStorage.removeItem(PKCE_STATE_KEY);
        }

        async function initBrowserAuth() {
            authRuntimeConfig = await loadAuthConfig();
            if (!authRuntimeConfig || !authRuntimeConfig.enabled || !authRuntimeConfig.client_id || !authRuntimeConfig.tenant_id || !authRuntimeConfig.scope) {
                updateLoginButton();
                return;
            }

            try {
                const params = new URLSearchParams(window.location.search);
                const code = params.get('code');
                const state = params.get('state');
                if (code && state) {
                    await redeemAuthorizationCode(code, state);

                    const cleanUrl = new URL(window.location.href);
                    cleanUrl.searchParams.delete('code');
                    cleanUrl.searchParams.delete('state');
                    cleanUrl.searchParams.delete('session_state');
                    cleanUrl.searchParams.delete('error');
                    cleanUrl.searchParams.delete('error_description');
                    window.history.replaceState({}, document.title, cleanUrl.toString());
                }
            } catch (error) {
                clearTokenState();
                addMessage(`❌ Sign-in failed: ${error.message}`);
            }

            updateLoginButton();
        }

        async function handleLoginClick() {
            if (!isBrowserAuthEnabled()) {
                addMessage('🔒 Browser sign-in is not configured yet.');
                return;
            }

            if (hasValidAccessToken()) {
                clearTokenState();
                updateLoginButton();
                return;
            }

            await beginPkceLogin();
        }

        async function getAccessTokenForChat() {
            if (!isBrowserAuthEnabled()) {
                return null;
            }

            if (!hasValidAccessToken()) {
                return null;
            }

            return sessionStorage.getItem(TOKEN_STORAGE_KEY);
        }

        function addMessage(text, isUser = false, toolsUsed = null) {
            const messageDiv = document.createElement('div');
            messageDiv.className = `message ${isUser ? 'user-message' : 'bot-message'}`;
            
            // Create message content with markdown-style formatting
            const messageContent = document.createElement('div');
            if (!isUser) {
                // Escape first so untrusted sandbox output cannot inject markup,
                // then apply markdown-style formatting.
                let formatted = escapeHtml(text)
                    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
                    .replace(/```([\s\S]*?)```/g, '<pre>$1</pre>')
                    .replace(/`([^`]+)`/g, '<code>$1</code>');
                messageContent.innerHTML = formatted;
            } else {
                messageContent.textContent = text;
            }
            messageDiv.appendChild(messageContent);
            
            // Add tools used indicator if available
            if (!isUser && toolsUsed && toolsUsed.length > 0) {
                const toolsDiv = document.createElement('div');
                toolsDiv.className = 'tools-used';
                
                const toolsLabel = document.createElement('span');
                toolsLabel.textContent = 'Tools used: ';
                toolsDiv.appendChild(toolsLabel);
                
                toolsUsed.forEach((tool, index) => {
                    const toolBadge = document.createElement('span');
                    toolBadge.className = 'tool-badge';
                    toolBadge.textContent = `${tool.icon} ${tool.description}`;
                    toolsDiv.appendChild(toolBadge);
                    
                    // Add spacing between badges
                    if (index < toolsUsed.length - 1) {
                        const space = document.createElement('span');
                        space.innerHTML = ' ';
                        toolsDiv.appendChild(space);
                    }
                });
                
                messageDiv.appendChild(toolsDiv);
            }
            
            chatContainer.appendChild(messageDiv);
            chatContainer.scrollTop = chatContainer.scrollHeight;
        }

        function handleKeyPress(event) {
            if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                sendMessage();
            }
        }

        async function sendMessage() {
            const message = messageInput.value.trim();
            if (!message) return;

            // Add user message
            addMessage(message, true);
            messageInput.value = '';
            
            // Disable input while processing
            sendButton.disabled = true;
            sendButton.textContent = 'Thinking...';
            chatContainer.classList.add('loading');

            try {
                let authorizationHeader = null;
                if (authRuntimeConfig && authRuntimeConfig.enabled) {
                    const accessToken = await getAccessTokenForChat();
                    if (!accessToken) {
                        addMessage('🔒 Sign-in required. Click Sign In, then try again.');
                        return;
                    }
                    authorizationHeader = `Bearer ${accessToken}`;
                }

                const response = await fetch('/api/chat/', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        ...(authorizationHeader ? { 'Authorization': authorizationHeader } : {}),
                    },
                    body: JSON.stringify({
                        prompt: message
                    })
                });

                if (response.status === 401) {
                    if (authRuntimeConfig && authRuntimeConfig.enabled) {
                        addMessage('🔒 Your sign-in token was rejected. Please Sign Out and Sign In again.');
                    } else {
                        addMessage('🔒 Authentication required for chat endpoints. Configure browser Entra auth or platform auth.');
                    }
                    return;
                }

                const data = await response.json();
                console.log('📥 Received response:', data);
                
                if (data.error) {
                    addMessage(`❌ Error: ${data.error}`);
                } else {
                    addMessage(
                        data.response || data.message || 'No response received',
                        false,
                        data.tools_used || null
                    );
                }
            } catch (error) {
                addMessage(`❌ Connection error: ${error.message}`);
            } finally {
                // Re-enable input
                sendButton.disabled = false;
                sendButton.textContent = 'Send';
                chatContainer.classList.remove('loading');
                messageInput.focus();
            }
        }

        function escapeHtml(value) {
            return String(value === undefined || value === null ? '' : value)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        function updateSessionPanel(sessions) {
            const sessionList = document.getElementById('sessionList');
            console.log('📊 Updating session panel:', sessions);
            
            if (!sessions || Object.keys(sessions).length === 0) {
                console.log('⚠️ No sessions to display');
                sessionList.innerHTML = '<div class="no-sessions">No active sessions</div>';
                return;
            }
            
            console.log('✅ Found', Object.keys(sessions).length, 'sessions');
            let html = '';
            for (const [sessionId, sessionData] of Object.entries(sessions)) {
                const shortId = sessionId.substring(0, 16);
                console.log('  📝 Session:', shortId, sessionData);
                const jsonData = JSON.stringify(sessionData, null, 2);
                
                // Format session data with better readability
                html += `
                    <div class="session-item">
                        <div class="session-header">🔹 Session ID: ${escapeHtml(shortId)}...</div>
                        <div class="session-details">
                            <div><strong>Executions:</strong> ${escapeHtml(sessionData.execution_count || 0)}</div>
                            <div><strong>Created:</strong> ${escapeHtml(new Date(sessionData.created_at).toLocaleTimeString())}</div>
                            ${sessionData.last_used ? `<div><strong>Last Used:</strong> ${escapeHtml(new Date(sessionData.last_used).toLocaleTimeString())}</div>` : ''}
                            ${sessionData.last_status ? `<div><strong>Status:</strong> ${escapeHtml(sessionData.last_status)}</div>` : ''}
                            ${sessionData.last_returnCode !== undefined ? `<div><strong>Return Code:</strong> ${escapeHtml(sessionData.last_returnCode)}</div>` : ''}
                            ${sessionData.last_stdout ? `<div class="output-section"><strong>stdout:</strong><pre>${escapeHtml(sessionData.last_stdout)}</pre></div>` : ''}
                            ${sessionData.last_stderr ? `<div class="error-section"><strong>stderr:</strong><pre>${escapeHtml(sessionData.last_stderr)}</pre></div>` : ''}
                        </div>
                        <details class="session-json-toggle">
                            <summary>View Raw JSON</summary>
                            <pre class="session-json">${escapeHtml(jsonData)}</pre>
                        </details>
                    </div>
                `;
            }
            sessionList.innerHTML = html;
        }
        

        initBrowserAuth();

        // Focus input on load
        messageInput.focus();
    </script>
</body>
</html>
    """

@chat_ns.route('/')
class Chat(Resource):
    @api.doc('chat_with_agent')
    @api.expect(chat_request_model)
    @api.marshal_with(chat_response_model, code=200)
    @api.response(401, 'Unauthorized', error_response_model)
    @api.response(400, 'Bad Request', error_response_model)
    @api.response(500, 'Internal Server Error', error_response_model)
    def post(self):
        """Send a message to the AI agent and get a response with automatic tool selection"""
        data = request.get_json(silent=True) or {}
        prompt = data.get("prompt", "")
        session_id = data.get("session_id", "default")
        
        if not isinstance(prompt, str) or not prompt.strip():
            return {"error": "No prompt provided"}, 400
        
        try:
            if not agent:
                return {
                    "error": "Azure OpenAI configuration required. Please set AZURE_OPENAI_ENDPOINT environment variable and ensure proper authentication."
                }, 500
            
            print("\n🚀 NEW REQUEST")
            print(f"📝 User Input: {prompt}")
            print("🤖 Agent analyzing request and selecting appropriate tools...")
            
            # Get or create conversation thread for session continuity
            if session_id not in conversation_threads:
                conversation_threads[session_id] = agent.create_session(session_id=session_id)
            
            thread = conversation_threads[session_id]
            
            tools_used = []
            session_token = current_session_id.set(session_id)
            tools_token = request_tools_used.set(tools_used)
            
            # Run the agent asynchronously with conversation thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            print(f"🤖 DEBUG: About to call agent.run() with prompt: {prompt[:50]}...")
            result = loop.run_until_complete(agent.run(prompt, session=thread))
            loop.close()
            print(f"🤖 DEBUG: agent.run() completed")
            
            print(f"🔧 DEBUG: Tools used during this request: {tools_used}")
            
            print(f"✅ Agent Response Generated")
            print(f"📤 Response: {result.text[:100]}...")
            if tools_used:
                print(f"🔧 Tools Used: {[tool['name'] for tool in tools_used]}")
            else:
                print(f"⚠️ WARNING: No tools were used for this request!")
            
            # Build tools_available list based on what's actually registered
            tools_available = ["search_tools_available"]
            if SESSION_POOL_ENDPOINT:
                tools_available.append("execute_in_dynamic_session")
            
            response_data = {
                "response": result.text,
                "agent": "Microsoft Agent Framework SmartAssistant",
                "model": AZURE_OPENAI_DEPLOYMENT,
                "tools_used": tools_used,
                "tools_available": tools_available,
                "conversation_length": len(thread.messages) if hasattr(thread, 'messages') else 0
            }
            return response_data
        except Exception as e:
            print(f"❌ Error: {str(e)}")
            return {"error": str(e)}, 500

@chat_ns.route('/stream')
class ChatStream(Resource):
    @api.doc('chat_stream')
    @api.expect(chat_request_model)
    @api.response(200, 'Success')
    @api.response(401, 'Unauthorized', error_response_model)
    @api.response(400, 'Bad Request', error_response_model)
    @api.response(500, 'Internal Server Error', error_response_model)
    def post(self):
        """Stream responses from the AI agent in real-time (experimental)"""
        data = request.get_json(silent=True) or {}
        prompt = data.get("prompt", "")
        session_id = data.get("session_id", "default")
        
        if not isinstance(prompt, str) or not prompt.strip():
            return {"error": "No prompt provided"}, 400
        
        try:
            print("\n🚀 STREAMING REQUEST")
            print(f"📝 User Input: {prompt}")
            
            # Get or create conversation thread
            if session_id not in conversation_threads:
                conversation_threads[session_id] = agent.create_session(session_id=session_id)
            
            thread = conversation_threads[session_id]
            
            def stream_generator():
                q = queue.Queue()
                
                def run_async_stream():
                    session_token = current_session_id.set(session_id)
                    tools_token = request_tools_used.set([])
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                    async def collect_stream():
                        try:
                            async for chunk in agent.run(prompt, session=thread, stream=True):
                                if chunk.text:
                                    q.put(("data", chunk.text))
                                    print(f"📡 Streaming: {chunk.text}", end="", flush=True)
                            q.put(("done", None))
                        except Exception as stream_error:
                            q.put(("error", str(stream_error)))

                    try:
                        loop.run_until_complete(collect_stream())
                    finally:
                        loop.close()
                        current_session_id.reset(session_token)
                        request_tools_used.reset(tools_token)

                threading.Thread(target=run_async_stream, daemon=True).start()

                # Initial event so the connection starts immediately
                yield "event: open\ndata: {}\n\n"

                while True:
                    try:
                        kind, payload = q.get(timeout=10)
                    except queue.Empty:
                        # Keep-alive ping to avoid idle timeouts
                        yield "event: ping\ndata: {}\n\n"
                        continue

                    if kind == "data" and payload is not None:
                        yield f"data: {json.dumps({'text': payload})}\n\n"
                    elif kind == "error" and payload is not None:
                        yield f"event: error\ndata: {json.dumps({'error': payload})}\n\n"
                        break
                    elif kind == "done":
                        yield "event: done\ndata: {}\n\n"
                        break

                print(f"\n✅ Streaming Complete")

            return Response(
                stream_with_context(stream_generator()),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no"
                }
            )
        except Exception as e:
            print(f"❌ Streaming Error: {str(e)}")
            return {"error": str(e)}, 500

@system_ns.route('/test-session-payload')  
class TestSessionPayload(Resource):
    @api.doc('test_session_payload')
    def post(self):
        """Test endpoint to debug session payload format"""
        try:
            data = request.get_json()
            print(f"🔍 TEST DEBUG - Raw request data: {data}", flush=True)
            
            if not data:
                return {"error": "No JSON data provided"}, 400
                
            properties = data.get('properties', {})
            print(f"🔍 TEST DEBUG - Properties: {properties}", flush=True)
            
            code = properties.get('code', '')
            print(f"🔍 TEST DEBUG - Code: {repr(code)} (len={len(code)})", flush=True)
            
            has_code = code and code.strip()
            print(f"🔍 TEST DEBUG - has_code: {has_code}", flush=True)
            
            if not has_code:
                return {"error": "No code provided"}, 400
                
            return {"success": True, "code_received": code, "length": len(code)}
        except Exception as e:
            print(f"🔍 TEST DEBUG - Exception: {e}", flush=True)
            return {"error": str(e)}, 500

@system_ns.route('/auth-config')
class AuthConfig(Resource):
    @api.doc('auth_config')
    def get(self):
        return {
            "enabled": bool(CHAT_AUTH_CLIENT_ID and CHAT_AUTH_TENANT_ID and CHAT_AUTH_SCOPE),
            "tenant_id": CHAT_AUTH_TENANT_ID,
            "client_id": CHAT_AUTH_CLIENT_ID,
            "audience": CHAT_AUTH_AUDIENCE,
            "scope": CHAT_AUTH_SCOPE,
            "platform_auth_enabled": TRUST_EASYAUTH_HEADERS
        }

@system_ns.route('/health')
class Health(Resource):
    @api.doc('health_check')
    @api.marshal_with(health_response_model, code=200)
    @api.response(500, 'Configuration Error', error_response_model)
    def get(self):
        """Check system health and configuration status"""
        global agent
        if not agent:
            return {
                "status": "configuration_error",
                "error": "Azure OpenAI configuration required",
                "framework": "Microsoft Agent Framework",
                "azure_configured": False
            }, 500
        
        return {
            "status": "healthy",
            "framework": "Microsoft Agent Framework",
            "agent_name": "SmartAssistant",
            "endpoint": AZURE_OPENAI_ENDPOINT,
            "model": AZURE_OPENAI_DEPLOYMENT,
            "tools_count": 3,
            "session_pool_configured": bool(SESSION_POOL_ENDPOINT),
            "azure_configured": True
        }

@tools_ns.route('/')
class Tools(Resource):
    @api.doc('list_tools')
    @api.marshal_with(tools_response_model, code=200)
    def get(self):
        """Get detailed information about all available AI tools"""
        return {
            "tools": [
                {
                    "name": "search_tools_available",
                    "description": "List all available tools and their capabilities",
                    "parameters": [],
                    "example_usage": "What tools do you have?"
                },
                {
                    "name": "execute_in_dynamic_session",
                    "description": "Execute Python code securely in Azure Container Apps dynamic sessions",
                    "parameters": ["code (Python source code)"],
                    "example_usage": "Run this code: print('Hello from Azure!')"
                }
            ],
            "total_tools": 2,
            "framework": "Microsoft Agent Framework"
        }

@chat_ns.route('/session')
class SessionManager(Resource):
    @api.doc('clear_session')
    @api.response(200, 'Session cleared successfully')
    @api.response(401, 'Unauthorized', error_response_model)
    @api.response(404, 'Session not found')
    def delete(self, session_id):
        """Clear conversation history for a specific session"""
        if session_id in conversation_threads:
            del conversation_threads[session_id]
            return {"message": f"Session {session_id} cleared"}
        else:
            return {"message": f"Session {session_id} not found"}, 404

if __name__ == "__main__":
    print("🚀 Starting Microsoft Agent Framework SmartAssistant")
    print(f"📡 Azure OpenAI Endpoint: {AZURE_OPENAI_ENDPOINT}")
    print(f"🤖 Model Deployment: {AZURE_OPENAI_DEPLOYMENT}")
    print("🔧 Available Tools:")
    print("   🔧 search_tools_available - Tool discovery")
    print("   📦 execute_in_dynamic_session - Secure code execution")
    print(f"🔗 Python Pool: {'Configured' if SESSION_POOL_ENDPOINT else 'Not configured'}")
    print("\n📋 Endpoints:")
    print("   POST /chat - Main chat interface")
    print("   POST /chat/stream - Streaming responses")
    print("   GET /tools - List available tools")
    print("   GET /health - Health check")
    print("   DELETE /api/chat/session - Clear the current client session")
    print("\n🎯 The agent will automatically select appropriate tools based on your requests!")
    app.run(host="0.0.0.0", port=8080)

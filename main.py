import os
import asyncio
import json
import random
import uuid
import time
import base64
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file
from flask_restx import Api, Resource, fields, Namespace
from agent_framework import ChatAgent, ai_function, AgentThread
from agent_framework.azure import AzureOpenAIChatClient
from azure.identity import DefaultAzureCredential
from typing import Annotated, List, Dict, Any, Optional
from pydantic import Field
import requests
import aiohttp

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
    'prompt': fields.String(required=True, description='The user message or question', example='Execute print("Hello World")'),
    'session_id': fields.String(required=False, description='Session identifier for conversation continuity', example='user_123')
})

chat_response_model = api.model('ChatResponse', {
    'response': fields.String(required=True, description='AI agent response'),
    'session_id': fields.String(required=True, description='Session identifier'),
    'agent': fields.String(required=True, description='Agent framework name'),
    'model': fields.String(required=True, description='AI model used'),
    'tools_used': fields.List(fields.Raw, description='List of tools that were used'),
    'tools_available': fields.List(fields.String, description='Available tools'),
    'conversation_length': fields.Integer(description='Number of messages in conversation'),
    'active_sessions': fields.Raw(description='Active dynamic sessions with execution details')
})

health_response_model = api.model('HealthResponse', {
    'status': fields.String(required=True, description='System health status'),
    'framework': fields.String(required=True, description='Agent framework name'),
    'agent_name': fields.String(required=True, description='AI agent name'),
    'endpoint': fields.String(description='Azure OpenAI endpoint'),
    'model': fields.String(description='AI model deployment'),
    'tools_count': fields.Integer(description='Number of available tools'),
    'active_sessions': fields.Integer(description='Number of active chat sessions'),
    'dynamic_sessions': fields.Integer(description='Number of active dynamic sessions'),
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

# Session management storage
active_sessions: Dict[str, Dict[str, Any]] = {}

# Track which session IDs have been used in current request to avoid duplicates
current_request_sessions: set = set()

# Enhanced AI functions (tools) for the agent
@ai_function
def search_tools_available() -> str:
    """List all available tools and their capabilities."""
    global current_tools_used
    current_tools_used.append({"name": "search_tools_available", "icon": "🔧", "description": "Tool discovery"})
    print("🔧 TOOL CALLED: search_tools_available()")
    
    tools_info = """Available AI Tools:

🔍 search_tools_available(query) - Discover available tools and capabilities
📦 execute_in_dynamic_session(code) - Execute Python code in secure Azure Container Apps session

The AI agent automatically selects the appropriate tool(s) based on your request!"""
    
    print(f"🔧 TOOL RESPONSE: Listed available tools")
    return tools_info

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
    # Always use Python and reuse existing sessions when available
    
    # Write debug info to file to see if function is called
    import os
    debug_path = "/tmp/debug_session.log"
    try:
        with open(debug_path, "a") as f:
            f.write(f"FUNCTION CALLED: endpoint={SESSION_POOL_ENDPOINT}\n")
    except:
        pass  # Ignore file write errors
    
    try:
        # Reuse existing session if available, otherwise create new one
        session_id = None
        if active_sessions:
            session_id = list(active_sessions.keys())[-1]
            print(f"📦 Reusing existing session: {session_id}")
        else:
            session_id = uuid.uuid4().hex[:12]
            print(f"📦 Creating new session: {session_id}")
        
        global current_tools_used, current_request_sessions
        
        # Only track if this session hasn't been used in current request
        if session_id not in current_request_sessions:
            current_tools_used.append({
                "name": "execute_in_dynamic_session", 
                "icon": "📦", 
                "description": "Python Execution", 
                "session_id": session_id
            })
            current_request_sessions.add(session_id)
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
        
        print(f"📦 Executing Python code in session {session_id[:8]}...")
        print(f"🔗 Session URL: {session_url}")
        print(f"📋 Payload: {execution_payload}")
        try:
            print(f"🚀 Making request to: {session_url}")
            print(f"📋 Headers: {headers}")
            print(f"📦 Payload: {execution_payload}")
            
            response = requests.post(session_url, json=execution_payload, headers=headers, timeout=60)
            print(f"📊 Response Status: {response.status_code}")
            print(f"📝 Response Headers: {dict(response.headers)}")
            print(f"📝 Response Body: {response.text}")
        except requests.exceptions.RequestException as req_error:
            print(f"❌ Request failed: {req_error}")
            return f"Network error: Unable to connect to session pool. Error: {str(req_error)}"
        
        if response.status_code == 200:
            result = response.json()
            print(f"📊 DEBUG: Full response from session container: {result}")
            print(f"📊 DEBUG: Full response JSON: {json.dumps(result, indent=2)}")
            
            # Track auto-allocated session
            if session_id not in active_sessions:
                active_sessions[session_id] = {
                    "created_at": datetime.now().isoformat(),
                    "execution_count": 0,
                    "last_stdout": "",
                    "last_stderr": ""
                }
                print(f"✅ Session auto-allocated: {session_id}")
            
            # Update session statistics
            active_sessions[session_id]["execution_count"] += 1
            active_sessions[session_id]["last_used"] = datetime.now().isoformat()
            
            # Debug logging
            print(f"📊 DEBUG: active_sessions dict has {len(active_sessions)} entries")
            print(f"📊 DEBUG: active_sessions = {active_sessions}")
            
            # Extract execution result and capture stdout/stderr
            # Handle both formats: properties-based (Azure) and direct fields (our container)
            execution_result = ""
            if "properties" in result:
                # Azure Container Apps format
                props = result["properties"]
                print(f"📊 DEBUG: Properties from response: {props}")
                
                # Capture stdout and stderr
                stdout = props.get("stdout", "")
                stderr = props.get("stderr", "")
                status = props.get("status", "")
                return_code = props.get("returnCode", None)
                
                active_sessions[session_id]["last_stdout"] = stdout
                active_sessions[session_id]["last_stderr"] = stderr
                active_sessions[session_id]["last_returnCode"] = return_code
                
                # Determine if execution failed based on multiple signals
                # Check for error indicators in stdout (Python errors often go to stdout)
                has_error_in_stdout = any(error_keyword in stdout for error_keyword in [
                    "Error:", "Traceback", "Exception:", "ImportError:", "ModuleNotFoundError:",
                    "SyntaxError:", "NameError:", "TypeError:", "ValueError:", "AttributeError:"
                ])
                
                # If there's error content in stderr OR error patterns in stdout, mark as failed
                if stderr or has_error_in_stdout or status == "Failed" or (return_code and return_code != 0):
                    active_sessions[session_id]["last_status"] = "Failed"
                    # Move error from stdout to stderr if it contains error patterns
                    if has_error_in_stdout and not stderr:
                        active_sessions[session_id]["last_stderr"] = stdout
                        active_sessions[session_id]["last_stdout"] = ""
                else:
                    active_sessions[session_id]["last_status"] = "Success"
                
                print(f"📊 DEBUG: Raw props.stdout = {repr(stdout)}")
                print(f"📊 DEBUG: Raw props.stderr = {repr(stderr)}")
                print(f"📊 DEBUG: Status: '{status}', ReturnCode: {return_code}")
                print(f"📊 DEBUG: Has error in stdout: {has_error_in_stdout}")
                print(f"📊 DEBUG: Final active_sessions[{session_id}] = {active_sessions[session_id]}")
                
                # Extract the execution result - use stderr if present, otherwise stdout
                execution_result = stderr if stderr else stdout
            else:
                # Direct format from our session container
                print(f"📊 DEBUG: Direct format (no properties wrapper): {result}")
                
                # Capture output and error
                stdout = result.get("output", "")
                stderr = result.get("error", "")
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
                        active_sessions[session_id]["last_stderr"] = stdout
                        active_sessions[session_id]["last_stdout"] = ""
                    else:
                        active_sessions[session_id]["last_stdout"] = stdout
                        active_sessions[session_id]["last_stderr"] = stderr
                    active_sessions[session_id]["last_status"] = "Failed"
                    active_sessions[session_id]["last_returnCode"] = return_code if return_code != 0 else 1
                else:
                    active_sessions[session_id]["last_stdout"] = stdout
                    active_sessions[session_id]["last_stderr"] = stderr
                    active_sessions[session_id]["last_status"] = "Success"
                    active_sessions[session_id]["last_returnCode"] = return_code
                
                print(f"📊 DEBUG: Captured stdout: '{stdout}', stderr: '{stderr}'")
                print(f"📊 DEBUG: Has error in stdout: {has_error_in_stdout}")
                print(f"📊 DEBUG: Final Status: '{active_sessions[session_id]['last_status']}', ReturnCode: {active_sessions[session_id]['last_returnCode']}")
                print(f"📊 DEBUG: active_sessions[{session_id}] = {active_sessions[session_id]}")
                
                # Use stderr if present, otherwise stdout
                execution_result = active_sessions[session_id]["last_stderr"] if active_sessions[session_id]["last_stderr"] else active_sessions[session_id]["last_stdout"]
            
            # Check if execution was successful or failed (use updated values from active_sessions)
            return_code = active_sessions[session_id].get("last_returnCode", 0)
            status = active_sessions[session_id].get("last_status", "Success")
            stderr = active_sessions[session_id].get("last_stderr", "")
            stdout = active_sessions[session_id].get("last_stdout", "")
            
            # Format output with clear visual separation
            if status == "Failed" or return_code != 0 or stderr:
                # Execution failed
                formatted_output = f"""❌ **Code Execution Failed**

**Session ID:** {session_id[:12]}...
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

**Session ID:** {session_id[:12]}...

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
        chat_client = AzureOpenAIChatClient(
            deployment_name=AZURE_OPENAI_DEPLOYMENT,
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
- DO NOT repeat the calculation result in your text response using LaTeX notation like \(5 \times 10\) or mathematical symbols
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
            agent = ChatAgent(
                chat_client=chat_client,
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


# Global thread storage for conversation continuity
conversation_threads = {}

# Global tool usage tracking
current_tools_used = []

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
            <strong>Available Tools:</strong> Tool Discovery 🔧 | Python Execution 📦 | <a href="/docs/" target="_blank" style="color: #007bff;">📖 API Docs</a>
        </div>
        
        <div class="chat-container" id="chatContainer">
            <div class="message bot-message" style="white-space: normal;">
                Hello! I'm powered by Microsoft Agent Framework with Azure Container Apps dynamic sessions using custom containers. I can help you with secure Python code execution in isolated custom containers and tool discovery!
            </div>
        </div>
        
        <div class="input-container">
            <input type="text" id="messageInput" class="input-field" placeholder="Ask me to execute Python code or discover tools..." onkeypress="handleKeyPress(event)">
            <button id="sendButton" class="send-button" onclick="sendMessage()">Send</button>
        </div>
    </div>
    
    <div class="session-panel">
        <h3>📦 Active Sessions</h3>
        <div id="sessionList">
            <div class="no-sessions">No active sessions</div>
        </div>
    </div>
    </div>

    <script>
        const chatContainer = document.getElementById('chatContainer');
        const messageInput = document.getElementById('messageInput');
        const sendButton = document.getElementById('sendButton');

        function addMessage(text, isUser = false, toolsUsed = null) {
            const messageDiv = document.createElement('div');
            messageDiv.className = `message ${isUser ? 'user-message' : 'bot-message'}`;
            
            // Create message content with markdown-style formatting
            const messageContent = document.createElement('div');
            if (!isUser) {
                // Process code blocks and bold text for bot messages
                let formatted = text
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
                    toolBadge.innerHTML = `${tool.icon} ${tool.description}`;
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
                const response = await fetch('/api/chat/', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        prompt: message,
                        session_id: 'web_chat'
                    })
                });

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
                    console.log('🔄 Calling updateSessionPanel with:', data.active_sessions);
                    updateSessionPanel(data.active_sessions || {});
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
                        <div class="session-header">🔹 Session ID: ${shortId}...</div>
                        <div class="session-details">
                            <div><strong>Executions:</strong> ${sessionData.execution_count || 0}</div>
                            <div><strong>Created:</strong> ${new Date(sessionData.created_at).toLocaleTimeString()}</div>
                            ${sessionData.last_used ? `<div><strong>Last Used:</strong> ${new Date(sessionData.last_used).toLocaleTimeString()}</div>` : ''}
                            ${sessionData.last_status ? `<div><strong>Status:</strong> ${sessionData.last_status}</div>` : ''}
                            ${sessionData.last_returnCode !== undefined ? `<div><strong>Return Code:</strong> ${sessionData.last_returnCode}</div>` : ''}
                            ${sessionData.last_stdout ? `<div class="output-section"><strong>stdout:</strong><pre>${sessionData.last_stdout}</pre></div>` : ''}
                            ${sessionData.last_stderr ? `<div class="error-section"><strong>stderr:</strong><pre>${sessionData.last_stderr}</pre></div>` : ''}
                        </div>
                        <details class="session-json-toggle">
                            <summary>View Raw JSON</summary>
                            <pre class="session-json">${jsonData}</pre>
                        </details>
                    </div>
                `;
            }
            sessionList.innerHTML = html;
        }
        

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
    @api.response(400, 'Bad Request', error_response_model)
    @api.response(500, 'Internal Server Error', error_response_model)
    def post(self):
        """Send a message to the AI agent and get a response with automatic tool selection"""
        data = request.json
        prompt = data.get("prompt", "")
        session_id = data.get("session_id", "default")
        
        if not prompt:
            return {"error": "No prompt provided"}, 400
        
        try:
            if not agent:
                return {
                    "error": "Azure OpenAI configuration required. Please set AZURE_OPENAI_ENDPOINT environment variable and ensure proper authentication."
                }, 500
            
            print(f"\n🚀 NEW REQUEST (Session: {session_id})")
            print(f"📝 User Input: {prompt}")
            print("🤖 Agent analyzing request and selecting appropriate tools...")
            
            # Get or create conversation thread for session continuity
            if session_id not in conversation_threads:
                conversation_threads[session_id] = agent.get_new_thread()
            
            thread = conversation_threads[session_id]
            
            # Reset tool usage tracking for this request
            global current_tools_used, current_request_sessions
            current_tools_used = []
            current_request_sessions = set()
            print(f"🔧 DEBUG: Reset current_tools_used and session tracking, starting fresh for this request")
            
            # Run the agent asynchronously with conversation thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            print(f"🤖 DEBUG: About to call agent.run() with prompt: {prompt[:50]}...")
            result = loop.run_until_complete(agent.run(prompt, thread=thread))
            loop.close()
            print(f"🤖 DEBUG: agent.run() completed")
            
            # Get the tools that were used during this request
            tools_used = current_tools_used.copy()
            print(f"🔧 DEBUG: Tools used during this request: {tools_used}")
            
            print(f"✅ Agent Response Generated")
            print(f"📤 Response: {result.text[:100]}...")
            if tools_used:
                print(f"🔧 Tools Used: {[tool['name'] for tool in tools_used]}")
            else:
                print(f"⚠️ WARNING: No tools were used for this request!")
            
            import copy
            sessions_copy = copy.deepcopy(active_sessions)
            print(f"📊 Active Sessions Count: {len(sessions_copy)}")
            print(f"📊 DEBUG: sessions_copy = {sessions_copy}")
            if sessions_copy:
                print(f"📊 Session IDs: {list(sessions_copy.keys())}")
            
            # Build tools_available list based on what's actually registered
            tools_available = ["search_tools_available"]
            if SESSION_POOL_ENDPOINT:
                tools_available.append("execute_in_dynamic_session")
            
            response_data = {
                "response": result.text,
                "session_id": session_id,
                "agent": "Microsoft Agent Framework SmartAssistant",
                "model": AZURE_OPENAI_DEPLOYMENT,
                "tools_used": tools_used,
                "tools_available": tools_available,
                "conversation_length": len(thread.messages) if hasattr(thread, 'messages') else 0,
                "active_sessions": sessions_copy if sessions_copy else None
            }
            print(f"📊 DEBUG: Returning response with active_sessions = {response_data.get('active_sessions')}")
            return response_data
        except Exception as e:
            print(f"❌ Error: {str(e)}")
            return {"error": str(e)}, 500

@chat_ns.route('/stream')
class ChatStream(Resource):
    @api.doc('chat_stream')
    @api.expect(chat_request_model)
    @api.response(200, 'Success')
    @api.response(400, 'Bad Request', error_response_model)
    @api.response(500, 'Internal Server Error', error_response_model)
    def post(self):
        """Stream responses from the AI agent in real-time (experimental)"""
        data = request.json
        prompt = data.get("prompt", "")
        session_id = data.get("session_id", "default")
        
        if not prompt:
            return {"error": "No prompt provided"}, 400
        
        try:
            print(f"\n🚀 STREAMING REQUEST (Session: {session_id})")
            print(f"📝 User Input: {prompt}")
            
            # Get or create conversation thread
            if session_id not in conversation_threads:
                conversation_threads[session_id] = agent.get_new_thread()
            
            thread = conversation_threads[session_id]
            
            # Collect streaming responses
            responses = []
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            async def collect_stream():
                async for chunk in agent.run_stream(prompt, thread=thread):
                    if chunk.text:
                        responses.append(chunk.text)
                        print(f"📡 Streaming: {chunk.text}", end="", flush=True)
            
            loop.run_until_complete(collect_stream())
            loop.close()
            
            full_response = "".join(responses)
            print(f"\n✅ Streaming Complete")
            
            return {
                "response": full_response,
                "session_id": session_id,
                "streaming": True,
                "chunks_received": len(responses),
                "agent": "Microsoft Agent Framework SmartAssistant"
            }
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
            "active_sessions": len(conversation_threads),
            "dynamic_sessions": len(active_sessions),
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

@chat_ns.route('/sessions/<string:session_id>')
class SessionManager(Resource):
    @api.doc('clear_session')
    @api.response(200, 'Session cleared successfully')
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
    print(f"📊 Dynamic Sessions: {len(active_sessions)} active")
    print(f"🔗 Python Pool: {'Configured' if SESSION_POOL_ENDPOINT else 'Not configured'}")
    print("\n📋 Endpoints:")
    print("   POST /chat - Main chat interface")
    print("   POST /chat/stream - Streaming responses")
    print("   GET /tools - List available tools")
    print("   GET /health - Health check")
    print("   DELETE /sessions/<id> - Clear session")
    print("\n🎯 The agent will automatically select appropriate tools based on your requests!")
    app.run(host="0.0.0.0", port=8080)
